from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import UTC
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


REPO_ROOT = Path(__file__).resolve().parent.parent
NY_TZ = ZoneInfo("America/New_York")
SH_TZ = ZoneInfo("Asia/Shanghai")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/148.0.0.0 Safari/537.36"
)
DEFAULT_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = [60, 180, 300]

SYMBOLS = [
    {
        "symbol": "XOP",
        "encoded_symbol": "XOP",
        "instrument_label": "SPDR S&P Oil & Gas Exploration & Production ETF",
        "snapshot_dir": REPO_ROOT / "snapshots" / "yahoo" / "xop" / "daily",
        "raw_dir": REPO_ROOT / "raw" / "yahoo" / "chart" / "xop" / "daily",
        "series_kind": "etf_close",
        "source_quality": "market_close",
    },
    {
        "symbol": "^XOP-IV",
        "encoded_symbol": "%5EXOP-IV",
        "instrument_label": "SPDR S&P Oil & Gas Exploration & Production ETF indicative value",
        "snapshot_dir": REPO_ROOT / "snapshots" / "yahoo" / "xop_iv" / "daily",
        "raw_dir": REPO_ROOT / "raw" / "yahoo" / "chart" / "xop_iv" / "daily",
        "series_kind": "indicative_value",
        "source_quality": "primary_reference_candidate",
    },
]


def _now_bundle() -> dict[str, str]:
    now_utc = datetime.now(UTC)
    return {
        "captured_at_utc": now_utc.isoformat(),
        "captured_at_shanghai": now_utc.astimezone(SH_TZ).isoformat(),
        "captured_at_new_york": now_utc.astimezone(NY_TZ).isoformat(),
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


def _market_dt_from_timestamp(timestamp_value: int, timezone_name: str) -> datetime:
    tz = ZoneInfo(timezone_name)
    return datetime.fromtimestamp(int(timestamp_value), UTC).astimezone(tz)


def _latest_non_null(values: list[float | None]) -> tuple[float | None, int | None]:
    for idx in range(len(values) - 1, -1, -1):
        value = values[idx]
        if value is not None:
            return float(value), idx
    return None, None


def _pick(values: list[float | None], idx: int) -> float | None:
    if idx >= len(values):
        return None
    value = values[idx]
    if value is None:
        return None
    return float(value)


def _build_snapshot(symbol_config: dict[str, object], payload_text: str) -> tuple[str, dict[str, object]]:
    payload = json.loads(payload_text)
    result = payload["chart"]["result"][0]
    meta = result["meta"]
    timestamps = result.get("timestamp", [])
    quote = result["indicators"]["quote"][0]
    adjclose_rows = result["indicators"].get("adjclose", [])
    adjclose = adjclose_rows[0] if adjclose_rows else {}

    close_values = quote.get("close", [])
    close_value, idx = _latest_non_null(close_values)
    adjclose_value, adj_idx = _latest_non_null(adjclose.get("adjclose", []))
    if idx is None:
        idx = adj_idx
    if idx is None:
        raise ValueError(f"No usable bar found for {symbol_config['symbol']}")

    timestamp_value = int(timestamps[idx])
    market_timezone = str(meta.get("exchangeTimezoneName") or "America/New_York")
    market_dt = _market_dt_from_timestamp(timestamp_value, market_timezone)
    market_date = market_dt.strftime("%Y-%m-%d")
    regular_market_time = meta.get("regularMarketTime")
    regular_market_dt = (
        _market_dt_from_timestamp(int(regular_market_time), market_timezone)
        if regular_market_time is not None
        else None
    )
    capture_meta = _now_bundle()

    snapshot = {
        "schema_version": 1,
        "provider": "yahoo_chart",
        "symbol": symbol_config["symbol"],
        "encoded_symbol": symbol_config["encoded_symbol"],
        "instrument_label": symbol_config["instrument_label"],
        "series_kind": symbol_config["series_kind"],
        "source_quality": symbol_config["source_quality"],
        "market_timezone": market_timezone,
        "market_date": market_date,
        "captured_at_utc": capture_meta["captured_at_utc"],
        "captured_at_shanghai": capture_meta["captured_at_shanghai"],
        "captured_at_new_york": capture_meta["captured_at_new_york"],
        "source_url": (
            "https://query1.finance.yahoo.com/v8/finance/chart/"
            f"{symbol_config['encoded_symbol']}?range=5d&interval=1d"
        ),
        "meta": {
            "currency": meta.get("currency"),
            "exchange_name": meta.get("exchangeName"),
            "full_exchange_name": meta.get("fullExchangeName"),
            "instrument_type": meta.get("instrumentType"),
            "regular_market_price": meta.get("regularMarketPrice"),
            "previous_close": meta.get("previousClose"),
            "chart_previous_close": meta.get("chartPreviousClose"),
            "regular_market_time": meta.get("regularMarketTime"),
        },
        "latest_bar": {
            "timestamp": timestamp_value,
            "bar_market_datetime": market_dt.isoformat(),
            "regular_market_datetime": (
                regular_market_dt.isoformat() if regular_market_dt is not None else None
            ),
            "open": _pick(quote.get("open", []), idx),
            "high": _pick(quote.get("high", []), idx),
            "low": _pick(quote.get("low", []), idx),
            "close": close_value,
            "adjclose": adjclose_value,
            "volume": _pick(quote.get("volume", []), idx),
        },
        "history_summary": {
            "bar_count": len(timestamps),
            "latest_non_null_close_count": sum(1 for value in close_values if value is not None),
        },
    }
    return market_date, snapshot


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


def archive_symbol(symbol_config: dict[str, object]) -> dict[str, object]:
    encoded_symbol = str(symbol_config["encoded_symbol"])
    chart_url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded_symbol}?range=5d&interval=1d"
    raw_text = _fetch_text(chart_url)
    market_date, snapshot = _build_snapshot(symbol_config, raw_text)

    yyyy, mm, _dd = market_date.split("-")
    snapshot_path = Path(symbol_config["snapshot_dir"]) / yyyy / mm / f"{market_date}.json"
    raw_path = Path(symbol_config["raw_dir"]) / yyyy / mm / f"{market_date}.json"

    _write_json(snapshot_path, snapshot)
    _write_json(raw_path, json.loads(raw_text))

    return {
        "symbol": symbol_config["symbol"],
        "market_date": market_date,
        "snapshot_path": snapshot_path.relative_to(REPO_ROOT).as_posix(),
        "raw_path": raw_path.relative_to(REPO_ROOT).as_posix(),
        "latest_value": snapshot["latest_bar"]["adjclose"] or snapshot["latest_bar"]["close"],
        "source_url": snapshot["source_url"],
    }


def archive_once() -> dict[str, object]:
    capture_meta = _now_bundle()
    archived = [archive_symbol(symbol_config) for symbol_config in SYMBOLS]
    dirty_paths = _git_dirty_paths()
    by_symbol = {item["symbol"]: item for item in archived}
    return {
        "status": "success",
        "captured_at_utc": capture_meta["captured_at_utc"],
        "captured_at_shanghai": capture_meta["captured_at_shanghai"],
        "captured_at_new_york": capture_meta["captured_at_new_york"],
        "archived": archived,
        "symbols": {
            "XOP": by_symbol.get("XOP"),
            "^XOP-IV": by_symbol.get("^XOP-IV"),
        },
        "changed_paths": dirty_paths,
        "has_changes": bool(dirty_paths),
    }


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
            error_row = {
                "attempt": attempt,
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            }
            errors.append(error_row)
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
        "captured_at_new_york": datetime.now(UTC).astimezone(NY_TZ).isoformat(),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--attempts", type=int, default=DEFAULT_ATTEMPTS)
    parser.add_argument(
        "--backoff-seconds",
        type=str,
        default=",".join(str(value) for value in DEFAULT_BACKOFF_SECONDS),
    )
    parser.add_argument(
        "--result-path",
        type=str,
        default="",
        help="Optional path to save structured result JSON.",
    )
    parser.add_argument(
        "--no-sleep",
        action="store_true",
        help="Disable retry sleeping, useful for local quick verification.",
    )
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
