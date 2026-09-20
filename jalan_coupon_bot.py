#!/usr/bin/env python3
"""Watch public Jalan coupon listings and notify Discord about new bargains.

This tool only reads public listing pages. It does not log in, acquire coupons,
or automate reservations.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen


BASE_URL = "https://www.jalan.net"
LISTING_URL = f"{BASE_URL}/jalancponsum/zenkoku/"
LISTING_PARAMS = {
    "screenId": "UWW7862",
    "searchType": "2",
    "stayCount": "1",
    "couponPriceMin": "0",
    "couponPriceMax": "999999",
    "priceMax": "999999",
    "activeSort": "2",
}
DEFAULT_USER_AGENT = "JalanCouponWatcher/1.0 (+personal, low-frequency monitor)"
STATE_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class Coupon:
    coupon_id: str
    yad_no: str
    title: str
    hotel: str
    area: str
    distribution_period: str
    reservation_period: str
    stay_period: str
    minimum_reservation_yen: int | None
    discount_yen: int
    detail_url: str
    calendar_url: str

    @property
    def discount_rate(self) -> float | None:
        if not self.minimum_reservation_yen:
            return None
        return self.discount_yen / self.minimum_reservation_yen


def _classes(attrs: list[tuple[str, str | None]]) -> set[str]:
    value = dict(attrs).get("class") or ""
    return set(value.split())


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _yen(text: str) -> int | None:
    match = re.search(r"([0-9０-９,，]+)\s*円", text)
    if not match:
        return None
    normalized = match.group(1).translate(str.maketrans("０１２３４５６７８９，", "0123456789,"))
    return int(normalized.replace(",", ""))


class JalanCouponParser(HTMLParser):
    """Small parser scoped to Jalan's public coupon cassette markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.coupons: list[Coupon] = []
        self._item: dict[str, Any] | None = None
        self._li_depth = 0
        self._capture: str | None = None
        self._buffer: list[str] = []
        self._label = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        if tag == "li":
            if self._item is not None:
                self._li_depth += 1
            elif "item" in _classes(attrs):
                self._item = {"fields": {}, "coupon_id": "", "yad_no": "", "calendar_url": ""}
                self._li_depth = 1
            return

        if self._item is None:
            return

        if tag == "h2" and "item-title" in _classes(attrs):
            self._start_capture("title")
        elif tag == "dt":
            self._start_capture("dt")
        elif tag == "dd":
            self._start_capture("dd")
        elif tag == "img" and not self._item.get("hotel"):
            alt = _clean(attrs_dict.get("alt") or "")
            if alt:
                self._item["hotel"] = alt
        elif tag == "a":
            href = attrs_dict.get("href") or ""
            match = re.search(r"doPromotionDtl\(['\"]([^'\"]+)['\"],['\"]([^'\"]+)['\"]\)", href)
            if match:
                self._item["coupon_id"], self._item["yad_no"] = match.groups()
            elif "uww7832.do" in href:
                self._item["calendar_url"] = urljoin(BASE_URL, href)

    def handle_data(self, data: str) -> None:
        if self._item is not None and self._capture:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._item is None:
            return

        if tag == "h2" and self._capture == "title":
            self._item["title"] = self._finish_capture()
        elif tag == "dt" and self._capture == "dt":
            self._label = self._finish_capture()
        elif tag == "dd" and self._capture == "dd":
            value = self._finish_capture()
            if self._label:
                self._item["fields"][self._label] = value
            self._label = ""
        elif tag == "li":
            self._li_depth -= 1
            if self._li_depth == 0:
                self._finish_item()

    def _start_capture(self, kind: str) -> None:
        self._capture = kind
        self._buffer = []

    def _finish_capture(self) -> str:
        value = _clean("".join(self._buffer))
        self._capture = None
        self._buffer = []
        return value

    def _finish_item(self) -> None:
        assert self._item is not None
        fields = self._item["fields"]
        coupon_id = self._item.get("coupon_id", "")
        yad_no = self._item.get("yad_no", "")
        discount = _yen(fields.get("クーポン額", ""))
        hotel = self._item.get("hotel") or fields.get("宿名", "") or fields.get("施設名", "")

        if coupon_id and discount is not None:
            detail_query = urlencode({"discountCouponId": coupon_id, "yadNo": yad_no})
            self.coupons.append(
                Coupon(
                    coupon_id=coupon_id,
                    yad_no=yad_no,
                    title=self._item.get("title", ""),
                    hotel=hotel,
                    area=fields.get("エリア", ""),
                    distribution_period=fields.get("配布期間", ""),
                    reservation_period=fields.get("予約期間", ""),
                    stay_period=fields.get("宿泊対象期間", fields.get("宿泊期間", "")),
                    minimum_reservation_yen=_yen(fields.get("予約金額", "")),
                    discount_yen=discount,
                    detail_url=f"{BASE_URL}/uw/uwp7800/uww7830.do?{detail_query}",
                    calendar_url=self._item.get("calendar_url", ""),
                )
            )
        self._item = None
        self._capture = None
        self._buffer = []
        self._label = ""


def parse_coupons(page_html: str) -> list[Coupon]:
    parser = JalanCouponParser()
    parser.feed(page_html)
    return parser.coupons


def coupon_key(coupon: Coupon) -> str:
    return f"{coupon.coupon_id}:{coupon.yad_no}"


def build_listing_url(page: int) -> str:
    params = {**LISTING_PARAMS, "pageIdx": max(1, page)}
    return f"{LISTING_URL}?{urlencode(params)}"


def parse_total_results(page_html: str) -> int:
    decoded = html.unescape(page_html)
    match = re.search(r"[（(]\s*([0-9０-９,，]+)\s*件中\s*[）)]", decoded)
    if not match:
        raise RuntimeError("Could not determine the total coupon count from the listing page")
    normalized = match.group(1).translate(str.maketrans("０１２３４５６７８９，", "0123456789,"))
    return int(normalized.replace(",", ""))


def fetch_page(page: int, timeout: int, user_agent: str) -> str:
    url = build_listing_url(page)
    request = Request(url, headers={"User-Agent": user_agent, "Accept-Language": "ja"})
    with urlopen(request, timeout=timeout) as response:
        raw = response.read()
    # Jalan currently declares Shift_JIS; cp932 covers the common extensions.
    return raw.decode("cp932", errors="replace")


def _fetch_parsed_page(
    page: int,
    timeout: int,
    user_agent: str,
    attempts: int,
    retry_delay: float,
) -> tuple[str, list[Coupon]]:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            page_html = fetch_page(page, timeout, user_agent)
            parsed = parse_coupons(page_html)
            if parsed:
                return page_html, parsed
            last_error = RuntimeError(f"Page {page} contained no coupon entries")
        except (HTTPError, URLError, OSError) as exc:
            last_error = exc
        if attempt + 1 < attempts:
            time.sleep(retry_delay)
    raise RuntimeError(f"Failed to read coupon page {page} after {attempts} attempts: {last_error}")


def fetch_coupons(config: dict[str, Any]) -> list[Coupon]:
    delay = max(float(config.get("request_delay_seconds", 2.0)), 1.0)
    timeout = max(int(config.get("request_timeout_seconds", 20)), 5)
    user_agent = str(config.get("user_agent") or DEFAULT_USER_AGENT)
    attempts = max(1, min(int(config.get("request_retry_count", 3)), 5))
    retry_delay = max(float(config.get("retry_delay_seconds", 3.0)), 1.0)
    max_pages = max(1, int(config.get("max_pages_to_scan", 100)))
    collected: dict[str, Coupon] = {}

    first_html, first_page = _fetch_parsed_page(1, timeout, user_agent, attempts, retry_delay)
    total_results = parse_total_results(first_html)
    pages = max(1, math.ceil(total_results / len(first_page)))
    page_override = int(config.get("pages_to_scan", 0))
    if page_override > 0:
        pages = min(pages, page_override)
    elif pages > max_pages:
        raise RuntimeError(f"Listing requires {pages} pages, above max_pages_to_scan={max_pages}")

    for coupon in first_page:
        collected[coupon_key(coupon)] = coupon
    for page in range(2, pages + 1):
        time.sleep(delay)
        _, parsed = _fetch_parsed_page(page, timeout, user_agent, attempts, retry_delay)
        for coupon in parsed:
            collected[coupon_key(coupon)] = coupon
    return list(collected.values())


def matches(coupon: Coupon, config: dict[str, Any]) -> bool:
    min_amount = int(config.get("minimum_coupon_yen", 5000))
    min_rate = float(config.get("minimum_discount_rate", 0.50))
    amount_match = coupon.discount_yen >= min_amount
    rate_match = coupon.discount_rate is not None and coupon.discount_rate > min_rate
    match_mode = config.get("match_mode", "rate")
    if match_mode == "rate":
        return rate_match
    if match_mode == "all":
        return amount_match and rate_match
    return amount_match or rate_match


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _fmt_yen(value: int | None) -> str:
    return "不明" if value is None else f"{value:,}円"


def _embed(coupon: Coupon, anomaly_rate: float) -> dict[str, Any]:
    rate = coupon.discount_rate
    is_anomaly = rate is not None and rate >= anomaly_rate
    heading = "🚨 高割引率" if is_anomaly else "🎫 条件の良い新着"
    fields = [
        {"name": "宿泊施設", "value": coupon.hotel or "不明", "inline": False},
        {"name": "エリア", "value": coupon.area or "不明", "inline": True},
        {"name": "クーポン額", "value": _fmt_yen(coupon.discount_yen), "inline": True},
        {"name": "最低予約金額", "value": _fmt_yen(coupon.minimum_reservation_yen), "inline": True},
        {"name": "実質割引率", "value": "算出不可" if rate is None else f"{rate:.1%}", "inline": True},
        {"name": "配布期間", "value": coupon.distribution_period or "不明", "inline": False},
    ]
    if coupon.stay_period:
        fields.append({"name": "宿泊対象期間", "value": coupon.stay_period, "inline": False})
    return {
        "title": f"{heading}｜{coupon.title}"[:256],
        "url": coupon.detail_url,
        "color": 0xE53935 if is_anomaly else 0xFF7A00,
        "fields": fields,
        "footer": {"text": "取得・利用条件は必ずじゃらん公式ページで確認してください"},
    }


def post_discord(webhook_url: str, payload: dict[str, Any], timeout: int = 20) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    for attempt in range(3):
        request = Request(webhook_url, data=body, method="POST", headers={"Content-Type": "application/json", "User-Agent": DEFAULT_USER_AGENT})
        try:
            with urlopen(request, timeout=timeout) as response:
                response.read()
            return
        except HTTPError as exc:
            if exc.code != 429 or attempt == 2:
                raise
            retry_after = 1.0
            try:
                retry_after = float(json.loads(exc.read().decode("utf-8")).get("retry_after", 1.0))
                if retry_after > 100:
                    retry_after /= 1000
            except (ValueError, json.JSONDecodeError):
                pass
            time.sleep(max(retry_after, 1.0))


def notify_discord(webhook_url: str, coupons: list[Coupon], config: dict[str, Any]) -> None:
    anomaly_rate = float(config.get("anomaly_discount_rate", 0.50))
    # Discord allows up to 10 embeds. Five keeps each alert compact.
    for offset in range(0, len(coupons), 5):
        chunk = coupons[offset : offset + 5]
        post_discord(
            webhook_url,
            {
                "content": f"新着クーポンを{len(chunk)}件検知しました。",
                "username": "じゃらんクーポン監視",
                "embeds": [_embed(coupon, anomaly_rate) for coupon in chunk],
            },
        )


def print_coupons(coupons: Iterable[Coupon]) -> None:
    for coupon in coupons:
        rate = "不明" if coupon.discount_rate is None else f"{coupon.discount_rate:.1%}"
        print(
            f"[{coupon.coupon_id}] {coupon.hotel} | {coupon.discount_yen:,}円 | "
            f"最低 {_fmt_yen(coupon.minimum_reservation_yen)} | {rate}\n  {coupon.title}\n  {coupon.detail_url}"
        )


def run(args: argparse.Namespace) -> int:
    config_path = Path(args.config).resolve()
    config = load_json(config_path, {})
    if args.pages is not None:
        config["pages_to_scan"] = args.pages
    state_path = Path(args.state or config.get("state_file", "data/state.json"))
    if not state_path.is_absolute():
        state_path = config_path.parent / state_path

    webhook_url = os.getenv("DISCORD_WEBHOOK_URL") or config.get("discord_webhook_url", "")
    if args.test_webhook:
        if not webhook_url:
            raise ValueError("DISCORD_WEBHOOK_URL is not set")
        post_discord(webhook_url, {"content": "✅ じゃらんクーポン監視Botの接続テストに成功しました。"})
        print("Discord test message sent.")
        return 0

    coupons = fetch_coupons(config)
    selected = [coupon for coupon in coupons if matches(coupon, config)]
    if args.dry_run:
        print(f"Fetched {len(coupons)} coupons; {len(selected)} match the configured thresholds.")
        print_coupons(selected)
        return 0

    state = load_json(state_path, {"initialized": False, "seen": {}})
    schema_matches = state.get("schema_version") == STATE_SCHEMA_VERSION
    seen: dict[str, str] = state.setdefault("seen", {}) if schema_matches else {}
    now = datetime.now(timezone.utc).isoformat()
    is_first_run = not schema_matches or not bool(state.get("initialized"))
    new_coupons = [coupon for coupon in coupons if coupon_key(coupon) not in seen]
    candidates = [coupon for coupon in new_coupons if matches(coupon, config)]
    if is_first_run and not args.notify_existing:
        candidates = []

    if candidates:
        if not webhook_url:
            raise ValueError("Matching coupons found, but DISCORD_WEBHOOK_URL is not set")
        notify_discord(webhook_url, candidates, config)

    for coupon in coupons:
        key = coupon_key(coupon)
        seen[key] = seen.get(key, now)
    state["seen"] = seen
    state["schema_version"] = STATE_SCHEMA_VERSION
    max_seen = max(int(config.get("max_seen_ids", 10000)), 1000)
    if len(seen) > max_seen:
        state["seen"] = dict(sorted(seen.items(), key=lambda item: item[1], reverse=True)[:max_seen])
    state["initialized"] = True
    # Avoid a needless state commit on every GitHub Actions run.
    if config.get("write_run_metadata", False):
        state.update({"last_success_utc": now, "last_fetched_count": len(coupons)})
    atomic_write_json(state_path, state)
    print(f"Fetched {len(coupons)} coupons; {len(new_coupons)} new; {len(candidates)} notified.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Monitor new public Jalan coupons and notify Discord")
    parser.add_argument("--config", default="config.json", help="Path to JSON config (default: config.json)")
    parser.add_argument("--state", help="Override state file path")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and print matches without sending or saving")
    parser.add_argument("--notify-existing", action="store_true", help="Notify matching coupons on first run")
    parser.add_argument("--test-webhook", action="store_true", help="Send a Discord connection test")
    parser.add_argument("--pages", type=int, help="Temporarily override the number of listing pages to scan")
    return parser


def main() -> int:
    try:
        return run(build_parser().parse_args())
    except (HTTPError, URLError, OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
