"""
Aggregate Tesla energy CSV files to one row per calendar day.

Sub-daily intervals are summed; the timestamp column is set to YYYY-MM-DD (no time).
"""

import argparse
import csv
import glob
import os
import sys

from dateutil.parser import parse


def aggregate_energy_rows(rows):
    """Sum sub-daily energy intervals into one row per calendar day."""
    by_date = {}
    for row in rows:
        if 'timestamp' not in row:
            raise ValueError('Row missing timestamp column')
        day = parse(row['timestamp']).date()
        if day not in by_date:
            by_date[day] = dict(row)
            continue
        agg = by_date[day]
        for key, value in row.items():
            if key == 'timestamp':
                continue
            if key not in agg:
                agg[key] = value
                continue
            try:
                agg[key] = float(agg[key]) + float(value)
            except (TypeError, ValueError):
                pass
    return [
        {**row, 'timestamp': day.isoformat()}
        for day, row in sorted(by_date.items())
    ]


def _fieldnames_from_rows(rows):
    keys = {}
    for row in rows:
        for key in row:
            keys[key] = True
    fieldnames = list(keys.keys())
    if 'timestamp' in fieldnames:
        fieldnames.remove('timestamp')
        fieldnames.insert(0, 'timestamp')
    return fieldnames


def aggregate_energy_csv(csv_path, output_path=None):
    with open(csv_path, newline='') as csv_file:
        rows = list(csv.DictReader(csv_file))
    if not rows:
        print(f'  (empty) {csv_path}')
        return
    aggregated = aggregate_energy_rows(rows)
    out_path = output_path or csv_path
    fieldnames = _fieldnames_from_rows(aggregated)
    with open(out_path, 'w', newline='') as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(aggregated)
    print(f'  {os.path.basename(out_path)} ({len(aggregated)} days)')


def _expand_paths(paths):
    files = []
    for path in paths:
        if os.path.isdir(path):
            for name in os.listdir(path):
                if name.endswith('.csv'):
                    files.append(os.path.join(path, name))
        elif os.path.isfile(path):
            files.append(path)
        else:
            files.extend(glob.glob(path))
    return sorted(set(files))


def main():
    parser = argparse.ArgumentParser(
        description='Aggregate Tesla energy CSVs to one row per day (date-only timestamps)'
    )
    parser.add_argument(
        'paths',
        nargs='+',
        help='Energy CSV file(s), directory(ies), or glob pattern(s)',
    )
    parser.add_argument(
        '-o',
        '--output',
        help='Write a single input file here instead of overwriting (one file only)',
    )
    args = parser.parse_args()

    files = _expand_paths(args.paths)
    if not files:
        print('No CSV files found.', file=sys.stderr)
        sys.exit(1)
    if args.output and len(files) != 1:
        print('--output requires exactly one input file.', file=sys.stderr)
        sys.exit(1)

    for csv_path in files:
        aggregate_energy_csv(csv_path, output_path=args.output)


if __name__ == '__main__':
    main()
