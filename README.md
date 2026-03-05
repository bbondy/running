# running

Get Strava running distance/elevation totals for an inclusive date range.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
cp .envrc.template .envrc
# fill in STRAVA_* values in .envrc (see auth options below)
direnv allow
```

Auth options:
- `STRAVA_ACCESS_TOKEN` (same style used in `go-brianbondy`)
- Or `STRAVA_CLIENT_ID` + `STRAVA_CLIENT_SECRET` (+ optional `STRAVA_REFRESH_TOKEN`)

If only `STRAVA_CLIENT_ID` + `STRAVA_CLIENT_SECRET` are set, the script opens browser OAuth and stores the token at `.ignore/strava_token.json`.

## Usage

```bash
source .venv/bin/activate
python scripts/running_totals.py --from 2026-02-01 --to 2026-02-28
```

Defaults:
- Sport filter is `Run`.
- Date range is inclusive on both ends.
- Cache is stored in `.ignore/strava_activities.json` and auto-refreshes after 6 hours.

Examples:

```bash
# Include all activity types
python scripts/running_totals.py --from 2026-02-01 --to 2026-02-28 --sport ALL

# Force refresh from Strava now
python scripts/running_totals.py --from 2026-02-01 --to 2026-02-28 --refresh-cache
```
