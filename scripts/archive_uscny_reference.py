from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from datetime import UTC
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


REPO_ROOT = Path(__file__).resolve().parent.parent
SH_TZ = ZoneInfo("Asia/Shanghai")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/148.0.0.0 Safari/537.36"
)
DEFAULT_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = [60, 180, 300]
CCPR_URL = "https://www.chinamoney.com.cn/r/cms/www/chinamoney/data/fx/ccpr.json"
SNAPSHOT_DIR = REPO_ROOT / "snapshots" / "chinamoney" / "uscny" / "daily"
RAW_DIR = REPO_ROOT / "raw" / "chinamoney" / "ccpr" / "daily"


def _now_bundle() -> dict[str, str]:
    now_utc = datetime.now(UTC)
    return {
        "captured_at_utc": now_utc.isoformat(),
        "captured_at_shanghai": now_utc.astimezone(SH_TZ).isoformat(),
    }


def _fetch_text(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json,text/plain,*/*",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


def _write_json(target: Path, payload: dict[str, object] | list[object] | str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        target.write_text(payload, encoding="utf-8")
    else:
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def _git_dirty_paths() -> list[str]:
    result = subprocess.run(
        ["git", "status", "--short"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    paths: list[str] = []
    for line in result.stdout.splitlines():
        cleaned = line.strip()
        if not cleaned:
            continue
        parts = cleaned.split(maxsplit=1)
        if len(parts) == 2:
            paths.append(parts[1])
    return paths


def _extract_uscny_row(payload: dict[str, object]) -> dict[str, object]:
    records = payload.get("records", [])
    for row in records:
        if row.get("vrtEName") == "USD/CNY":
            return row
    raise ValueError("USD/CNY row was not found in Chinamoney ccpr payload.")


def archive_once() -> dict[str, object]:
    raw_text = _fetch_text(CCPR_URL)
    payload = json.loads(raw_text)
    row = _extract_uscny_row(payload)
    data = payload["data"]
    last_date = str(data["lastDate"])
    market_date = last_date.split(" ")[0]
    yyyy, mm, _dd = market_date.split("-")
    capture_meta = _now_bundle()

    snapshot = {
        "schema_version": 1,
        "provider": "chinamoney_ccpr",
        "symbol": "USCNY",
        "reference_pair": "USD/CNY",
        "source_quality": "official_fixing",
        "market_date": market_date,
        "published_at_source": last_date,
        "captured_at_utc": capture_meta["captured_at_utc"],
        "captured_at_shanghai": capture_meta["captured_at_shanghai"],
        "source_url": CCPR_URL,
        "price": float(row["price"]),
        "bp": row.get("bp"),
        "bp_double": row.get("bpDouble"),
        "meta": {
            "provider": payload.get("head", {}).get("provider"),
            "rep_code": payload.get("head", {}).get("rep_code"),
            "rep_message": payload.get("head", {}).get("rep_message"),
            "last_date_en": data.get("lastDateEn"),
            "vrt_name": row.get("vrtName"),
            "foreign_code": row.get("foreignCName"),
        },
    }

    snapshot_path = SNAPSHOT_DIR / yyyy / mm / f"{market_date}.json"
    raw_path = RAW_DIR / yyyy / mm / f"{market_date}.json"
    _write_json(snapshot_path, snapshot)
    _write_json(raw_path, payload)

    dirty_paths = _git_dirty_paths()
    result = {
        "status": "success",
        "captured_at_utc": capture_meta["captured_at_utc"],
        "captured_at_shanghai": capture_meta["captured_at_shanghai"],
        "archived": [
            {
                "symbol": "USCNY",
                "market_date": market_date,
                "snapshot_path": snapshot_path.relative_to(REPO_ROOT).as_posix(),
                "raw_path": raw_path.relative_to(REPO_ROOT).as_posix(),
                "latest_value": float(row["price"]),
                "source_url": CCPR_URL,
                "published_at_source": last_date,
            }
        ],
        "symbols": {
            "USCNY": {
                "symbol": "USCNY",
                "market_date": market_date,
                "snapshot_path": snapshot_path.relative_to(REPO_ROOT).as_posix(),
                "raw_path": raw_path.relative_to(REPO_ROOT).as_posix(),
                "latest_value": float(row["price"]),
                "source_url": CCPR_URL,
                "published_at_source": last_date,
            }
        },
        "changed_paths": dirty_paths,
        "has_changes": bool(dirty_paths),
    }
    return result


def archive_with_retry(*, attempts: int, backoff_seconds: list[int], sleep_enabled: bool) -> dict[str, object]:
    errors: list[dict[str, object]] = []
    for attempt in range(1, attempts + 1):
        try:
            result = archive_once()
            result["attempt"] = attempt
            result["attempts"] = attempts
            result["errors"] = errors
            return result
        except Exception as exc:
            errors.append(
                {
                    "attempt": attempt,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                }
            )
            if attempt >= attempts:
                break
            sleep_seconds = backoff_seconds[min(attempt - 1, len(backoff_seconds) - 1)]
            if sleep_enabled:
                time.sleep(sleep_seconds)

    return {
        "status": "failed",
        "attempts": attempts,
        "errors": errors,
        "captured_at_utc": datetime.now(UTC).isoformat(),
        "captured_at_shanghai": datetime.now(UTC).astimezone(SH_TZ).isoformat(),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--attempts", type=int, default=DEFAULT_ATTEMPTS)
    parser.add_argument(
        "--backoff-seconds",
        type=str,
        default=",".join(str(value) for value in DEFAULT_BACKOFF_SECONDS),
    )
    parser.add_argument("--result-path", type=str, default="")
    parser.add_argument("--no-sleep", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    backoff_seconds = [
        int(part.strip())
        for part in args.backoff_seconds.split(",")
        if part.strip()
    ]
    result = archive_with_retry(
        attempts=args.attempts,
        backoff_seconds=backoff_seconds,
        sleep_enabled=not args.no_sleep,
    )

    if args.result_path:
        result_path = Path(args.result_path)
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("status") != "success":
        sys.exit(1)


if __name__ == "__main__":
    main()
