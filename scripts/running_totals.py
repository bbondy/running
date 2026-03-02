#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


STRAVA_OAUTH_URL = "https://www.strava.com/api/v3/oauth/token"
STRAVA_ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"


@dataclass
class Config:
    client_id: str
    client_secret: str
    refresh_token: str
    cache_file: Path
    force_refresh: bool
    cache_max_age_hours: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compute Strava totals for a date range (inclusive). "
            "Loads activities from a local cache under .ignore/."
        )
    )
    parser.add_argument("--from", dest="from_date", required=True, help="Start date (YYYY-MM-DD).")
    parser.add_argument("--to", dest="to_date", required=True, help="End date (YYYY-MM-DD).")
    parser.add_argument(
        "--sport",
        default="Run",
        help="Sport filter (default: Run). Use ALL to include every activity.",
    )
    parser.add_argument(
        "--refresh-cache",
        action="store_true",
        help="Ignore existing cache and fetch all activities from Strava.",
    )
    parser.add_argument(
        "--cache-max-age-hours",
        type=int,
        default=6,
        help="Cache age in hours before auto-refresh (default: 6).",
    )
    return parser.parse_args()


def parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(f"Invalid date: {value}. Use YYYY-MM-DD.") from exc


def load_config(args: argparse.Namespace) -> Config:
    missing = [
        name
        for name in ("STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET", "STRAVA_REFRESH_TOKEN")
        if not os.getenv(name)
    ]
    if missing:
        raise SystemExit(
            "Missing required environment variable(s): "
            f"{', '.join(missing)}. Set them in .envrc and run `direnv allow`."
        )

    cache_dir = Path(".ignore")
    cache_dir.mkdir(parents=True, exist_ok=True)

    return Config(
        client_id=os.environ["STRAVA_CLIENT_ID"],
        client_secret=os.environ["STRAVA_CLIENT_SECRET"],
        refresh_token=os.environ["STRAVA_REFRESH_TOKEN"],
        cache_file=cache_dir / "strava_activities.json",
        force_refresh=args.refresh_cache,
        cache_max_age_hours=args.cache_max_age_hours,
    )


def http_json_request(url: str, method: str = "GET", data: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> Any:
    body = None
    req_headers = {"Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    if data is not None:
        body = urlencode(data).encode("utf-8")
        req_headers["Content-Type"] = "application/x-www-form-urlencoded"

    request = Request(url=url, data=body, method=method, headers=req_headers)

    try:
        with urlopen(request, timeout=30) as response:
            payload = response.read().decode("utf-8")
            return json.loads(payload)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Strava API error {exc.code}: {detail}") from exc
    except URLError as exc:
        raise SystemExit(f"Failed to reach Strava API: {exc}") from exc


def fetch_access_token(cfg: Config) -> str:
    payload = http_json_request(
        STRAVA_OAUTH_URL,
        method="POST",
        data={
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": cfg.refresh_token,
        },
    )
    token = payload.get("access_token")
    if not token:
        raise SystemExit("Strava OAuth response did not include an access_token.")
    return token


def fetch_all_activities(access_token: str) -> list[dict[str, Any]]:
    page = 1
    per_page = 200
    all_activities: list[dict[str, Any]] = []

    while True:
        url = f"{STRAVA_ACTIVITIES_URL}?page={page}&per_page={per_page}"
        payload = http_json_request(
            url,
            method="GET",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if not isinstance(payload, list):
            raise SystemExit("Unexpected Strava activities response format.")
        if not payload:
            break

        all_activities.extend(payload)
        if len(payload) < per_page:
            break
        page += 1

    return all_activities


def is_cache_fresh(cache_payload: dict[str, Any], max_age_hours: int) -> bool:
    fetched_at = cache_payload.get("fetched_at")
    if not fetched_at:
        return False
    try:
        fetched_dt = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    age_seconds = (datetime.now(timezone.utc) - fetched_dt).total_seconds()
    return age_seconds <= max_age_hours * 3600


def load_cached_activities(cfg: Config) -> list[dict[str, Any]] | None:
    if cfg.force_refresh or not cfg.cache_file.exists():
        return None

    try:
        payload = json.loads(cfg.cache_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return None
    if not is_cache_fresh(payload, cfg.cache_max_age_hours):
        return None

    activities = payload.get("activities")
    if not isinstance(activities, list):
        return None

    return activities


def write_cache(cache_file: Path, activities: list[dict[str, Any]]) -> None:
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "activities": activities,
    }
    cache_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def activity_date(activity: dict[str, Any]) -> date | None:
    raw = activity.get("start_date_local") or activity.get("start_date")
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def activity_sport(activity: dict[str, Any]) -> str:
    sport = activity.get("sport_type") or activity.get("type") or ""
    return str(sport)


def main() -> int:
    args = parse_args()
    from_date = parse_iso_date(args.from_date)
    to_date = parse_iso_date(args.to_date)
    if from_date > to_date:
        raise SystemExit("--from must be before or equal to --to.")
    if args.cache_max_age_hours < 0:
        raise SystemExit("--cache-max-age-hours must be >= 0.")

    cfg = load_config(args)

    activities = load_cached_activities(cfg)
    cache_used = activities is not None
    if activities is None:
        token = fetch_access_token(cfg)
        activities = fetch_all_activities(token)
        write_cache(cfg.cache_file, activities)

    sport_filter = args.sport.strip()
    include_all = sport_filter.upper() == "ALL"
    if not include_all:
        target = sport_filter.lower()
    else:
        target = ""

    total_distance_m = 0.0
    total_elev_m = 0.0
    matching_count = 0

    for activity in activities:
        act_date = activity_date(activity)
        if act_date is None or act_date < from_date or act_date > to_date:
            continue
        if not include_all and activity_sport(activity).lower() != target:
            continue

        total_distance_m += float(activity.get("distance") or 0.0)
        total_elev_m += float(activity.get("total_elevation_gain") or 0.0)
        matching_count += 1

    print(
        f"Date range (inclusive): {from_date.isoformat()} to {to_date.isoformat()}\n"
        f"Sport filter: {'ALL' if include_all else sport_filter}\n"
        f"Cache: {'hit' if cache_used else 'refreshed'} ({cfg.cache_file})\n"
        f"Activities matched: {matching_count}\n"
        f"Distance: {total_distance_m:.2f} m | {total_distance_m / 1000:.2f} km | {total_distance_m / 1609.344:.2f} mi\n"
        f"Elevation gain: {total_elev_m:.2f} m | {total_elev_m * 3.28084:.2f} ft"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

