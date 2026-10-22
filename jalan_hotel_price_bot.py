#!/usr/bin/env python3
"""Collect Jalan hotel prices and alert on drops below the recorded minimum."""

from __future__ import annotations

import argparse
import csv
import gzip
import html
import json
import math
import os
import re
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from jalan_coupon_bot import (
    BASE_URL,
    DEFAULT_USER_AGENT,
    _fetch_text_url,
    atomic_write_json,
    load_json,
    post_discord,
)


JST = timezone(timedelta(hours=9))
RESULTS_PER_PAGE = 30
STATE_SCHEMA_VERSION = 1
SHARD_SCHEMA_VERSION = 1
DEFAULT_DROP_RATE_THRESHOLDS = (0.6, 0.7, 0.8, 0.9)
LISTING_QUERY = {
    "dateUndecided": "1",
    "stayCount": "1",
    "roomCount": "1",
    "adultNum": "2",
    "roomCrack": "200000",
    "minPrice": "0",
    "maxPrice": "999999",
    "activeSort": "1",
}

PREFECTURES = (
    ("010000", "北海道"),
    ("020000", "青森県"),
    ("030000", "岩手県"),
    ("040000", "宮城県"),
    ("050000", "秋田県"),
    ("060000", "山形県"),
    ("070000", "福島県"),
    ("080000", "茨城県"),
    ("090000", "栃木県"),
    ("100000", "群馬県"),
    ("110000", "埼玉県"),
    ("120000", "千葉県"),
    ("130000", "東京都"),
    ("140000", "神奈川県"),
    ("150000", "山梨県"),
    ("160000", "長野県"),
    ("170000", "新潟県"),
    ("180000", "富山県"),
    ("190000", "石川県"),
    ("200000", "福井県"),
    ("210000", "静岡県"),
    ("220000", "岐阜県"),
    ("230000", "愛知県"),
    ("240000", "三重県"),
    ("250000", "滋賀県"),
    ("260000", "京都府"),
    ("270000", "大阪府"),
    ("280000", "兵庫県"),
    ("290000", "奈良県"),
    ("300000", "和歌山県"),
    ("310000", "鳥取県"),
    ("320000", "島根県"),
    ("330000", "岡山県"),
    ("340000", "広島県"),
    ("350000", "山口県"),
    ("360000", "徳島県"),
    ("370000", "香川県"),
    ("380000", "愛媛県"),
    ("390000", "高知県"),
    ("400000", "福岡県"),
    ("410000", "佐賀県"),
    ("420000", "長崎県"),
    ("430000", "熊本県"),
    ("440000", "大分県"),
    ("450000", "宮崎県"),
    ("460000", "鹿児島県"),
    ("470000", "沖縄県"),
)


@dataclass(frozen=True)
class HotelPrice:
    hotel_id: str
    hotel_name: str
    prefecture: str
    area: str
    price_yen: int | None
    detail_url: str


@dataclass(frozen=True)
class PriceAnomaly:
    hotel_id: str
    hotel_name: str
    prefecture: str
    area: str
    price_yen: int
    historical_min_yen: int
    drop_rate: float
    sample_count: int
    detail_url: str


def _classes(attrs: list[tuple[str, str | None]]) -> set[str]:
    return set((dict(attrs).get("class") or "").split())


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def _parse_yen(value: str) -> int | None:
    normalized = value.translate(str.maketrans("０１２３４５６７８９，", "0123456789,"))
    match = re.search(r"([0-9,]+)\s*円", normalized)
    return int(match.group(1).replace(",", "")) if match else None


class HotelResultParser(HTMLParser):
    """Parse only regular hotel cards; Jalan's PR cards use sa_yadNo IDs."""

    def __init__(self, prefecture: str) -> None:
        super().__init__(convert_charrefs=True)
        self.prefecture = prefecture
        self.hotels: list[HotelPrice] = []
        self._card: dict[str, Any] | None = None
        self._li_depth = 0
        self._capture: str | None = None
        self._capture_tag = ""
        self._capture_depth = 0
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        if self._card is None:
            match = re.fullmatch(r"yadNo(\d+)", attrs_dict.get("id") or "")
            if tag == "li" and match and "js-searchResultItem" in _classes(attrs):
                self._card = {"hotel_id": match.group(1), "hotel_name": "", "area": "", "price": None}
                self._li_depth = 1
            return

        if tag == "li":
            self._li_depth += 1
        if self._capture is not None:
            self._capture_depth += 1
            return
        classes = _classes(attrs)
        if tag == "h2" and "p-searchResultItem__facilityName" in classes:
            self._start_capture("hotel_name", tag)
        elif tag == "dd" and "p-searchResultItem__areaValues" in classes:
            self._start_capture("area", tag)
        elif tag == "span" and "p-searchResultItem__lowestPriceValue" in classes:
            self._start_capture("price", tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        return

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._card is None:
            return
        if self._capture is not None:
            if self._capture_depth:
                self._capture_depth -= 1
            elif tag == self._capture_tag:
                value = _clean("".join(self._buffer))
                self._card[self._capture] = _parse_yen(value) if self._capture == "price" else value
                self._capture = None
                self._capture_tag = ""
                self._buffer = []
        if tag == "li":
            self._li_depth -= 1
            if self._li_depth == 0:
                self._finish_card()

    def _start_capture(self, field: str, tag: str) -> None:
        self._capture = field
        self._capture_tag = tag
        self._capture_depth = 0
        self._buffer = []

    def _finish_card(self) -> None:
        assert self._card is not None
        hotel_id = str(self._card["hotel_id"])
        hotel_name = str(self._card["hotel_name"])
        if not hotel_name:
            raise RuntimeError(f"Hotel card {hotel_id} did not contain a name")
        self.hotels.append(
            HotelPrice(
                hotel_id=hotel_id,
                hotel_name=hotel_name,
                prefecture=self.prefecture,
                area=str(self._card["area"]),
                price_yen=self._card["price"],
                detail_url=f"{BASE_URL}/yad{hotel_id}/?{urlencode(LISTING_QUERY)}",
            )
        )
        self._card = None
        self._capture = None
        self._li_depth = 0


def parse_hotel_results(page_html: str, prefecture: str) -> list[HotelPrice]:
    parser = HotelResultParser(prefecture)
    parser.feed(page_html)
    parser.close()
    return parser.hotels


def parse_result_count(page_html: str) -> int:
    patterns = (
        r'class=["\'][^"\']*jlnpc-listInformation--count[^"\']*["\'][^>]*>\s*([0-9,]+)',
        r'class=["\'][^"\']*jlnpc-displayedResults__num[^"\']*["\'][^>]*>\s*([0-9,]+)',
    )
    for pattern in patterns:
        match = re.search(pattern, page_html, re.IGNORECASE)
        if match:
            return int(match.group(1).replace(",", ""))
    if "お探しの宿泊施設は見つかりませんでした" in page_html:
        return 0
    raise RuntimeError("Could not parse the Jalan hotel result count")


def discover_region_urls(page_html: str, prefecture_code: str) -> list[str]:
    paths = re.findall(rf"/{re.escape(prefecture_code)}/LRG_[0-9]{{6}}/", page_html)
    return [f"{BASE_URL}{path}" for path in dict.fromkeys(paths)]


def build_region_page_url(region_url: str, page: int) -> str:
    if page < 1:
        raise ValueError("page must be at least 1")
    base = region_url.rstrip("/")
    path = f"{base}/" if page == 1 else f"{base}/page{page}.html"
    return f"{path}?{urlencode(LISTING_QUERY)}"


def shard_prefectures(shard_index: int, shard_count: int) -> list[tuple[str, str]]:
    if shard_count < 1 or not 0 <= shard_index < shard_count:
        raise ValueError("invalid shard index/count")
    return [value for index, value in enumerate(PREFECTURES) if index % shard_count == shard_index]


def shard_regions(regions: list[dict[str, str]], shard_index: int, shard_count: int) -> list[dict[str, str]]:
    if shard_count < 1 or not 0 <= shard_index < shard_count:
        raise ValueError("invalid shard index/count")
    return [value for index, value in enumerate(regions) if index % shard_count == shard_index]


def _fetch_validated_region_page(
    url: str,
    prefecture: str,
    expected_cards: int | None,
    timeout: int,
    user_agent: str,
    attempts: int,
    retry_delay: float,
) -> tuple[str, int, list[HotelPrice]]:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            _, page_html = _fetch_text_url(url, timeout, user_agent, 1, retry_delay)
            count = parse_result_count(page_html)
            hotels = parse_hotel_results(page_html, prefecture)
            if expected_cards is not None and len(hotels) != expected_cards:
                raise RuntimeError(f"expected {expected_cards} regular cards, parsed {len(hotels)}")
            return page_html, count, hotels
        except (RuntimeError, ValueError) as exc:
            last_error = exc
        if attempt + 1 < attempts:
            time.sleep(retry_delay)
    raise RuntimeError(f"Invalid Jalan result page {url} after {attempts} attempts: {last_error}")


def discover_regions(
    config: dict[str, Any],
    prefectures: list[tuple[str, str]],
) -> list[dict[str, str]]:
    timeout = max(int(config.get("request_timeout_seconds", 20)), 5)
    attempts = max(1, min(int(config.get("request_retry_count", 3)), 5))
    retry_delay = max(float(config.get("retry_delay_seconds", 3.0)), 1.0)
    delay = max(float(config.get("hotel_price_request_delay_seconds", 1.0)), 0.5)
    user_agent = str(config.get("user_agent") or DEFAULT_USER_AGENT)
    max_regions = max(int(config.get("hotel_price_max_regions_per_prefecture", 40)), 1)
    discovered: list[dict[str, str]] = []
    for index, (prefecture_code, prefecture_name) in enumerate(prefectures):
        if index:
            time.sleep(delay)
        print(f"Discovering {prefecture_name} regions", flush=True)
        _, landing_html = _fetch_text_url(
            f"{BASE_URL}/{prefecture_code}/", timeout, user_agent, attempts, retry_delay
        )
        region_urls = discover_region_urls(landing_html, prefecture_code)
        if not region_urls:
            raise RuntimeError(f"No large-area links found for {prefecture_name}")
        if len(region_urls) > max_regions:
            raise RuntimeError(f"{prefecture_name} has {len(region_urls)} regions, above safety limit {max_regions}")
        discovered.extend(
            {
                "prefecture_code": prefecture_code,
                "prefecture_name": prefecture_name,
                "region_url": region_url,
            }
            for region_url in region_urls
        )
    return discovered


def build_region_manifest(config: dict[str, Any], observed_date: date) -> dict[str, Any]:
    regions = discover_regions(config, list(PREFECTURES))
    if len(regions) < 100:
        raise RuntimeError(f"Only {len(regions)} nationwide regions were discovered; expected at least 100")
    return {
        "schema_version": 1,
        "observed_date_jst": observed_date.isoformat(),
        "regions": regions,
    }


def load_region_manifest(path: Path) -> tuple[date, list[dict[str, str]]]:
    manifest = load_json(path, {})
    if manifest.get("schema_version") != 1:
        raise RuntimeError("Unsupported region manifest schema")
    raw_regions = manifest.get("regions")
    if not isinstance(raw_regions, list) or not raw_regions:
        raise RuntimeError("Region manifest is empty")
    regions: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in raw_regions:
        if not isinstance(value, dict):
            raise RuntimeError("Region manifest contains an invalid item")
        item = {key: str(value.get(key, "")) for key in ("prefecture_code", "prefecture_name", "region_url")}
        if not all(item.values()) or item["region_url"] in seen:
            raise RuntimeError("Region manifest contains a missing or duplicate region")
        seen.add(item["region_url"])
        regions.append(item)
    return date.fromisoformat(str(manifest.get("observed_date_jst", ""))), regions


def _collect_region_once(
    region_url: str,
    prefecture_name: str,
    timeout: int,
    user_agent: str,
    attempts: int,
    retry_delay: float,
    delay: float,
    max_pages_per_region: int,
) -> tuple[int, int, list[HotelPrice]]:
    first_url = build_region_page_url(region_url, 1)
    _, total, first_hotels = _fetch_validated_region_page(
        first_url, prefecture_name, None, timeout, user_agent, attempts, retry_delay
    )
    pages = math.ceil(total / RESULTS_PER_PAGE) if total else 0
    if pages > max_pages_per_region:
        raise RuntimeError(f"{region_url} requires {pages} pages, above safety limit {max_pages_per_region}")
    expected_first = min(total, RESULTS_PER_PAGE)
    if len(first_hotels) != expected_first:
        raise RuntimeError(
            f"{region_url} reports {total} hotels but page 1 has {len(first_hotels)} regular cards"
        )
    region_hotels: dict[str, HotelPrice] = {}
    _merge_hotels(region_hotels, first_hotels)
    for page in range(2, pages + 1):
        time.sleep(delay)
        expected = min(RESULTS_PER_PAGE, total - ((page - 1) * RESULTS_PER_PAGE))
        _, repeated_total, page_hotels = _fetch_validated_region_page(
            build_region_page_url(region_url, page),
            prefecture_name,
            expected,
            timeout,
            user_agent,
            attempts,
            retry_delay,
        )
        if repeated_total != total:
            raise RuntimeError(f"Result count changed while scanning {region_url}: {total} -> {repeated_total}")
        _merge_hotels(region_hotels, page_hotels)
    if len(region_hotels) != total:
        raise RuntimeError(
            f"{region_url} reports {total} hotels but scan produced {len(region_hotels)} unique hotels"
        )
    return total, max(pages, 1), list(region_hotels.values())


def collect_region_prices(
    config: dict[str, Any],
    regions: list[dict[str, str]],
    shard_index: int,
    shard_count: int,
    observed_date: date,
) -> dict[str, Any]:
    timeout = max(int(config.get("request_timeout_seconds", 20)), 5)
    attempts = max(1, min(int(config.get("request_retry_count", 3)), 5))
    retry_delay = max(float(config.get("retry_delay_seconds", 3.0)), 1.0)
    delay = max(float(config.get("hotel_price_request_delay_seconds", 1.0)), 0.5)
    user_agent = str(config.get("user_agent") or DEFAULT_USER_AGENT)
    max_pages_per_region = max(int(config.get("hotel_price_max_pages_per_region", 50)), 1)
    max_pages_per_shard = max(int(config.get("hotel_price_max_pages_per_shard", 500)), 1)
    region_attempts = max(1, min(int(config.get("hotel_price_region_retry_count", 3)), 5))
    hotels: dict[str, HotelPrice] = {}
    pages_fetched = 0

    for region_index, region in enumerate(regions, 1):
        prefecture_name = region["prefecture_name"]
        region_url = region["region_url"]
        if pages_fetched:
            time.sleep(delay)
        last_error: RuntimeError | None = None
        for region_attempt in range(region_attempts):
            if region_attempt:
                time.sleep(retry_delay)
                print(
                    f"[{shard_index + 1}/{shard_count}] retrying {region_url} "
                    f"({region_attempt + 1}/{region_attempts})",
                    flush=True,
                )
            try:
                total, region_pages, region_hotels = _collect_region_once(
                    region_url,
                    prefecture_name,
                    timeout,
                    user_agent,
                    attempts,
                    retry_delay,
                    delay,
                    max_pages_per_region,
                )
                break
            except RuntimeError as exc:
                last_error = exc
        else:
            raise RuntimeError(
                f"Region scan failed after {region_attempts} attempts for {region_url}: {last_error}"
            )
        if pages_fetched + region_pages > max_pages_per_shard:
            raise RuntimeError(f"Shard exceeded hotel_price_max_pages_per_shard={max_pages_per_shard}")
        pages_fetched += region_pages
        _merge_hotels(hotels, region_hotels)
        print(
            f"[{shard_index + 1}/{shard_count}] region {region_index}/{len(regions)} "
            f"({prefecture_name}): {total} hotels, {region_pages} pages",
            flush=True,
        )

    priced = {hotel_id: asdict(item) for hotel_id, item in hotels.items() if item.price_yen is not None and item.price_yen > 0}
    return {
        "schema_version": SHARD_SCHEMA_VERSION,
        "observed_date_jst": observed_date.isoformat(),
        "shard_index": shard_index,
        "shard_count": shard_count,
        "prefecture_codes": sorted({region["prefecture_code"] for region in regions}),
        "region_count": len(regions),
        "pages_fetched": pages_fetched,
        "hotels": priced,
    }


def collect_prices(
    config: dict[str, Any],
    prefectures: list[tuple[str, str]],
    shard_index: int,
    shard_count: int,
    observed_date: date,
) -> dict[str, Any]:
    regions = discover_regions(config, prefectures)
    return collect_region_prices(config, regions, shard_index, shard_count, observed_date)


def _merge_hotels(target: dict[str, HotelPrice], values: list[HotelPrice]) -> None:
    for item in values:
        existing = target.get(item.hotel_id)
        if existing is None or (
            item.price_yen is not None
            and (existing.price_yen is None or item.price_yen < existing.price_yen)
        ):
            target[item.hotel_id] = item


def load_complete_shards(input_dir: Path, expected_count: int) -> tuple[date, dict[str, dict[str, Any]], int]:
    paths = sorted(input_dir.glob("shard-*.json"))
    if len(paths) != expected_count:
        raise RuntimeError(f"Expected {expected_count} shard files, found {len(paths)}")
    seen: set[int] = set()
    observed_dates: set[str] = set()
    merged: dict[str, dict[str, Any]] = {}
    total_pages = 0
    for path in paths:
        shard = load_json(path, {})
        if shard.get("schema_version") != SHARD_SCHEMA_VERSION:
            raise RuntimeError(f"Unsupported shard schema in {path}")
        if int(shard.get("shard_count", -1)) != expected_count:
            raise RuntimeError(f"Wrong shard_count in {path}")
        index = int(shard.get("shard_index", -1))
        if index in seen or not 0 <= index < expected_count:
            raise RuntimeError(f"Invalid or duplicate shard index {index}")
        seen.add(index)
        observed_dates.add(str(shard.get("observed_date_jst", "")))
        total_pages += int(shard.get("pages_fetched", 0))
        shard_hotels = shard.get("hotels")
        if not isinstance(shard_hotels, dict):
            raise RuntimeError(f"Invalid hotel prices in {path}")
        for hotel_id, item in shard_hotels.items():
            if hotel_id in merged:
                raise RuntimeError(f"Hotel {hotel_id} appears in multiple shards")
            merged[hotel_id] = item
    if seen != set(range(expected_count)) or len(observed_dates) != 1:
        raise RuntimeError("Shard set is incomplete or contains mixed dates")
    if not merged:
        raise RuntimeError("Complete shard set contained no hotel prices")
    return date.fromisoformat(observed_dates.pop()), merged, total_pages


def load_price_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": STATE_SCHEMA_VERSION, "hotels": {}, "notified": {}}
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            value = json.load(stream)
        if not isinstance(value, dict):
            raise ValueError(f"{path} must contain a JSON object")
        return value
    return load_json(path, {})


def atomic_write_price_state(path: Path, value: dict[str, Any]) -> None:
    if path.suffix != ".gz":
        atomic_write_json(path, value)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(fd)
    try:
        with gzip.open(tmp_name, "wt", encoding="utf-8", compresslevel=9) as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def export_facilities_csv(state: dict[str, Any], output_path: Path) -> int:
    hotels = state.get("hotels", {})
    if not isinstance(hotels, dict):
        raise ValueError("Price state does not contain a hotels object")
    rows: list[dict[str, Any]] = []
    for hotel_id, hotel in hotels.items():
        valid_samples: list[tuple[str, int]] = []
        for sample in hotel.get("prices", []):
            try:
                sample_date = date.fromisoformat(str(sample["date"])).isoformat()
                sample_price = int(sample["price_yen"])
            except (KeyError, TypeError, ValueError):
                continue
            if sample_price > 0:
                valid_samples.append((sample_date, sample_price))
        valid_samples.sort()
        rows.append(
            {
                "hotel_id": str(hotel_id),
                "hotel_name": str(hotel.get("hotel_name", "")),
                "prefecture": str(hotel.get("prefecture", "")),
                "area": str(hotel.get("area", "")),
                "detail_url": str(hotel.get("detail_url", "")),
                "first_observed_date": valid_samples[0][0] if valid_samples else "",
                "last_observed_date": valid_samples[-1][0] if valid_samples else "",
                "latest_daily_min_yen": valid_samples[-1][1] if valid_samples else "",
                "recorded_30day_min_yen": min((price for _, price in valid_samples), default=""),
                "observation_days": len(valid_samples),
            }
        )
    rows.sort(key=lambda row: (row["prefecture"], row["area"], row["hotel_name"], row["hotel_id"]))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "hotel_id",
        "hotel_name",
        "prefecture",
        "area",
        "detail_url",
        "first_observed_date",
        "last_observed_date",
        "latest_daily_min_yen",
        "recorded_30day_min_yen",
        "observation_days",
    ]
    with output_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def find_anomalies(
    observations: dict[str, dict[str, Any]],
    state: dict[str, Any],
    observed_date: date,
    history_days: int,
    drop_rate_threshold: float,
) -> list[PriceAnomaly]:
    anomalies: list[PriceAnomaly] = []
    state_hotels = state.get("hotels", {})
    earliest = observed_date - timedelta(days=history_days)
    for hotel_id, current in observations.items():
        saved = state_hotels.get(hotel_id, {}) if isinstance(state_hotels, dict) else {}
        prices_by_date: dict[date, int] = {}
        for sample in saved.get("prices", []):
            try:
                sample_date = date.fromisoformat(str(sample["date"]))
                sample_price = int(sample["price_yen"])
            except (KeyError, TypeError, ValueError):
                continue
            if earliest <= sample_date < observed_date and sample_price > 0:
                previous = prices_by_date.get(sample_date)
                prices_by_date[sample_date] = sample_price if previous is None else min(previous, sample_price)
        if not prices_by_date:
            continue
        baseline = min(prices_by_date.values())
        current_price = int(current["price_yen"])
        drop_rate = 1 - (current_price / baseline)
        if drop_rate + 1e-12 < drop_rate_threshold:
            continue
        anomalies.append(
            PriceAnomaly(
                hotel_id=hotel_id,
                hotel_name=str(current.get("hotel_name", "")),
                prefecture=str(current.get("prefecture", "")),
                area=str(current.get("area", "")),
                price_yen=current_price,
                historical_min_yen=baseline,
                drop_rate=drop_rate,
                sample_count=len(prices_by_date),
                detail_url=str(current.get("detail_url", "")),
            )
        )
    return sorted(anomalies, key=lambda item: (-item.drop_rate, item.price_yen, item.hotel_id))


def count_comparable_hotels(
    observations: dict[str, dict[str, Any]],
    state: dict[str, Any],
    observed_date: date,
    history_days: int,
) -> int:
    state_hotels = state.get("hotels", {})
    if not isinstance(state_hotels, dict):
        return 0
    earliest = observed_date - timedelta(days=history_days)
    count = 0
    for hotel_id in observations:
        saved = state_hotels.get(hotel_id, {})
        for sample in saved.get("prices", []):
            try:
                sample_date = date.fromisoformat(str(sample["date"]))
                sample_price = int(sample["price_yen"])
            except (KeyError, TypeError, ValueError):
                continue
            if earliest <= sample_date < observed_date and sample_price > 0:
                count += 1
                break
    return count


def update_history(
    state: dict[str, Any],
    observations: dict[str, dict[str, Any]],
    observed_date: date,
    history_days: int,
) -> dict[str, Any]:
    hotels = state.get("hotels")
    if not isinstance(hotels, dict):
        hotels = {}
    for hotel_id, current in observations.items():
        saved = hotels.get(hotel_id, {})
        by_date: dict[str, int] = {}
        for sample in saved.get("prices", []):
            try:
                by_date[str(sample["date"])] = int(sample["price_yen"])
            except (KeyError, TypeError, ValueError):
                continue
        day = observed_date.isoformat()
        current_price = int(current["price_yen"])
        by_date[day] = min(by_date.get(day, current_price), current_price)
        retained = sorted(by_date.items())[-history_days:]
        hotels[hotel_id] = {
            "hotel_name": str(current.get("hotel_name", "")),
            "prefecture": str(current.get("prefecture", "")),
            "area": str(current.get("area", "")),
            "detail_url": str(current.get("detail_url", "")),
            "prices": [{"date": day, "price_yen": price} for day, price in retained],
        }
    stale_before = observed_date - timedelta(days=history_days * 2)
    for hotel_id, saved in list(hotels.items()):
        sample_dates = []
        for sample in saved.get("prices", []):
            try:
                sample_dates.append(date.fromisoformat(str(sample["date"])))
            except (KeyError, TypeError, ValueError):
                continue
        if not sample_dates or max(sample_dates) < stale_before:
            del hotels[hotel_id]
    state.update(
        {
            "schema_version": STATE_SCHEMA_VERSION,
            "history_days": history_days,
            "last_observed_date_jst": observed_date.isoformat(),
            "hotels": hotels,
        }
    )
    if not isinstance(state.get("notified"), dict):
        state["notified"] = {}
    return state


def parse_drop_rate_thresholds(config: dict[str, Any]) -> tuple[float, ...]:
    raw = config.get("hotel_price_drop_rates")
    if raw is None:
        legacy = config.get("hotel_price_drop_rate")
        raw = [legacy] if legacy is not None else list(DEFAULT_DROP_RATE_THRESHOLDS)
    if not isinstance(raw, list) or not raw:
        raise ValueError("hotel_price_drop_rates must be a non-empty list")
    thresholds = tuple(sorted({float(value) for value in raw}))
    if any(not 0 < value < 1 for value in thresholds):
        raise ValueError("hotel_price_drop_rates values must be between 0 and 1")
    return thresholds


def drop_rate_tier(drop_rate: float, thresholds: tuple[float, ...]) -> float:
    eligible = [value for value in thresholds if drop_rate + 1e-12 >= value]
    if not eligible:
        raise ValueError("drop rate does not reach a configured threshold")
    return max(eligible)


def count_anomaly_tiers(
    anomalies: list[PriceAnomaly], thresholds: tuple[float, ...]
) -> dict[int, int]:
    counts = {round(value * 100): 0 for value in thresholds}
    for item in anomalies:
        counts[round(drop_rate_tier(item.drop_rate, thresholds) * 100)] += 1
    return counts


def _anomaly_embed(item: PriceAnomaly, tier_percent: int) -> dict[str, Any]:
    return {
        "title": f"ホテル料金の異常値｜{item.hotel_name}"[:256],
        "url": item.detail_url,
        "color": 0xE53935,
        "fields": [
            {"name": "エリア", "value": f"{item.prefecture} / {item.area or '不明'}", "inline": False},
            {"name": "現在の表示最低料金", "value": f"{item.price_yen:,}円", "inline": True},
            {"name": "直近30日の記録済み最低価格", "value": f"{item.historical_min_yen:,}円", "inline": True},
            {"name": "下落率", "value": f"{item.drop_rate:.1%}", "inline": True},
            {"name": "通知区分", "value": f"{tier_percent}%以上下落", "inline": True},
        ],
        "footer": {"text": "日付未定・1泊・1室・大人2名。予約前にじゃらん公式で条件と料金を確認してください"},
    }


def notify_anomalies(
    webhook_url: str,
    anomalies: list[PriceAnomaly],
    thresholds: tuple[float, ...],
) -> None:
    grouped: dict[int, list[PriceAnomaly]] = {}
    for item in anomalies:
        tier_percent = round(drop_rate_tier(item.drop_rate, thresholds) * 100)
        grouped.setdefault(tier_percent, []).append(item)
    for tier_percent in sorted(grouped, reverse=True):
        values = grouped[tier_percent]
        for offset in range(0, len(values), 5):
            chunk = values[offset : offset + 5]
            post_discord(
                webhook_url,
                {
                    "content": (
                        f"記録済み最低価格より{tier_percent}%以上安いホテルを"
                        f"{len(chunk)}件検知しました。"
                    ),
                    "username": "じゃらんホテル料金監視",
                    "embeds": [_anomaly_embed(item, tier_percent) for item in chunk],
                },
            )


def notify_scan_summary(
    webhook_url: str,
    completed_at: datetime,
    hotel_count: int,
    page_count: int,
    shard_count: int,
    comparable_count: int,
    anomaly_count: int,
    notified_count: int,
    tier_counts: dict[int, int],
) -> None:
    tier_fields = [
        {
            "name": f"{percent}-{next_percent - 1}%下落" if next_percent else f"{percent}%以上下落",
            "value": f"{tier_counts.get(percent, 0):,}件",
            "inline": True,
        }
        for percent, next_percent in zip(
            sorted(tier_counts),
            [*sorted(tier_counts)[1:], None],
        )
    ]
    post_discord(
        webhook_url,
        {
            "content": f"全国ホテル料金チェック完了：60%以上下落 {anomaly_count:,}件",
            "username": "じゃらんホテル料金監視",
            "embeds": [
                {
                    "title": "3時間ごとの全国チェック結果",
                    "color": 0xE53935 if anomaly_count else 0x1976D2,
                    "fields": [
                        {"name": "確認時刻", "value": completed_at.strftime("%Y-%m-%d %H:%M JST"), "inline": True},
                        {"name": "取得宿数", "value": f"{hotel_count:,}軒", "inline": True},
                        {"name": "比較できた宿", "value": f"{comparable_count:,}軒", "inline": True},
                        {"name": "大幅下落", "value": f"{anomaly_count:,}件", "inline": True},
                        {"name": "新規通知", "value": f"{notified_count:,}件", "inline": True},
                        *tier_fields,
                        {"name": "取得ページ数", "value": f"{page_count:,}ページ", "inline": True},
                        {"name": "収集Bot", "value": f"{shard_count}分割すべて完了", "inline": True},
                    ],
                    "footer": {"text": "日付未定・1泊・1室・大人2名。直近30日の記録済み最低価格と比較"},
                }
            ],
        },
    )


def run_prepare(args: argparse.Namespace, config: dict[str, Any]) -> int:
    observed_date = date.fromisoformat(args.observed_date) if args.observed_date else datetime.now(JST).date()
    payload = build_region_manifest(config, observed_date)
    atomic_write_json(Path(args.output), payload)
    print(f"Prepared {len(payload['regions'])} nationwide regions for 100 collectors.", flush=True)
    return 0


def run_export(args: argparse.Namespace, config: dict[str, Any]) -> int:
    state_path = Path(args.state or config.get("hotel_price_state_file", "data/hotel_price_state.json.gz"))
    output_path = Path(args.output)
    count = export_facilities_csv(load_price_state(state_path), output_path)
    print(f"Exported {count} hotel facilities to {output_path}.", flush=True)
    return 0


def run_collect(args: argparse.Namespace, config: dict[str, Any]) -> int:
    if args.prefecture:
        selected = [item for item in PREFECTURES if item[0] == args.prefecture]
        if not selected:
            raise ValueError(f"Unknown prefecture code: {args.prefecture}")
        observed_date = date.fromisoformat(args.observed_date) if args.observed_date else datetime.now(JST).date()
        payload = collect_prices(config, selected, args.shard_index, args.shard_count, observed_date)
    else:
        if not args.manifest:
            raise ValueError("--manifest is required unless --prefecture is used")
        observed_date, regions = load_region_manifest(Path(args.manifest))
        assigned = shard_regions(regions, args.shard_index, args.shard_count)
        payload = collect_region_prices(config, assigned, args.shard_index, args.shard_count, observed_date)
    atomic_write_json(Path(args.output), payload)
    print(
        f"Collected {len(payload['hotels'])} priced hotels from {payload['pages_fetched']} pages "
        f"for shard {args.shard_index}/{args.shard_count - 1}.",
        flush=True,
    )
    return 0


def run_aggregate(args: argparse.Namespace, config: dict[str, Any]) -> int:
    expected_count = max(int(config.get("hotel_price_parallel_shards", 100)), 1)
    history_days = max(int(config.get("hotel_price_history_days", 30)), 1)
    thresholds = parse_drop_rate_thresholds(config)
    observed_date, observations, total_pages = load_complete_shards(Path(args.input_dir), expected_count)
    state_path = Path(args.state or config.get("hotel_price_state_file", "data/hotel_price_state.json.gz"))
    state = load_price_state(state_path)
    anomalies = find_anomalies(observations, state, observed_date, history_days, min(thresholds))
    tier_counts = count_anomaly_tiers(anomalies, thresholds)
    comparable_count = count_comparable_hotels(observations, state, observed_date, history_days)
    notified = state.get("notified", {})
    if not isinstance(notified, dict):
        notified = {}
    pending = []
    for item in anomalies:
        signature = f"{observed_date.isoformat()}:{item.price_yen}:{item.historical_min_yen}"
        if notified.get(item.hotel_id) != signature:
            pending.append(item)

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not args.dry_run and not webhook_url:
        raise RuntimeError("DISCORD_WEBHOOK_URL is required for scan summaries")
    if pending and not args.dry_run:
        notify_anomalies(webhook_url, pending, thresholds)
        for item in pending:
            notified[item.hotel_id] = f"{observed_date.isoformat()}:{item.price_yen}:{item.historical_min_yen}"
    completed_at = datetime.now(JST)
    if not args.dry_run:
        notify_scan_summary(
            webhook_url,
            completed_at,
            len(observations),
            total_pages,
            expected_count,
            comparable_count,
            len(anomalies),
            len(pending),
            tier_counts,
        )
    state["notified"] = notified
    update_history(state, observations, observed_date, history_days)
    state["last_run"] = {
        "observed_date_jst": observed_date.isoformat(),
        "completed_at_jst": completed_at.isoformat(),
        "hotel_count": len(observations),
        "pages_fetched": total_pages,
        "anomaly_count": len(anomalies),
        "anomaly_counts_by_drop_rate": {str(key): value for key, value in tier_counts.items()},
        "notified_count": 0 if args.dry_run else len(pending),
        "comparable_hotel_count": comparable_count,
        "summary_notified": not args.dry_run,
    }
    if not args.dry_run:
        atomic_write_price_state(state_path, state)
    print(
        f"Aggregated {len(observations)} hotels from {total_pages} pages; "
        f"{len(anomalies)} anomalies; {len(pending)} pending notifications; dry_run={args.dry_run}.",
        flush=True,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare", help="Discover nationwide regions")
    prepare.add_argument("--config", default="config.example.json")
    prepare.add_argument("--output", required=True)
    prepare.add_argument("--observed-date", help="Override JST date (YYYY-MM-DD) for tests")
    export = subparsers.add_parser("export", help="Export the stored facility list as CSV")
    export.add_argument("--config", default="config.example.json")
    export.add_argument("--state")
    export.add_argument("--output", default="data/hotel_facilities.csv")
    collect = subparsers.add_parser("collect", help="Collect one region shard")
    collect.add_argument("--config", default="config.example.json")
    collect.add_argument("--shard-index", type=int, required=True)
    collect.add_argument("--shard-count", type=int, required=True)
    collect.add_argument("--output", required=True)
    collect.add_argument("--manifest", help="Region manifest produced by the prepare command")
    collect.add_argument("--prefecture", help="Restrict a local test to one six-digit prefecture code")
    collect.add_argument("--observed-date", help="Override JST date (YYYY-MM-DD) for tests")
    aggregate = subparsers.add_parser("aggregate", help="Merge shards, compare history, and notify")
    aggregate.add_argument("--config", default="config.example.json")
    aggregate.add_argument("--input-dir", required=True)
    aggregate.add_argument("--state")
    aggregate.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = load_json(Path(args.config), {})
    if args.command == "prepare":
        return run_prepare(args, config)
    if args.command == "export":
        return run_export(args, config)
    if args.command == "collect":
        return run_collect(args, config)
    return run_aggregate(args, config)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
