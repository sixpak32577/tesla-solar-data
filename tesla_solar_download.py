"""
Copyright 2023 Ziga Mahkovec

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import argparse
import csv
import os
import time
import traceback
from datetime import datetime, timedelta

import pytz
import teslapy
from dateutil.parser import parse
from retry import retry

from aggregate_energy_daily import aggregate_energy_rows

# Exclude columns that are not relevant (and generally not set).
EXCLUDED_COLUMNS = (
    'grid_services_power',
    'generator_power',
    'generator_energy_exported',
    'grid_services_energy_imported',
    'grid_services_energy_exported',
    'grid_energy_exported_from_generator',
    'battery_energy_imported_from_generator',
    'consumer_energy_imported_from_generator',
)


def _remove_excluded_columns(timeseries):
    for col in EXCLUDED_COLUMNS:
        if col in timeseries:
            del timeseries[col]


def _get_energy_csv_name(date, site_id, partial_month=False):
    str_date = date.strftime('%Y-%m')
    suffix = '.partial.csv' if partial_month else '.csv'
    return f'download/{site_id}/energy/{str_date}{suffix}'


def _get_fieldnames_from_series(timeseries):
    keys = dict()
    for series in timeseries:
        for k in series.keys():
            keys[k] = True
    return list(keys.keys())


def _write_energy_csv(timeseries, date, site_id, partial_month=False):
    if not timeseries:
        raise ValueError('No timeseries')

    timeseries = aggregate_energy_rows(timeseries)
    csv_filename = _get_energy_csv_name(date, site_id, partial_month=partial_month)
    os.makedirs(os.path.dirname(csv_filename), exist_ok=True)
    fieldnames = _get_fieldnames_from_series(timeseries)
    fieldnames = [n for n in fieldnames if n not in EXCLUDED_COLUMNS]
    with open(csv_filename, 'w') as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        for ts in timeseries:
            _remove_excluded_columns(ts)
            writer.writerow(ts)


@retry(tries=2, delay=5)
def _download_energy_day(tesla, site_id, timezone, date):
    """Fetch a single day's energy total using period=day (no interval).

    Returns the time_series list from the response (typically one row).
    """
    tz = pytz.timezone(timezone)
    end_date = tz.localize(date.replace(hour=23, minute=59, second=59, tzinfo=None))
    response = tesla.api(
        'CALENDAR_HISTORY_DATA',
        path_vars={'site_id': site_id},
        kind='energy',
        period='day',
        end_date=end_date.isoformat(),
        time_zone=timezone,
    )['response']

    if not response or 'time_series' not in response:
        raise ValueError(f'No timeseries for {date.date()}')
    return response['time_series']


def _download_energy_month(
    tesla, site_id, timezone, start_date, end_date, partial_month=False
):
    """Fetch each calendar day in [start_date, end_date] individually using
    period=day, then write all rows to a single monthly CSV.
    """
    tz = pytz.timezone(timezone)
    rows = []
    day = tz.localize(start_date.replace(hour=0, minute=0, second=0, tzinfo=None))

    total_days = (end_date.date() - start_date.date()).days + 1
    fetched = 0
    while day.date() <= end_date.date():
        fetched += 1
        print(
            f'    day {fetched}/{total_days} ({day.date()})',
            end='\r',
            flush=True,
        )
        try:
            day_rows = _download_energy_day(tesla, site_id, timezone, day)
            rows.extend(day_rows)
        except Exception:
            traceback.print_exc()
        day = tz.localize((day + timedelta(days=1)).replace(tzinfo=None))
        time.sleep(1)
    print(' ' * 40, end='\r')  # clear the progress line

    if not rows:
        raise ValueError(f'No data for month starting {start_date.date()}')
    _write_energy_csv(rows, start_date, site_id, partial_month=partial_month)


def _get_timezone(site_config, installation_date):
    if 'installation_time_zone' in site_config:
        return site_config['installation_time_zone']
    offset = installation_date.strftime('%z')
    for tz in pytz.country_timezones('us'):
        if datetime.now(pytz.timezone(tz)).strftime('%z') == offset:
            return tz
    for tz in pytz.common_timezones:
        if datetime.now(pytz.timezone(tz)).strftime('%z') == offset:
            return tz
    for tz in pytz.all_timezones:
        if datetime.now(pytz.timezone(tz)).strftime('%z') == offset:
            return tz


def _download_energy_data(tesla, site_id, start=None, end=None, debug=False):
    site_config = tesla.api('SITE_CONFIG', path_vars={'site_id': site_id})['response']
    installation_date = parse(site_config['installation_date'])
    timezone = _get_timezone(site_config, installation_date)
    tz = pytz.timezone(timezone)

    now = datetime.now(tz).replace(microsecond=0)

    # end defaults to end of today; honour --end-date if provided
    if end:
        end_date = tz.localize(
            datetime(end.year, end.month, end.day, 23, 59, 59)
        )
    else:
        end_date = now.replace(hour=23, minute=59, second=59)

    # Always start at the beginning of the end month and walk backward month by
    # month. --start-date only controls where the loop stops (see `earliest`).
    start_date = end_date.replace(hour=0, minute=0, second=0)
    start_date = start_date - timedelta(days=start_date.day - 1)

    if debug:
        print(f'Timezone: {timezone}')
        print(f'Start date: {start_date}')
        print(f'End date: {end_date}')

    # The first (most recent) month fetched may be partial.
    partial_month = end_date.date() >= now.date()

    # Stop at whichever is later: installation date or the requested start.
    earliest = tz.localize(
        datetime(start.year, start.month, 1, 0, 0, 0)
    ) if start else installation_date

    while end_date > earliest:
        csv_name = _get_energy_csv_name(start_date, site_id)
        if partial_month or not os.path.exists(
            _get_energy_csv_name(start_date, site_id)
        ):
            print(f'  {os.path.basename(csv_name)}')
            try:
                _download_energy_month(
                    tesla,
                    site_id,
                    timezone,
                    start_date,
                    end_date,
                    partial_month=partial_month,
                )
            except Exception:
                traceback.print_exc()
        partial_month = False
        end_date = start_date - timedelta(seconds=1)
        start_date = end_date.replace(hour=0, minute=0, second=0) - timedelta(
            days=end_date.day - 1
        )
        start_date = pytz.timezone(timezone).localize(start_date.replace(tzinfo=None))


def _delete_partial_energy_files(site_id):
    dir = os.path.join('download', str(site_id), 'energy')
    if not os.path.exists(dir):
        return
    for fname in os.listdir(dir):
        if '.partial.csv' in fname:
            os.remove(os.path.join(dir, fname))


def _get_power_csv_name(date, site_id, partial_day=False):
    str_date = date.strftime('%Y-%m-%d')
    suffix = '.partial.csv' if partial_day else '.csv'
    return f'download/{site_id}/power/{str_date}{suffix}'


def _get_soe_csv_name(date, site_id, partial_day=False):
    str_date = date.strftime('%Y-%m-%d')
    suffix = '.partial.csv' if partial_day else '.csv'
    return f'download/{site_id}/soe/{str_date}{suffix}'


def _write_power_csv(timeseries, date, site_id, partial_day=False):
    if not timeseries:
        raise ValueError(f'No timeseries for {date}')

    csv_filename = _get_power_csv_name(date, site_id, partial_day=partial_day)
    os.makedirs(os.path.dirname(csv_filename), exist_ok=True)
    fieldnames = _get_fieldnames_from_series(timeseries) + ['load_power']
    fieldnames = [n for n in fieldnames if n not in EXCLUDED_COLUMNS]
    with open(csv_filename, 'w') as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        for ts in timeseries:
            ts['timestamp'] = parse(ts['timestamp']).strftime('%Y-%m-%d %H:%M:%S')
            ts['load_power'] = (
                ts['solar_power']
                + ts['battery_power']
                + ts['grid_power']
                + ts['generator_power']
            )
            _remove_excluded_columns(ts)
            writer.writerow(ts)


def _write_soe_csv(timeseries, date, site_id, partial_day=False):
    if not timeseries:
        raise ValueError(f'No timeseries for {date}')

    csv_filename = _get_soe_csv_name(date, site_id, partial_day=partial_day)
    os.makedirs(os.path.dirname(csv_filename), exist_ok=True)
    fieldnames = _get_fieldnames_from_series(timeseries)
    fieldnames = [n for n in fieldnames if n not in EXCLUDED_COLUMNS]
    with open(csv_filename, 'w') as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        for ts in timeseries:
            ts['timestamp'] = parse(ts['timestamp']).strftime('%Y-%m-%d %H:%M:%S')
            _remove_excluded_columns(ts)
            writer.writerow(ts)


@retry(tries=2, delay=5)
def _download_power_day(tesla, site_id, timezone, date, partial_day=True):
    start_date = (
        pytz.timezone(timezone)
        .localize(date.replace(hour=0, minute=0, second=0, tzinfo=None))
        .isoformat()
    )
    end_date = (
        pytz.timezone(timezone)
        .localize(date.replace(hour=23, minute=59, second=59, tzinfo=None))
        .isoformat()
    )
    response = tesla.api(
        'CALENDAR_HISTORY_DATA',
        path_vars={'site_id': site_id},
        kind='power',
        period='day',
        start_date=start_date,
        end_date=end_date,
        time_zone=timezone,
    )['response']

    if not response or 'time_series' not in response:
        raise ValueError(f'No timeseries for {date}')
    _write_power_csv(response['time_series'], date, site_id, partial_day=partial_day)


@retry(tries=2, delay=5)
def _download_soe_day(tesla, site_id, timezone, date, partial_day=True):
    start_date = (
        pytz.timezone(timezone)
        .localize(date.replace(hour=0, minute=0, second=0, tzinfo=None))
        .isoformat()
    )
    end_date = (
        pytz.timezone(timezone)
        .localize(date.replace(hour=23, minute=59, second=59, tzinfo=None))
        .isoformat()
    )
    response = tesla.api(
        'CALENDAR_HISTORY_DATA',
        path_vars={'site_id': site_id},
        kind='soe',
        period='day',
        start_date=start_date,
        end_date=end_date,
        time_zone=timezone,
    )['response']

    if response and 'time_series' in response:
        _write_soe_csv(response['time_series'], date, site_id, partial_day=partial_day)


def _download_power_data(tesla, site_id, start=None, end=None, debug=False):
    site_config = tesla.api('SITE_CONFIG', path_vars={'site_id': site_id})['response']
    installation_date = parse(site_config['installation_date'])
    timezone = _get_timezone(site_config, installation_date)
    tz = pytz.timezone(timezone)

    now = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)

    date = tz.localize(
        datetime(end.year, end.month, end.day, 0, 0, 0)
    ) if end else now

    earliest = tz.localize(
        datetime(start.year, start.month, start.day, 0, 0, 0)
    ) if start else installation_date

    if debug:
        print(f'Timezone: {timezone}')
        print(f'Start date: {date}')

    # The first day is partial only when it is today.
    partial_day = date.date() >= now.date()

    while date > earliest:
        csv_name = _get_power_csv_name(date, site_id)
        if partial_day or not os.path.exists(csv_name):
            print(f'  {os.path.basename(csv_name)}')
            try:
                _download_power_day(tesla, site_id, timezone, date, partial_day=partial_day)
                _download_soe_day(tesla, site_id, timezone, date, partial_day=partial_day)
            except Exception:
                traceback.print_exc()
            time.sleep(1)
        date -= timedelta(days=1)
        partial_day = False
        # Re-localize the date based on the timezone.  This is important because we maybe have
        # crossed a daylight saving change so the timezone offset will be different.
        date = pytz.timezone(timezone).localize(date.replace(tzinfo=None))


def _delete_partial_power_files(site_id):
    dir = os.path.join('download', str(site_id), 'power')
    if not os.path.exists(dir):
        return
    for fname in os.listdir(dir):
        if '.partial.csv' in fname:
            os.remove(os.path.join(dir, fname))


def _delete_partial_soe_files(site_id):
    dir = os.path.join('download', str(site_id), 'soe')
    if not os.path.exists(dir):
        return
    for fname in os.listdir(dir):
        if '.partial.csv' in fname:
            os.remove(os.path.join(dir, fname))


def main():
    parser = argparse.ArgumentParser(
        description='Download Tesla Solar/Powerwall power data'
    )
    parser.add_argument(
        '--email', type=str, required=True, help='Tesla account email address'
    )
    parser.add_argument('--debug', action='store_true', help='Print debug info')
    parser.add_argument(
        '--energy-only',
        action='store_true',
        help='Download only monthly energy data (skip power and battery SoE)',
    )
    parser.add_argument(
        '--start-date',
        type=lambda s: parse(s).date(),
        metavar='YYYY-MM-DD',
        help='Earliest date to download (default: installation date)',
    )
    parser.add_argument(
        '--end-date',
        type=lambda s: parse(s).date(),
        metavar='YYYY-MM-DD',
        help='Latest date to download (default: today)',
    )
    parser.add_argument(
        '--client-id',
        type=str,
        metavar='CLIENT_ID',
        help=(
            'Fleet API client ID from developer.tesla.com. Required if the '
            'Owner API returns 403. Register a free personal app at '
            'https://developer.tesla.com with scope energy_device_data and '
            'redirect URI https://auth.tesla.com/void/callback.'
        ),
    )
    parser.add_argument(
        '--region',
        type=str,
        default='na',
        choices=['na', 'eu', 'cn'],
        help='Fleet API region: na (North America/APAC), eu (Europe/Middle East/Africa), cn (China). Default: na',
    )
    args = parser.parse_args()

    fleet_api_hosts = {
        'na': 'https://fleet-api.prd.na.vn.cloud.tesla.com/',
        'eu': 'https://fleet-api.prd.eu.vn.cloud.tesla.com/',
        'cn': 'https://fleet-api.prd.cn.vn.cloud.tesla.cn/',
    }

    # Tesla() always passes client_id=SSO_CLIENT_ID to OAuth2Session in its
    # super().__init__(), so we must NOT pass client_id to the constructor.
    # Instead we patch the session attributes after construction.
    tesla = teslapy.Tesla(args.email, retry=2, timeout=10)

    if args.client_id:
        # Fleet API mode: override the OAuth client_id and point all API calls
        # at the Fleet API regional host instead of owner-api.teslamotors.com.
        # Delete cache.json before switching from an Owner API token.
        tesla.client_id = args.client_id
        tesla.auto_refresh_kwargs = {'client_id': args.client_id}
        teslapy.BASE_URL = fleet_api_hosts[args.region]
        tesla.redirect_uri = 'https://auth.tesla.com/void/callback'
        tesla.scope = ('openid', 'email', 'offline_access', 'energy_device_data')
    else:
        # Legacy Owner API mode (may return 403 if Tesla has cut off the account).
        # Tesla deprecated the https://auth.tesla.com/void/callback redirect URI;
        # the Tesla app's tesla://auth/callback is the only redirect still
        # registered for the ownerapi client_id.
        tesla.redirect_uri = 'tesla://auth/callback'

    if not tesla.authorized:
        print('STEP 1: Log in to Tesla.  Open this page in your browser:\n')
        print(tesla.authorization_url())
        print()
        if args.client_id:
            print(
                'After logging in, you will be redirected to '
                'https://auth.tesla.com/void/callback?code=...  Copy the full URL '
                'from the browser address bar and paste it below.'
            )
        else:
            print(
                'After successful login, you will see a "Verified Successfully" page.  Most '
                'browsers will not navigate to the tesla://auth/callback URL, so you need to '
                'copy it from the browser\'s developer console:\n'
                '  1. Open the developer tools and switch to the Console tab.\n'
                '     (Chrome: View > Developer > Developer Tools)\n'
                '  2. Find the message "Failed to launch \'tesla://auth/callback?...\'" or similar.\n'
                '  3. Right-click the tesla://auth/callback?... URL in that message and choose '
                '"Copy link address" (or your browser\'s equivalent).\n'
                'The URL should look like: tesla://auth/callback?code=NA_abcd12345...&issuer=...'
            )
        print('\nPaste the URL here:')
        auth_response = input('URL after authentication: ')
        if not args.client_id:
            # oauthlib refuses to parse non-https authorization responses. Only
            # the code/state query params are read from this URL, so rewriting
            # the scheme is safe; the redirect_uri sent to the token endpoint
            # still comes from tesla.redirect_uri.
            auth_response = auth_response.replace(
                'tesla://auth/callback', 'https://auth.tesla.com/void/callback', 1
            )
        tesla.fetch_token(authorization_response=auth_response)
        print('\nSuccess!')

    for product in tesla.api('PRODUCT_LIST')['response']:
        resource_type = product.get('resource_type')
        if resource_type in ('battery', 'solar'):
            site_id = product['energy_site_id']
            obfuscated_site_it = f'***{str(site_id)[-4:]}'
            print(
                f'Downloading energy data for {resource_type} site {obfuscated_site_it} to download/energy/'
            )
            try:
                _delete_partial_energy_files(site_id)
                _download_energy_data(
                    tesla, site_id,
                    start=args.start_date, end=args.end_date,
                    debug=args.debug,
                )
            except Exception:
                traceback.print_exc()
            print()

            if not args.energy_only:
                print(
                    f'Downloading power data for {resource_type} site {obfuscated_site_it} to download/power/'
                )
                try:
                    _delete_partial_power_files(site_id)
                    _delete_partial_soe_files(site_id)
                    _download_power_data(
                        tesla, site_id,
                        start=args.start_date, end=args.end_date,
                        debug=args.debug,
                    )
                except Exception:
                    traceback.print_exc()


if __name__ == '__main__':
    main()
