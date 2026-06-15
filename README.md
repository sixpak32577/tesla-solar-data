# Tesla Solar/Powerwall Data Downloader

## Introduction

This script will download your entire history of Tesla Solar power and energy data:
solar/battery/grid power data in 5 minute intervals, battery state of charge in 15 minute intervals,
and daily totals for solar/home/battery/grid energy.

The script is using the [unofficial Tesla API](https://tesla-api.timdorr.com/)
and [TeslaPy](https://github.com/tdorssers/TeslaPy) library.  Data is stored in CSV files: one file per
day for power, and one file per month for energy.  You can run the script repeatedly and it will only
download new data.

Note: if you're not comfortable running Python code and want better data exports from your Tesla solar/battery system,
consider the [Netzero app](https://www.netzero.energy).

## Installation

1. If needed, install Python 3.10+ and git.
2. Clone the repo:
    ```bash
    git clone https://github.com/netzero-labs/tesla-solar-download.git
    cd tesla-solar-download
    ```
3. Create a virtual environment and install the dependencies:
    ```bash
    python3 -m venv venv
    source venv/bin/activate
    pip3 install --upgrade pip
    pip3 install -r requirements.txt
    ```

Note: On Windows, run `venv\Scripts\activate` instead of `source venv/bin/activate`.

You must activate the virtual environment (`source venv/bin/activate`) in each new
terminal session before running any of the scripts below.

## Authentication (Tesla Fleet API)

Tesla has deprecated the legacy Owner API. If you run the downloader without a Fleet API
client ID you will likely get a `403 forbidden` error. To fix this, register a free personal
app on the Tesla developer portal and pass its client ID with `--client-id`.

1. Go to [developer.tesla.com](https://developer.tesla.com), sign in, and create an app.
   - Scopes: include `energy_device_data` (read-only energy data).
   - Allowed Origin URL: a domain you control (e.g. a GitHub Pages site like
     `https://yourusername.github.io`).
   - Allowed Redirect URI: `https://auth.tesla.com/void/callback`
   - Allowed Returned URL: leave empty.
   - Copy the generated **Client ID** and **Client Secret**.
2. Generate an EC key pair and host the public key on your domain:
    ```bash
    openssl ecparam -name prime256v1 -genkey -noout -out private-key.pem
    openssl ec -in private-key.pem -pubout -out public-key.pem
    ```
   Host `public-key.pem` at exactly:
   `https://<your-domain>/.well-known/appspecific/com.tesla.3p.public-key.pem`
   (On GitHub Pages, add an empty `.nojekyll` file so the `.well-known` directory is served.)
   Keep `private-key.pem` secret; it is git-ignored.
3. Register your app with the Fleet API (one time, per region):
    ```bash
    source venv/bin/activate
    python3 ./register_fleet_app.py \
      --client-id YOUR_CLIENT_ID \
      --client-secret YOUR_CLIENT_SECRET \
      --domain yourusername.github.io
    ```
   Use `--region eu` or `--region cn` if you are outside North America/APAC.

## Usage

Activate the virtual environment first:

```bash
source venv/bin/activate
```

Download all available data (power, energy, and battery state of charge):

```bash
python3 ./tesla_solar_download.py --email my_tesla_email@gmail.com --client-id YOUR_CLIENT_ID
```

Download only daily energy totals (skip 5-minute power and battery SoE):

```bash
python3 ./tesla_solar_download.py --email my_tesla_email@gmail.com --client-id YOUR_CLIENT_ID --energy-only
```

Download a specific date range (inclusive). Defaults: start = installation date,
end = today:

```bash
python3 ./tesla_solar_download.py --email my_tesla_email@gmail.com --client-id YOUR_CLIENT_ID \
  --start-date 2025-01-01 --end-date 2025-12-31 --energy-only
```

Full list of options:

| Option | Description |
| --- | --- |
| `--email` | Tesla account email address (required). |
| `--client-id` | Fleet API client ID from developer.tesla.com (required unless the legacy Owner API still works for you). |
| `--region` | Fleet API region: `na` (default), `eu`, or `cn`. |
| `--energy-only` | Download only energy data; skip power and battery SoE. |
| `--start-date` | Earliest date to download (`YYYY-MM-DD`). Defaults to the installation date. |
| `--end-date` | Latest date to download (`YYYY-MM-DD`). Defaults to today. |
| `--debug` | Print the resolved timezone and date range. |

The first run opens a Tesla login in your browser to generate an API token (credentials are
only sent to Tesla). After login, paste the resulting `https://auth.tesla.com/void/callback?...`
URL back into the terminal. The token is stored in `cache.json` so subsequent runs reuse it.
If you ever get an auth error, delete `cache.json` and run again.

Data downloads to the `download` directory, starting with the most recent month/day and going
back in time. You may interrupt and restart the process -- any CSV files that already exist are
skipped on the next run.

- Power/SoE data: ~1 API call per day (~1.5 seconds/day due to rate-limiting delays).
- Energy data: also ~1 API call per day (uses `period=day` for accuracy that matches the
  Tesla app), so expect roughly 6-9 minutes per year of energy history. A live `day N/total`
  progress counter is shown while each month downloads.

### Aggregate existing energy CSVs to one row per day

```bash
source venv/bin/activate
python3 ./aggregate_energy_daily.py download/<site_id>/energy/
```

Pass one or more files, directories, or globs. Files are overwritten in place unless `-o` is used with a single input file.

### Compare the Tesla app with API data for a single day

```bash
source venv/bin/activate
python3 ./compare_energy_day.py --email my_tesla_email@gmail.com --date 2026-05-20
```

Use `--site-id` if you have more than one energy site. Values are shown in Wh and kWh (`kWh = Wh / 1000`).

### Import a CSV into PostgreSQL

Load a downloaded CSV file into a local PostgreSQL database. The table schema is derived from
the CSV header, so it works for energy, power, and soe files. Rows are upserted on `timestamp`,
so re-importing the same file is safe.

```bash
source venv/bin/activate
python3 ./import_to_postgres.py download/<site_id>/energy/2025-12.csv
```

Import a whole folder with a shell loop:

```bash
for f in download/<site_id>/energy/*.csv; do
  python3 ./import_to_postgres.py "$f"
done
```

Connection settings default to the standard libpq environment variables (`PGHOST`, `PGPORT`,
`PGDATABASE`, `PGUSER`, `PGPASSWORD`, `PGSCHEMA`) and can be overridden with flags:

| Option | Description |
| --- | --- |
| `--table` | Target table name. Defaults to the data kind from the path (`energy`, `power`, or `soe`). |
| `--schema` | Target schema. Defaults to `public`. Created if it does not exist. |
| `--dbname` | Database name. Defaults to `postgres`. |
| `--host` / `--port` | Server host/port. Default `localhost:5432`. |
| `--user` / `--password` | Credentials (default to libpq env vars / current user). |
| `--dsn` | Full libpq connection string, e.g. `postgresql://user:pass@host:5432/dbname` (overrides the other connection flags). |

Example using a custom schema and database:

```bash
python3 ./import_to_postgres.py download/<site_id>/energy/2025-12.csv --dbname tesla --schema tesla_data
```


## Data

Power data is formatted as follows:
`download/<site_id>/power/2022-07-19.csv`
```CSV
timestamp,solar_power,battery_power,grid_power,load_power
[...]
2023-07-19 10:40:00,7506.428571428572,-7401.224489795918,612.5714285714286,717.775510204082
2023-07-19 10:45:00,7576.836734693878,-3342.0408163265306,-3555.030612244898,679.7653061224487
2023-07-19 10:50:00,7616.666666666667,-3466.6666666666665,-3544.4,605.5999999999999
[...]
```

- One CSV file per day.
- Every file starts at midnight and ends at 11.55pm, in 5 minute increments.
- All power values are in Watts. Note: to get Watt-hour energy values for the 5-minute interval, divide the value by 12. You can then add up all the values and divide by 1000 for the daily kWh total.
- load_power is simply a sum of solar+battery+grid+generator power and is what is shown as "house" load in the Tesla app.  (Note: this value is not included in API responses since it can be easily derived.)

Energy data:
`download/<site_id>/energy/2022-07.csv`

- One CSV file per month.
- Rows are aggregated to one row per calendar day (sub-daily API intervals are summed). The `timestamp` column is a date only (`YYYY-MM-DD`).
- Energy values are in watt-hours (Wh).

```CSV
timestamp,solar_energy_exported,grid_energy_imported,grid_energy_exported_from_solar,grid_energy_exported_from_battery,battery_energy_exported,battery_energy_imported_from_grid,battery_energy_imported_from_solar,consumer_energy_imported_from_grid,consumer_energy_imported_from_solar,consumer_energy_imported_from_battery
2023-07-01,66700,6493.5,43456,0,16760,249.5,15640.5,6244,7603.5,16760
2023-07-02,66780,6353,40874,0,14060,260,18510,6093,7396,14060
2023-07-03,67380,6282,45964.5,0,10030,230,15580,6052,5835.5,10030
[...]
```

Powerwall state of charge data:
`download/<site_id>/soe/2022-07-19.csv`
```CSV
timestamp,soe
2024-07-19 00:00:00,44
2024-07-19 00:15:00,43
2024-07-19 00:30:00,43
[...]
```
