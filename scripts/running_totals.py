#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen


STRAVA_AUTH_URL = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"
REDIRECT_URI = "http://localhost:8080/"


@dataclass
class Config:
    access_token: str | None
    client_id: str | None
    client_secret: str | None
    refresh_token: str | None
    token_cache_file: Path
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
    access_token = os.getenv("STRAVA_ACCESS_TOKEN")
    client_id = os.getenv("STRAVA_CLIENT_ID")
    client_secret = os.getenv("STRAVA_CLIENT_SECRET")
    refresh_token = os.getenv("STRAVA_REFRESH_TOKEN")

    has_client_creds = all((client_id, client_secret))
    has_refresh_flow = has_client_creds and bool(refresh_token)
    if not access_token and not has_client_creds and not has_refresh_flow:
        raise SystemExit(
            "Missing Strava auth configuration. Provide either:\n"
            "1) STRAVA_ACCESS_TOKEN\n"
            "or\n"
            "2) STRAVA_CLIENT_ID + STRAVA_CLIENT_SECRET (+ optional STRAVA_REFRESH_TOKEN)\n"
            "Set them in .envrc and run `direnv allow`."
        )

    cache_dir = Path(".ignore")
    cache_dir.mkdir(parents=True, exist_ok=True)

    return Config(
        access_token=access_token,
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
        token_cache_file=cache_dir / "strava_token.json",
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
        if exc.code == 401:
            try:
                parsed = json.loads(detail)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                errors = parsed.get("errors")
                if isinstance(errors, list):
                    for err in errors:
                        if (
                            isinstance(err, dict)
                            and err.get("field") == "activity:read_permission"
                            and err.get("code") == "missing"
                        ):
                            raise SystemExit(
                                "Strava token is missing activity scope. "
                                "Re-authorize with `activity:read_all` (or `activity:read`) "
                                "and update STRAVA_ACCESS_TOKEN or STRAVA_REFRESH_TOKEN."
                            ) from exc
        raise SystemExit(f"Strava API error {exc.code}: {detail}") from exc
    except URLError as exc:
        raise SystemExit(f"Failed to reach Strava API: {exc}") from exc


def fetch_access_token(cfg: Config) -> str:
    if not cfg.client_id or not cfg.client_secret or not cfg.refresh_token:
        raise SystemExit(
            "Refresh-token auth requires STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, and STRAVA_REFRESH_TOKEN."
        )
    payload = http_json_request(
        STRAVA_TOKEN_URL,
        method="POST",
        data={
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": cfg.refresh_token,
        },
    )
    scope = str(payload.get("scope") or "")
    has_required_scope = "activity:read_all" in scope or "activity:read" in scope
    if scope and not has_required_scope:
        raise SystemExit(
            f"Refresh token scope is `{scope}` but needs `activity:read_all` or `activity:read`."
        )
    token = payload.get("access_token")
    if not token:
        raise SystemExit("Strava OAuth response did not include an access_token.")
    return token


class OAuthHandler(BaseHTTPRequestHandler):
    code: str | None = None

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        if code:
            OAuthHandler.code = code
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>Authorization successful. You can close this window.</h1>")
        else:
            self.send_response(400)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>Authorization failed.</h1>")

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        # Keep CLI output clean during OAuth callback.
        return


def load_cached_access_token(cfg: Config) -> str | None:
    if not cfg.token_cache_file.exists():
        return None
    try:
        payload = json.loads(cfg.token_cache_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    token = payload.get("access_token")
    if isinstance(token, str) and token:
        return token
    return None


def save_token_cache(cfg: Config, token_payload: dict[str, Any]) -> None:
    try:
        cfg.token_cache_file.write_text(json.dumps(token_payload, indent=2), encoding="utf-8")
    except OSError:
        # Non-fatal: continue without local token cache.
        return


def run_oauth_browser_flow(cfg: Config) -> str:
    if not cfg.client_id or not cfg.client_secret:
        raise SystemExit("Browser OAuth requires STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET.")

    OAuthHandler.code = None
    server = HTTPServer(("localhost", 8080), OAuthHandler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    params = {
        "client_id": cfg.client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "activity:read_all",
        "approval_prompt": "auto",
    }
    auth_url = f"{STRAVA_AUTH_URL}?{urlencode(params)}"
    print(f"Opening browser for Strava login: {auth_url}")
    webbrowser.open(auth_url)

    timeout_seconds = 180
    elapsed = 0.0
    while OAuthHandler.code is None and elapsed < timeout_seconds:
        time.sleep(0.2)
        elapsed += 0.2
    server.server_close()

    if OAuthHandler.code is None:
        raise SystemExit("Timed out waiting for Strava OAuth callback on http://localhost:8080/")

    payload = http_json_request(
        STRAVA_TOKEN_URL,
        method="POST",
        data={
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "code": OAuthHandler.code,
            "grant_type": "authorization_code",
        },
    )
    if not isinstance(payload, dict):
        raise SystemExit("Unexpected OAuth token response format.")

    save_token_cache(cfg, payload)
    token = payload.get("access_token")
    if not token:
        raise SystemExit("Strava OAuth response did not include an access_token.")
    return str(token)


def resolve_access_token(cfg: Config) -> str:
    if cfg.access_token:
        return cfg.access_token

    if cfg.refresh_token and cfg.client_id and cfg.client_secret:
        return fetch_access_token(cfg)

    cached = load_cached_access_token(cfg)
    if cached:
        return cached

    return run_oauth_browser_flow(cfg)


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
        token = resolve_access_token(cfg)
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
