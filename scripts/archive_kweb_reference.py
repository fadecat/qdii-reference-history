from __future__ import annotations

import argparse
import json
import re
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
    "Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0"
)
DEFAULT_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = [60, 180, 300]
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v7/finance/chart/KWEB?range=5d&interval=1d&includePrePost=false"
KRANE_PRODUCT_URL = "https://kraneshares.com/etf/kweb/"
SNAPSHOT_DIR = REPO_ROOT / "snapshots" / "krane" / "kweb_nav" / "daily"
RAW_YAHOO_DIR = REPO_ROOT / "raw" / "yahoo" / "chart" / "kweb" / "daily"
RAW_KRANE_DIR = REPO_ROOT / "raw" / "krane" / "product-json" / "kweb_premium_discount" / "daily"


def _now_bundle() -> dict[str, str]:
    now_utc = datetime.now(UTC)
    return {
        "captured_at_utc": now_utc.isoformat(),
        "captured_at_shanghai": now_utc.astimezone(SH_TZ).isoformat(),
        "captured_at_new_york": now_utc.astimezone(NY_TZ).isoformat(),
    }


def _fetch_text(url: str, *, headers: dict[str, str] | None = None) -> str:
    base_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-US;q=0.7",
        "Connection": "keep-alive",
    }
    if headers:
        base_headers.update(headers)
    request = urllib.request.Request(url, headers=base_headers)
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


def _latest_non_null(values: list[float | None]) -> tuple[float | None, int | None]:
    for idx in range(len(values) - 1, -1, -1):
        value = values[idx]
        if value is not None:
            return float(value), idx
    return None, None


def _discover_krane_product_id() -> tuple[str, str]:
    html = _fetch_text(
        KRANE_PRODUCT_URL,
        headers={
            "Referer": "https://kraneshares.com/",
            "Origin": "https://kraneshares.com",
        },
    )
    patterns = [
        r"drawPremiumDiscountHighChart\(startDate,endDate\)\{.*?product-json/\?pid=(\d+)&type=premium-discount",
        r"Premium/Discount Chart.*?product-json/\?pid=(\d+)&type=premium-discount",
        r"product-json/\?pid=(\d+)&type=premium-discount",
        r'"postid":"(\d+)"',
        r"https://kraneshares\.com/\?p=(\d+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, html, re.S)
        if match is not None:
            return match.group(1), html
    raise ValueError("Unable to discover current Krane KWEB product id from product page.")


def _fetch_latest_yahoo_close() -> tuple[dict[str, object], dict[str, object]]:
    raw_text = _fetch_text(
        YAHOO_CHART_URL,
        headers={
            "Referer": "https://finance.yahoo.com/quote/KWEB/",
            "Origin": "https://finance.yahoo.com",
        },
    )
    payload = json.loads(raw_text)
    result = payload["chart"]["result"][0]
    meta = result["meta"]
    quote = result["indicators"]["quote"][0]
    timestamps = result.get("timestamp", [])
    close_values = quote.get("close", [])
    close_value, idx = _latest_non_null(close_values)
    if close_value is None or idx is None:
        raise ValueError("Yahoo KWEB chart payload did not contain a usable close price.")

    timestamp_value = int(timestamps[idx])
    market_dt = datetime.fromtimestamp(timestamp_value, UTC).astimezone(NY_TZ)
    row = {
        "market_date": market_dt.strftime("%Y-%m-%d"),
        "timestamp": timestamp_value,
        "market_datetime": market_dt.isoformat(),
        "close_price": close_value,
        "currency": meta.get("currency"),
        "exchange_name": meta.get("exchangeName"),
        "instrument_type": meta.get("instrumentType"),
        "regular_market_price": meta.get("regularMarketPrice"),
        "previous_close": meta.get("previousClose"),
        "source_url": YAHOO_CHART_URL,
    }
    return row, payload


def _fetch_krane_premium_discount(*, market_date: str, product_id: str) -> tuple[dict[str, object], list[object]]:
    url = (
        "https://kraneshares.com/product-json/"
        f"?pid={product_id}&type=premium-discount&start={market_date}&end={market_date}"
    )
    raw_text = _fetch_text(
        url,
        headers={
            "Referer": KRANE_PRODUCT_URL,
            "Origin": "https://kraneshares.com",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    payload = json.loads(raw_text)
    if not payload or len(payload[0]) < 2:
        raise ValueError(f"Krane premium-discount payload is empty for {market_date}.")
    source_timestamp_ms = int(payload[0][0])
    source_utc_dt = datetime.fromtimestamp(source_timestamp_ms / 1000, UTC)
    source_market_dt = source_utc_dt.astimezone(NY_TZ)
    source_market_date = source_utc_dt.strftime("%Y-%m-%d")
    if source_market_date != market_date:
        raise ValueError(
            f"Krane premium-discount market date mismatch. expected={market_date} actual={source_market_date}"
        )
    row = {
        "market_date": market_date,
        "source_utc_datetime": source_utc_dt.isoformat(),
        "source_market_datetime": source_market_dt.isoformat(),
        "source_timestamp_ms": source_timestamp_ms,
        "premium_discount": float(payload[0][1]),
        "product_id": product_id,
        "source_url": url,
    }
    return row, payload


def archive_once() -> dict[str, object]:
    capture_meta = _now_bundle()
    product_id, _krane_html = _discover_krane_product_id()
    yahoo_row, yahoo_payload = _fetch_latest_yahoo_close()
    krane_row, krane_payload = _fetch_krane_premium_discount(
        market_date=str(yahoo_row["market_date"]),
        product_id=product_id,
    )
    market_date = str(yahoo_row["market_date"])
    kweb_nav = float(yahoo_row["close_price"]) * (1.0 - float(krane_row["premium_discount"]))

    yyyy, mm, _dd = market_date.split("-")
    snapshot_path = SNAPSHOT_DIR / yyyy / mm / f"{market_date}.json"
    yahoo_raw_path = RAW_YAHOO_DIR / yyyy / mm / f"{market_date}.json"
    krane_raw_path = RAW_KRANE_DIR / yyyy / mm / f"{market_date}.json"

    snapshot = {
        "schema_version": 1,
        "provider": "krane_yahoo_derived",
        "symbol": "KWEB",
        "series_kind": "nav_like_reference",
        "source_quality": "derived_from_official_and_market_inputs",
        "market_timezone": "America/New_York",
        "market_date": market_date,
        "captured_at_utc": capture_meta["captured_at_utc"],
        "captured_at_shanghai": capture_meta["captured_at_shanghai"],
        "captured_at_new_york": capture_meta["captured_at_new_york"],
        "kweb_nav": kweb_nav,
        "market_close": float(yahoo_row["close_price"]),
        "premium_discount": float(krane_row["premium_discount"]),
        "formula": "kweb_nav = market_close * (1 - premium_discount)",
        "sources": {
            "yahoo_chart_url": str(yahoo_row["source_url"]),
            "krane_product_page_url": KRANE_PRODUCT_URL,
            "krane_premium_discount_url": str(krane_row["source_url"]),
        },
        "krane_product_id": product_id,
        "yahoo_meta": {
            "currency": yahoo_row.get("currency"),
            "exchange_name": yahoo_row.get("exchange_name"),
            "instrument_type": yahoo_row.get("instrument_type"),
            "regular_market_price": yahoo_row.get("regular_market_price"),
            "previous_close": yahoo_row.get("previous_close"),
        },
        "yahoo_latest_bar": {
            "timestamp": yahoo_row["timestamp"],
            "market_datetime": yahoo_row["market_datetime"],
            "close_price": yahoo_row["close_price"],
        },
        "krane_premium_discount_row": {
            "source_timestamp_ms": krane_row["source_timestamp_ms"],
            "source_utc_datetime": krane_row["source_utc_datetime"],
            "source_market_datetime": krane_row["source_market_datetime"],
            "premium_discount": krane_row["premium_discount"],
        },
    }

    _write_json(snapshot_path, snapshot)
    _write_json(yahoo_raw_path, yahoo_payload)
    _write_json(krane_raw_path, krane_payload)

    dirty_paths = _git_dirty_paths()
    symbol_row = {
        "symbol": "KWEB",
        "market_date": market_date,
        "snapshot_path": snapshot_path.relative_to(REPO_ROOT).as_posix(),
        "yahoo_raw_path": yahoo_raw_path.relative_to(REPO_ROOT).as_posix(),
        "krane_raw_path": krane_raw_path.relative_to(REPO_ROOT).as_posix(),
        "latest_value": kweb_nav,
        "market_close": float(yahoo_row["close_price"]),
        "premium_discount": float(krane_row["premium_discount"]),
        "krane_product_id": product_id,
        "yahoo_source_url": str(yahoo_row["source_url"]),
        "krane_source_url": str(krane_row["source_url"]),
    }
    return {
        "status": "success",
        "captured_at_utc": capture_meta["captured_at_utc"],
        "captured_at_shanghai": capture_meta["captured_at_shanghai"],
        "captured_at_new_york": capture_meta["captured_at_new_york"],
        "archived": [symbol_row],
        "symbols": {"KWEB": symbol_row},
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

    now_utc = datetime.now(UTC)
    return {
        "status": "failed",
        "attempts": attempts,
        "errors": errors,
        "captured_at_utc": now_utc.isoformat(),
        "captured_at_shanghai": now_utc.astimezone(SH_TZ).isoformat(),
        "captured_at_new_york": now_utc.astimezone(NY_TZ).isoformat(),
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
