"""
Import a downloaded Tesla CSV file into a local PostgreSQL database.

The table schema is derived from the CSV header, so this works for energy,
power, and soe files (including any extra columns). Rows are upserted on
(site_id, timestamp), so re-importing the same file is safe.

Connection settings come from standard libpq environment variables
(PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD) and can be overridden with
CLI flags or a full --dsn.

Usage:
  python3 import_to_postgres.py download/<site_id>/energy/2025-12.csv
  python3 import_to_postgres.py download/<site_id>/energy/2025-12.csv --dbname tesla
  python3 import_to_postgres.py path/to/file.csv --table energy --site-id 123
"""

import argparse
import csv
import os
import sys

import psycopg2
from dateutil.parser import parse as parse_dt
from psycopg2 import sql
from psycopg2.extras import execute_values

# Columns that need a non-numeric type. Everything else is DOUBLE PRECISION.
TIMESTAMP_COLUMNS = ('timestamp', 'raw_timestamp')


def _kind_from_path(path):
    """Guess the data kind (energy/power/soe) from the path; default 'energy'."""
    parts = path.replace(os.sep, '/').split('/')
    for kind in ('energy', 'power', 'soe'):
        if kind in parts:
            return kind
    return 'energy'


def _has_time_component(value):
    """True if the value parses to a datetime with a non-midnight time or tz."""
    try:
        dt = parse_dt(value)
    except (ValueError, TypeError):
        return False
    return (
        dt.hour or dt.minute or dt.second or dt.tzinfo is not None
        or 'T' in value or ':' in value
    )


def _column_type(name, sample):
    if name == 'timestamp':
        return 'TIMESTAMPTZ' if _has_time_component(sample) else 'DATE'
    if name in TIMESTAMP_COLUMNS:
        return 'TIMESTAMPTZ'
    return 'DOUBLE PRECISION'


def _read_csv(path):
    with open(path, newline='') as csv_file:
        reader = csv.DictReader(csv_file)
        fieldnames = reader.fieldnames or []
        rows = list(reader)
    if 'timestamp' not in fieldnames:
        raise ValueError(f'{path} has no "timestamp" column')
    return fieldnames, rows


def _coerce(name, value):
    if value is None or value == '':
        return None
    if name in TIMESTAMP_COLUMNS:
        return value
    try:
        return float(value)
    except ValueError:
        return value


def _ensure_schema(cur, schema):
    cur.execute(
        sql.SQL('CREATE SCHEMA IF NOT EXISTS {}').format(sql.Identifier(schema))
    )


def _ensure_table(cur, schema, table, fieldnames, sample_row):
    columns = []
    for name in fieldnames:
        col_type = _column_type(name, sample_row.get(name, ''))
        columns.append(
            sql.SQL('{} {}').format(sql.Identifier(name), sql.SQL(col_type))
        )
    columns.append(sql.SQL('PRIMARY KEY ({})').format(sql.Identifier('timestamp')))
    cur.execute(
        sql.SQL('CREATE TABLE IF NOT EXISTS {} ({})').format(
            sql.Identifier(schema, table), sql.SQL(', ').join(columns)
        )
    )


def _upsert(cur, schema, table, fieldnames, rows):
    insert_cols = sql.SQL(', ').join(sql.Identifier(c) for c in fieldnames)
    update_cols = [c for c in fieldnames if c != 'timestamp']
    set_clause = sql.SQL(', ').join(
        sql.SQL('{0} = EXCLUDED.{0}').format(sql.Identifier(c))
        for c in update_cols
    )
    statement = sql.SQL(
        'INSERT INTO {table} ({cols}) VALUES %s '
        'ON CONFLICT ({ts}) DO UPDATE SET {sets}'
    ).format(
        table=sql.Identifier(schema, table),
        cols=insert_cols,
        ts=sql.Identifier('timestamp'),
        sets=set_clause,
    )
    values = [
        tuple(_coerce(c, row.get(c)) for c in fieldnames)
        for row in rows
    ]
    execute_values(cur, statement, values)


def main():
    parser = argparse.ArgumentParser(
        description='Import a Tesla CSV file into a local PostgreSQL database'
    )
    parser.add_argument('csv_file', help='Path to the CSV file to import')
    parser.add_argument(
        '--table',
        help='Target table name (default: data kind from path, e.g. "energy")',
    )
    parser.add_argument('--dsn', help='Full libpq connection string (overrides other connection flags)')
    parser.add_argument('--host', default=os.environ.get('PGHOST', 'localhost'))
    parser.add_argument('--port', default=os.environ.get('PGPORT', '5432'))
    parser.add_argument('--dbname', default=os.environ.get('PGDATABASE', 'postgres'))
    parser.add_argument('--user', default=os.environ.get('PGUSER'))
    parser.add_argument('--password', default=os.environ.get('PGPASSWORD'))
    parser.add_argument(
        '--schema',
        default=os.environ.get('PGSCHEMA', 'public'),
        help='Target schema (default: public). Created if it does not exist.',
    )
    args = parser.parse_args()

    if not os.path.isfile(args.csv_file):
        print(f'File not found: {args.csv_file}', file=sys.stderr)
        sys.exit(1)

    table = args.table or _kind_from_path(args.csv_file)
    fieldnames, rows = _read_csv(args.csv_file)
    if not rows:
        print(f'{args.csv_file} has no data rows; nothing to import.')
        return

    if args.dsn:
        conn = psycopg2.connect(args.dsn)
    else:
        conn = psycopg2.connect(
            host=args.host,
            port=args.port,
            dbname=args.dbname,
            user=args.user,
            password=args.password,
        )

    try:
        with conn:
            with conn.cursor() as cur:
                _ensure_schema(cur, args.schema)
                _ensure_table(cur, args.schema, table, fieldnames, rows[0])
                _upsert(cur, args.schema, table, fieldnames, rows)
    finally:
        conn.close()

    print(
        f'Imported {len(rows)} row(s) from {os.path.basename(args.csv_file)} '
        f'into table "{args.schema}.{table}".'
    )


if __name__ == '__main__':
    main()
