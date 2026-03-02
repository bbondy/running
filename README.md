# running

Get Strava running distance/elevation totals for an inclusive date range.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
cp .envrc.template .envrc
# fill in STRAVA_* values in .envrc
direnv allow
```

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

