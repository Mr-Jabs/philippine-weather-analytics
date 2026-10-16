# Philippine Weather Analytics

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-14%2B-4169E1?logo=postgresql&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-API-009688?logo=fastapi&logoColor=white)
![JavaScript](https://img.shields.io/badge/JavaScript-Vanilla-F7DF1E?logo=javascript&logoColor=black)
![CSS](https://img.shields.io/badge/CSS-Responsive-663399?logo=css&logoColor=white)
![GitHub Actions](https://img.shields.io/badge/GitHub_Actions-Daily_ETL-2088FF?logo=githubactions&logoColor=white)

An end-to-end data engineering portfolio project that extracts hourly Philippine
weather forecasts, validates and transforms them in Python, loads PostgreSQL,
and serves analytical views through a FastAPI API and responsive web dashboard.

The project covers six cities: **Manila, Quezon City, Davao, Cebu, Baguio, and
Cagayan de Oro**. The database city dimension controls ingestion targets and
dashboard selectors; cities are not duplicated in the production frontend.

## Key features

- API extraction with timeouts, rate-limit handling, exponential retries, and raw JSON retention.
- Timezone-aware transformations into Philippine Standard Time (`Asia/Manila`, UTC+8).
- Explicit data types, invalid-value cleaning, null preservation, and city/hour deduplication.
- Engineered heat index and daily rainfall totals with coverage indicators.
- Normalized city dimension and hourly fact tables with transactional bulk UPSERTs.
- Four analytical REST endpoints with Pydantic response models and SQL aggregations.
- Responsive city selectors, metric cards, hourly charts, comparison bars, and retry/empty/offline states.
- Daily scheduled ETL and manual runs through GitHub Actions with repository secrets and diagnostic artifacts.

## Architecture

```mermaid
flowchart LR
    A["Open-Meteo API"] --> B["Python ETL"]
    B --> C[("PostgreSQL")]
    C --> D["FastAPI"]
    D --> E["Web Dashboard"]
    G["GitHub Actions<br/>Daily / Manual"] --> B
    B --> R["Raw JSON<br/>data/raw/"]
    B --> P["Processed CSVs<br/>data/processed/"]
```

`src.main` runs extraction → transformation → loading in order. An extraction
failure retains successful raw responses but stops downstream publication.
Every load uses one database transaction across all batches.

| Layer | Implementation |
| --- | --- |
| Source | Open-Meteo forecast REST API |
| Extraction | Python, Requests, psycopg 3 city lookup |
| Transformation | Pandas, timezone conversion, rainfall/heat-index features |
| Storage | PostgreSQL, identity keys, foreign key, composite unique index |
| Loading | SQLAlchemy + psycopg 3, batches of 1,000 rows |
| API | FastAPI, Pydantic, SQLAlchemy connection pooling, Uvicorn |
| Dashboard | Semantic HTML, vanilla JavaScript, custom CSS, native SVG charts |
| Automation | GitHub Actions, daily cron, manual dispatch, run artifacts |
| Tests | unittest / optional pytest, FastAPI TestClient, SQLite fixtures, Node test runner |

The frontend uses **custom CSS**, not Tailwind, and needs no frontend build step
or chart CDN.

## Local quickstart

Run commands from the repository root. Prerequisites: Python 3.10+, PostgreSQL
14+ with `psql` / `createdb` available, and internet access to Open-Meteo.
Node.js 20+ is optional for frontend tests. The examples use PowerShell.

### 1. Install Python dependencies

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` with your database username/password and connection details.
This is a configuration template: the Python modules **do not automatically load
dotenv files**. Export its `DATABASE_URL` line into the current terminal:

```powershell
$env:DATABASE_URL = (Get-Content .env | Where-Object { $_ -match '^DATABASE_URL=' } | Select-Object -First 1) -replace '^DATABASE_URL=', ''
```

For Bash, use `export DATABASE_URL='postgresql://USER:PASSWORD@localhost:5432/weather_analytics'`
and replace `.\.venv\Scripts\python` below with `.venv/bin/python`.

Keep `DATABASE_URL` on one unquoted line in `.env` when using the PowerShell
import above. Percent-encode reserved URL characters in credentials
(`@` → `%40`, `:` → `%3A`, `/` → `%2F`). A remote provider may also
require `?sslmode=require` or additional certificate settings. Never commit
credentials; `.env`, dotenv variants, and generated data are ignored by Git.

### 2. Create and initialize the database

For the local `postgres` role shown in `.env.example`:

```powershell
createdb -h localhost -p 5432 -U postgres weather_analytics
psql --dbname "$env:DATABASE_URL" --set ON_ERROR_STOP=1 --file sql/schema.sql
```

Create the database only once; use your own administration role/host if different.
The DDL creates both tables and seeds the six cities. Reapplying it preserves
city IDs and updates seeds by name. It initializes this schema; it is not an
upgrade migration for incompatible existing tables.

### 3. Run the ETL

```powershell
.\.venv\Scripts\python -m src.main
```

This defaults to a seven-day forecast. To run extraction only, or replay saved
raw data through transformation:

```powershell
.\.venv\Scripts\python -m src.ingestion --forecast-days 7
.\.venv\Scripts\python -m src.transform
```

Extraction alone does not populate facts; the full `src.main` command does.
`--forecast-days` accepts 1–16. `--raw-dir` and `--processed-dir` select
alternative landing/output paths. A replay transform writes CSVs without loading
the database.

### 4. Start the FastAPI backend

In a terminal with `DATABASE_URL` exported:

```powershell
.\.venv\Scripts\python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000 --reload
```

Visit [Swagger UI](http://127.0.0.1:8000/docs) or
[OpenAPI JSON](http://127.0.0.1:8000/openapi.json).

### 5. Open the dashboard

In a second terminal, from the same project root:

```powershell
.\.venv\Scripts\python -m http.server 4173 --bind 127.0.0.1
```

Open [the weather dashboard](http://127.0.0.1:4173/). The frontend defaults to
`https://philippine-weather-analytics.onrender.com/api/v1/`. To use the local
backend, set the `weather-api-base` meta tag in `index.html` to
`http://127.0.0.1:8000/api/v1/`. Stop local servers with Ctrl+C.

## Database model

```mermaid
erDiagram
    dim_cities ||--o{ fact_hourly_weather : "has hourly readings"
    dim_cities {
        integer city_id PK
        text city_name UK
        text region
        numeric latitude
        numeric longitude
    }
    fact_hourly_weather {
        bigint id PK
        integer city_id FK
        timestamptz timestamp_ph
        double temperature_c
        double humidity_pct
        double precipitation_mm
        double wind_speed_kmh
    }
```

The unique index on `(city_id, timestamp_ph)` supports
`ON CONFLICT (city_id, timestamp_ph) DO UPDATE`. Repeated loads update the same
city/hour instead of inserting duplicates. PostgreSQL stores absolute instants;
use `SET TIME ZONE 'Asia/Manila'` when displaying local timestamps in SQL.

| Fact field | Unit / behavior |
| --- | --- |
| `temperature_c` | Celsius |
| `humidity_pct` | Relative humidity, 0–100% |
| `precipitation_mm` | Hourly precipitation, millimeters |
| `wind_speed_kmh` | Wind speed, km/h |
| `timestamp_ph` | Aware timestamp; APIs serialize with +08:00 |

Measurement columns permit SQL NULLs. Heat index and daily rainfall remain in
processed CSVs; the API calculates hourly heat index from stored temperature and
humidity. These derived features are not additional fact-table columns.

## Data contracts and quality

The [Open-Meteo forecast endpoint](https://open-meteo.com/en/docs) is requested
with `temperature_2m`, `relative_humidity_2m`, `precipitation`, and
`wind_speed_10m`, explicit metric units, and `Asia/Manila` time.

Extraction uses 10/60-second connect/read timeouts and up to five attempts.
Timeouts, connection failures, HTTP 429, and HTTP 500/502/503/504 retry with
exponential backoff plus jitter and respect a longer `Retry-After` header.
Other HTTP errors fail immediately. Raw files include city ID/name, UTC
retrieval timestamp, and UUID; their payload is preserved before normalization.

Transformation applies these rules:

- Respect embedded timestamp offsets and payload timezone metadata; naive times
  without metadata default to UTC. Unix timestamps represent UTC seconds.
- Cast city IDs to int64 and measurements to float64. Replace nonnumeric,
  nonfinite, negative rain/wind, humidity outside 0–100%, and temperatures outside
  -10–55°C with null. The broad temperature range retains cool Baguio conditions.
- Drop invalid/non-hourly timestamps and records with no usable measurements;
  retain partially missing records without inventing values.
- Reject malformed/misaligned hourly arrays and incompatible measurement units.
- Resolve overlapping city/hour rows using the newest retrieval timestamp in the
  filename, falling back to file mtime for other filenames.

Each transform creates a unique run folder containing `hourly_weather.csv` and
`daily_weather.csv`. The hourly heat index uses the
[NWS simple/Rothfusz method](https://www.wpc.ncep.noaa.gov/html/heatindex_equation.shtml)
with humidity adjustments, returning Celsius. It estimates shade/light-wind
conditions; missing temperature/humidity produces a null result.

Daily rainfall groups deduplicated readings by Manila calendar day.
`rainfall_available_mm` is the subtotal of available values.
`daily_rainfall_mm` is populated only with 24 valid precipitation hours;
`rainfall_hours` and `is_complete_day` expose coverage. Missing rainfall is
never interpreted as zero.

## REST API

| Endpoint | Parameters | Response |
| --- | --- | --- |
| `GET /api/v1/cities` | None | City IDs, names, regions, coordinates |
| `GET /api/v1/weather/latest` | Optional `city` | Latest stored reading per city at or before now, including heat index |
| `GET /api/v1/weather/historical` | Optional `city`, `days=7` | Hourly records ordered by city and timestamp |
| `GET /api/v1/weather/comparison` | `days=7`, optional comma-separated `cities` | SQL aggregates and measurement counts per city |

All responses contain `data` and `count`. Weather responses include `as_of`;
historical/comparison also expose `start` and `end`. Dates serialize in PHT.
`days` accepts 1–365 and defaults to a trailing `[now - days, now)` window.
Future forecast hours are excluded from analytics.

Historical queries also accept both `start=YYYY-MM-DD` and `end=YYYY-MM-DD`.
These inclusive Manila dates override `days`; the exclusive next-day midnight
boundary is capped at now. Ranges are ordered, limited to 365 days, and cannot
start in the future. City selectors are trimmed, case-insensitive, and bound as
SQL parameters.

Unknown cities return 404; invalid inputs return 422; missing database
configuration or database errors return 503. Known cities without matching
weather return empty arrays. Comparison retains cities without data with zero
counts and null statistics. Rainfall sums use available values only; compare
measurement counts to assess coverage.

The API only reads the database; it never triggers extraction. Local CORS
permits `*` origins for GET requests with credentials disabled. Latest records
can be old; the dashboard displays their source time and flags readings older
than three hours.

```powershell
Invoke-RestMethod 'http://127.0.0.1:8000/api/v1/cities'
Invoke-RestMethod 'http://127.0.0.1:8000/api/v1/weather/latest?city=Manila'
Invoke-RestMethod 'http://127.0.0.1:8000/api/v1/weather/historical?city=Cebu&days=7'
Invoke-RestMethod 'http://127.0.0.1:8000/api/v1/weather/comparison'
```

## Dashboard behavior

City tabs, the dropdown, and sidebar use the city dimension through the API.
The metric cards show temperature, humidity, rainfall, and heat index.
The hourly chart switches between temperature, rainfall, and heat index, with
a data table. Comparison bars select cities; daily rainfall uses hatching for
partial-day coverage.

Requests have a 12-second timeout. City changes cancel superseded requests and
ignore their late results. Refresh reloads the city list and all weather panels.
Loading indicators, empty states, partial failures, and retry controls handle
unavailable data without fabricated readings. Null readings and missing hours
leave chart gaps.

The seven-day rainfall summary compares valid hours to 168. A rolling seven-day
window can span eight Manila calendar dates. Available-hour subtotals should not
be read as complete-day totals. Desktop, tablet, and mobile layouts use native
SVG charts, keyboard controls, reduced-motion support, and 44px mobile targets.

For a labeled preview without a database:

```powershell
.\.venv\Scripts\python tests/serve_dashboard_fixture.py
```

Open `http://127.0.0.1:4174/`. Test routes `/slow`, `/partial`, `/empty`,
and `/offline` exercise state handling. This separate fixture serves **sample
data**, is never a production fallback, and stops with Ctrl+C.

## GitHub Actions automation

[weather_etl.yml](.github/workflows/weather_etl.yml) runs on
`cron: '0 0 * * *'` (**00:00 UTC / 08:00 PHT daily**) and `workflow_dispatch`.

1. Push this repository, including the workflow, to GitHub's default branch.
2. Initialize a PostgreSQL database with `sql/schema.sql` once.
3. Under **Settings → Secrets and variables → Actions → Repository secrets**,
   add all five values below.
4. Open **Actions → Daily Weather ETL → Run workflow** for the first manual run.
5. Check the run summary, step logs, and uploaded diagnostic artifact.

| Repository secret | Value |
| --- | --- |
| `DB_HOST` | Reachable PostgreSQL hostname, without a URL scheme |
| `DB_PORT` | PostgreSQL port, usually `5432` |
| `DB_USER` | Database login, supplied as raw text |
| `DB_PASSWORD` | Password, supplied as raw text, not percent-encoded |
| `DB_NAME` | Existing database name, e.g. `weather_analytics` |

The workflow uses Python 3.12, installs `requirements.txt`, builds a properly
encoded/masked `DATABASE_URL` from these secrets, and executes
`python -m src.main`. The CI connection requires TLS (`sslmode=require`).
No separate `DATABASE_URL` repository secret is needed. The local dotenv file
is not used by GitHub Actions.

A hosted runner cannot reach PostgreSQL on your laptop's `localhost`. Use a
network-reachable database with the required network access and credentials, or
configure an appropriate runner/network arrangement. The database must already
have this schema and permit city reads plus fact inserts/updates. The workflow
does not create or migrate the remote database.

Runs have a 20-minute limit, read-only repository permissions, and a shared
concurrency group to avoid overlapping database loads. Bash pipe failure handling
preserves ETL failures even when logs are piped through `tee`. Logs, raw JSON,
and processed CSVs are retained as artifacts for seven days, including partial
outputs after a failure; dotenv files and credential files are not uploaded.

A failed run creates a GitHub error annotation and job summary. Configure GitHub
Actions notification preferences to receive failure notifications; no third-party
webhook or email service is required. Nothing commits generated data back to
the repository.

GitHub schedules run from the default branch and may be delayed, especially at
the top of the hour; public-repository schedules can be disabled after 60 days
without activity. See [GitHub's scheduling documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).
The workflow becomes active only after it is pushed and Actions/secrets are
configured; creating the local file does not publish or trigger it.

## Tests

```powershell
.\.venv\Scripts\python -m unittest discover -s tests -v
node --test tests/test_frontend.cjs
```

If you prefer pytest, the unittest-based Python suite is also compatible:

```powershell
.\.venv\Scripts\python -m pip install pytest
.\.venv\Scripts\python -m pytest tests -q
```

ETL tests mock HTTP/database writes. API tests execute the analytical SELECTs
against in-memory SQLite fixtures using FastAPI TestClient. CI configuration
tests check missing secrets, invalid ports, and reserved password characters
without contacting a database. Node tests cover request failures/timeouts,
cancellation, numeric nulls, Manila daily aggregation, chart gaps, and label
escaping.

These checks do not replace live PostgreSQL integration tests, browser visual
QA, or an actual GitHub Actions run with configured secrets.

## Repository guide

```text
.github/workflows/weather_etl.yml   Daily/manual ETL workflow
backend/app.py                     FastAPI routes and response models
scripts/configure_ci_database.py   CI secrets → masked DATABASE_URL
sql/schema.sql                     Dimension/fact DDL and city seeds
src/ingestion.py                   Retry-aware extraction and raw persistence
src/transform.py                   Cleaning and derived metrics
src/load.py                        Transactional bulk UPSERT
src/main.py                        Sequential ETL entry point
data/raw/                          Generated raw payloads (ignored)
data/processed/                    Generated hourly/daily CSVs (ignored)
tests/                             Python, API, and frontend regression checks
index.html                         Dashboard markup and API-base configuration
script.js                          Async API requests and SVG chart rendering
styles.css                         Responsive CSS design system
requirements.txt                   Python dependencies
.env.example                       Safe local connection template
```

## Troubleshooting and scope

| Symptom | Check |
| --- | --- |
| Missing `DATABASE_URL` | Export it in the same terminal that starts Python/Uvicorn; `.env` is not auto-loaded |
| API returns 503 | Database reachability, credentials, schema, and current API process environment |
| Empty historical chart | Run the full ETL; forecasts alone do not backfill seven days of past data |
| Workflow cannot connect | Repository secrets, TLS support, database network access, and initialized schema |
| Dashboard cannot load | API on port 8000, frontend API-base meta tag, and the retry banner |
| `psql` is not found | Add PostgreSQL's `bin` directory to PATH or use its full executable path |
| No diagnostic artifacts | Failure may have happened before extraction/log creation; inspect step logs |

The source provides **model forecasts, not station observations**. The API
labels historical data by timestamp; those rows remain model-derived. A fresh
install will not have a full seven-day historical window. Daily extraction
builds a timeline over time; archive backfilling is not implemented.

UPSERTs keep the latest stored value per city/hour, not every forecast vintage.
Raw/processed local files are not a permanent cloud data lake, and Actions
artifacts have limited retention. Dependency ranges are bounded but not a
reproducible lockfile. The API and wildcard CORS configuration target local
development; authentication and hosted deployment are not implemented.

Data source: [Open-Meteo](https://open-meteo.com/). Review its
[attribution and usage terms](https://open-meteo.com/en/terms) before publishing a
hosted application.
