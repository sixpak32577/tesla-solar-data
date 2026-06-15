"""
Fetch one day of Tesla energy data via the API for comparison with local CSVs.

Shows native daily API totals, sub-daily intervals (summed), and the on-disk CSV row.
"""

import argparse
import csv
import os
import sys
from datetime import datetime

import pytz
import teslapy
from dateutil.parser import parse

from aggregate_energy_daily import aggregate_energy_rows
from tesla_solar_download import _get_timezone

ENERGY_FIELDS = (
    'grid_energy_imported',
    'consumer_energy_imported_from_grid',
    'solar_energy_exported',
    'grid_energy_exported_from_solar',
    'battery_energy_imported_from_grid',
)


def _ensure_authorized(tesla):
    tesla.redirect_uri = 'tesla://auth/callback'
    if tesla.authorized:
        return
    print('STEP 1: Log in to Tesla. Open this page in your browser:\n')
    print(tesla.authorization_url())
    print('\nPaste the tesla://auth/callback?... URL from the browser console:')
    auth_response = input('URL after authentication: ')
    auth_response = auth_response.replace(
        'tesla://auth/callback', 'https://auth.tesla.com/void/callback', 1
    )
    tesla.fetch_token(authorization_response=auth_response)
    print('Success!\n')


def _energy_sites(tesla):
    sites = []
    for product in tesla.api('PRODUCT_LIST')['response']:
        if product.get('resource_type') in ('battery', 'solar'):
            sites.append((product['energy_site_id'], product.get('resource_type')))
    return sites


def _day_bounds(date, timezone):
    tz = pytz.timezone(timezone)
    start = tz.localize(datetime(date.year, date.month, date.day, 0, 0, 0))
    end = tz.localize(datetime(date.year, date.month, date.day, 23, 59, 59))
    return start, end


def _fetch_energy(tesla, site_id, timezone, period, start, end, interval=None):
    params = {
        'path_vars': {'site_id': site_id},
        'kind': 'energy',
        'period': period,
        'start_date': start.isoformat(),
        'end_date': end.isoformat(),
        'time_zone': timezone,
    }
    if interval:
        params['interval'] = interval
    response = tesla.api('CALENDAR_HISTORY_DATA', **params)['response']
    if not response or 'time_series' not in response:
        return []
    return response['time_series']


def _rows_on_date(rows, date, timezone):
    tz = pytz.timezone(timezone)
    matched = []
    for row in rows:
        dt = parse(row['timestamp'])
        if dt.tzinfo is None:
            dt = tz.localize(dt)
        else:
            dt = dt.astimezone(tz)
        if dt.date() == date:
            matched.append(row)
    return matched


def _field_value(row, field):
    if field not in row or row[field] in (None, ''):
        return None
    return float(row[field])


def _sum_field(rows, field):
    total = 0.0
    count = 0
    for row in rows:
        value = _field_value(row, field)
        if value is None:
            continue
        total += value
        count += 1
    return total, count


def _format_wh_kwh(wh):
    if wh is None:
        return 'n/a'
    return f'{wh:,.1f} Wh  ({wh / 1000:.3f} kWh)'


def _load_csv_row(site_id, date):
    month_path = os.path.join(
        'download', str(site_id), 'energy', date.strftime('%Y-%m') + '.csv'
    )
    partial_path = month_path.replace('.csv', '.partial.csv')
    for path in (month_path, partial_path):
        if not os.path.exists(path):
            continue
        with open(path, newline='') as csv_file:
            for row in csv.DictReader(csv_file):
                if row.get('timestamp', '').startswith(date.isoformat()):
                    return row, path
    return None, None


def _print_field_comparison(label, rows, fields):
    print(f'\n{label}')
    if not rows:
        print('  (no rows)')
        return
    print(f'  intervals: {len(rows)}')
    for field in fields:
        total, count = _sum_field(rows, field)
        if count:
            print(f'  {field}: {_format_wh_kwh(total)}  (from {count} row(s))')
        else:
            print(f'  {field}: n/a')


def _print_single_row(label, row, fields):
    print(f'\n{label}')
    if not row:
        print('  (no row)')
        return
    print(f"  timestamp: {row.get('timestamp', 'n/a')}")
    for field in fields:
        value = _field_value(row, field)
        print(f'  {field}: {_format_wh_kwh(value)}')


def main():
    parser = argparse.ArgumentParser(
        description='Compare one day of Tesla energy API data vs local CSV'
    )
    parser.add_argument('--email', required=True, help='Tesla account email')
    parser.add_argument(
        '--date', required=True, help='Calendar date to inspect (YYYY-MM-DD)'
    )
    parser.add_argument(
        '--site-id',
        type=int,
        help='Energy site id (default: only site, or first solar/battery site)',
    )
    args = parser.parse_args()

    date = parse(args.date).date()
    tesla = teslapy.Tesla(args.email, retry=2, timeout=10)
    _ensure_authorized(tesla)

    sites = _energy_sites(tesla)
    if not sites:
        print('No solar/battery sites found.', file=sys.stderr)
        sys.exit(1)
    if args.site_id:
        site_id = args.site_id
    elif len(sites) == 1:
        site_id = sites[0][0]
    else:
        print('Multiple sites; use --site-id:', file=sys.stderr)
        for sid, rtype in sites:
            print(f'  {sid} ({rtype})', file=sys.stderr)
        sys.exit(1)

    site_config = tesla.api('SITE_CONFIG', path_vars={'site_id': site_id})['response']
    timezone = _get_timezone(site_config, parse(site_config['installation_date']))
    day_start, day_end = _day_bounds(date, timezone)
    month_start = day_start.replace(day=1)

    print(f'Site: ***{str(site_id)[-4:]}')
    print(f'Date: {date.isoformat()} (site local calendar day)')
    print(f'Timezone: {timezone}')
    print(f'API window: {day_start.isoformat()} .. {day_end.isoformat()}')

    native_day = _fetch_energy(
        tesla, site_id, timezone, 'day', day_start, day_end, interval=None
    )
    subdaily_15m = []
    try:
        subdaily_15m = _fetch_energy(
            tesla, site_id, timezone, 'day', day_start, day_end, interval='15m'
        )
    except Exception as exc:
        print(f'\nSub-daily (period=day, interval=15m): request failed: {exc}')

    month_series = _fetch_energy(
        tesla, site_id, timezone, 'month', month_start, day_end, interval=None
    )
    month_on_day = _rows_on_date(month_series, date, timezone)
    month_agg = aggregate_energy_rows(month_on_day) if month_on_day else []

    subdaily_on_day = _rows_on_date(subdaily_15m, date, timezone) if subdaily_15m else []
    subdaily_agg = aggregate_energy_rows(subdaily_on_day) if subdaily_on_day else []

    csv_row, csv_path = _load_csv_row(site_id, date)

    print('\n' + '=' * 60)
    print('API: period=day (no interval) — Tesla daily rollup')
    _print_field_comparison('  all rows returned', native_day, ENERGY_FIELDS)
    native_on_day = _rows_on_date(native_day, date, timezone)
    if len(native_on_day) != len(native_day):
        _print_field_comparison('  rows on target date only', native_on_day, ENERGY_FIELDS)

    print('\n' + '=' * 60)
    print('API: period=day, interval=15m — sub-daily buckets')
    _print_field_comparison('  sum of intervals on target date', subdaily_on_day, ENERGY_FIELDS)
    if subdaily_agg:
        _print_single_row('  after aggregate_energy_rows()', subdaily_agg[0], ENERGY_FIELDS)

    print('\n' + '=' * 60)
    print('API: period=month (download uses this), filtered to target date')
    _print_field_comparison('  sum of intervals on target date', month_on_day, ENERGY_FIELDS)
    if month_agg:
        _print_single_row('  after aggregate_energy_rows()', month_agg[0], ENERGY_FIELDS)

    print('\n' + '=' * 60)
    print('Local CSV')
    if csv_row:
        print(f'  file: {csv_path}')
        _print_single_row('  stored row', csv_row, ENERGY_FIELDS)
    else:
        print(f'  no row for {date.isoformat()} in download/{site_id}/energy/')

    print('\n' + '=' * 60)
    print('Compare to Tesla app (grid import example)')
    for field in ('grid_energy_imported', 'consumer_energy_imported_from_grid'):
        parts = []
        if native_on_day:
            total, _ = _sum_field(native_on_day, field)
            parts.append(f'API day rollup: {total / 1000:.3f} kWh')
        if month_agg:
            v = _field_value(month_agg[0], field)
            if v is not None:
                parts.append(f'month→aggregated: {v / 1000:.3f} kWh')
        if csv_row:
            v = _field_value(csv_row, field)
            if v is not None:
                parts.append(f'CSV: {v / 1000:.3f} kWh')
        if parts:
            print(f'  {field}:')
            for part in parts:
                print(f'    {part}')


if __name__ == '__main__':
    main()
