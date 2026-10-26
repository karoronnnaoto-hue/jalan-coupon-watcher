import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jalan_hotel_price_bot import (
    HotelPrice,
    build_region_page_url,
    collect_region_prices,
    count_anomaly_tiers,
    count_comparable_hotels,
    discover_regions,
    discover_region_urls,
    export_facilities_csv,
    find_anomalies,
    atomic_write_price_state,
    load_price_state,
    load_complete_shards,
    notify_anomalies,
    notify_scan_summary,
    parse_drop_rate_thresholds,
    parse_hotel_results,
    parse_result_count,
    shard_regions,
    update_history,
)


FIXTURE = """
<span class="jlnpc-listInformation--count">2</span>
<li id="sa_yadNo999999" class="p-yadoCassette p-yadoCassette--pr js-searchResultItem">
  <h2 class="js-yadName p-searchResultItem__facilityName">広告ホテル</h2>
  <span class="p-searchResultItem__lowestPriceValue">100円～</span>
</li>
<li id="yadNo123456" class="p-yadoCassette p-searchResultItem js-searchResultItem">
  <dd class="p-searchResultItem__areaValues">東京都 &gt; 新宿</dd>
  <h2 class="js-yadName p-searchResultItem__facilityName"><a>テストホテル</a></h2>
  <span class="p-searchResultItem__lowestPriceValue">19,000円～</span>
</li>
<li id="yadNo234567" class="p-yadoCassette p-searchResultItem js-searchResultItem">
  <dd class="p-searchResultItem__areaValues">東京都 &gt; 渋谷</dd>
  <h2 class="js-yadName p-searchResultItem__facilityName">料金未定ホテル</h2>
</li>
"""


def observation(price: int = 1000) -> dict[str, dict]:
    return {
        "123456": {
            "hotel_id": "123456",
            "hotel_name": "テストホテル",
            "prefecture": "東京都",
            "area": "東京都 > 新宿",
            "price_yen": price,
            "detail_url": "https://www.jalan.net/yad123456/",
        }
    }


def history_state(today: date, days: int, price: int = 10000) -> dict:
    return {
        "hotels": {
            "123456": {
                "prices": [
                    {"date": (today - timedelta(days=offset)).isoformat(), "price_yen": price}
                    for offset in range(1, days + 1)
                ]
            }
        }
    }


class HotelPriceTests(unittest.TestCase):
    def test_regular_cards_are_parsed_and_pr_is_ignored(self):
        hotels = parse_hotel_results(FIXTURE, "東京都")
        self.assertEqual([item.hotel_id for item in hotels], ["123456", "234567"])
        self.assertEqual(hotels[0].hotel_name, "テストホテル")
        self.assertEqual(hotels[0].area, "東京都 > 新宿")
        self.assertEqual(hotels[0].price_yen, 19000)
        self.assertIsNone(hotels[1].price_yen)

    def test_result_count_and_region_discovery(self):
        self.assertEqual(parse_result_count(FIXTURE), 2)
        landing = '<a href="/130000/LRG_136200/">A</a><a href="/130000/LRG_136200/">A</a><a href="/130000/LRG_136500/">B</a>'
        self.assertEqual(
            discover_region_urls(landing, "130000"),
            ["https://www.jalan.net/130000/LRG_136200/", "https://www.jalan.net/130000/LRG_136500/"],
        )

    def test_region_discovery_retries_incomplete_landing_page(self):
        config = {
            "request_retry_count": 2,
            "retry_delay_seconds": 1,
            "hotel_price_request_delay_seconds": 0.5,
            "hotel_price_max_regions_per_prefecture": 40,
        }
        valid = '<a href="/330000/LRG_330200/">岡山・玉野</a>'
        with (
            patch("jalan_hotel_price_bot._fetch_text_url", side_effect=[("", "<html></html>"), ("", valid)]),
            patch("jalan_hotel_price_bot.time.sleep"),
        ):
            regions = discover_regions(config, [("330000", "岡山県")])
        self.assertEqual(regions[0]["region_url"], "https://www.jalan.net/330000/LRG_330200/")

    def test_region_discovery_retries_transient_fetch_failure(self):
        config = {
            "request_retry_count": 2,
            "retry_delay_seconds": 1,
            "hotel_price_request_delay_seconds": 0.5,
            "hotel_price_max_regions_per_prefecture": 40,
        }
        valid = '<a href="/030000/LRG_030100/">盛岡</a>'
        with (
            patch(
                "jalan_hotel_price_bot._fetch_text_url",
                side_effect=[RuntimeError("HTTP Error 503"), ("", valid)],
            ) as fetch,
            patch("jalan_hotel_price_bot.time.sleep"),
        ):
            regions = discover_regions(config, [("030000", "岩手県")])
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(regions[0]["region_url"], "https://www.jalan.net/030000/LRG_030100/")

    def test_region_discovery_uses_last_successful_manifest_when_live_page_is_empty(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            fallback_path = Path(directory) / "regions.json"
            fallback_path.write_text(
                json.dumps(
                    {
                        "regions": [
                            {
                                "prefecture_code": "100000",
                                "prefecture_name": "群馬県",
                                "region_url": "https://www.jalan.net/100000/LRG_101400/",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            config = {
                "request_retry_count": 2,
                "retry_delay_seconds": 1,
                "hotel_price_request_delay_seconds": 0.5,
                "hotel_price_max_regions_per_prefecture": 40,
                "hotel_price_region_fallback_file": str(fallback_path),
            }
            with (
                patch("jalan_hotel_price_bot._fetch_text_url", return_value=("", "<html></html>")) as fetch,
                patch("jalan_hotel_price_bot.time.sleep"),
            ):
                regions = discover_regions(config, [("100000", "群馬県")])
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(regions[0]["region_url"], "https://www.jalan.net/100000/LRG_101400/")

    def test_region_pagination_url(self):
        first = build_region_page_url("https://www.jalan.net/130000/LRG_136200/", 1)
        second = build_region_page_url("https://www.jalan.net/130000/LRG_136200/", 2)
        self.assertIn("LRG_136200/?", first)
        self.assertIn("LRG_136200/page2.html?", second)
        self.assertIn("adultNum=2", second)
        self.assertIn("dateUndecided=1", second)
        self.assertIn("activeSort=1", second)
        fallback = build_region_page_url("https://www.jalan.net/130000/LRG_136200/", 1, None)
        self.assertNotIn("activeSort", fallback)

    def test_one_hundred_shards_cover_every_region_once(self):
        regions = [
            {"prefecture_code": "130000", "prefecture_name": "東京都", "region_url": f"https://example/{index}/"}
            for index in range(237)
        ]
        assigned = [item for shard in range(100) for item in shard_regions(regions, shard, 100)]
        self.assertCountEqual(assigned, regions)
        self.assertEqual(len(assigned), len({item["region_url"] for item in assigned}))

    def test_region_scan_restarts_after_result_count_changes(self):
        hotel = HotelPrice(
            hotel_id="123456",
            hotel_name="テストホテル",
            prefecture="東京都",
            area="東京都 > 新宿",
            price_yen=1000,
            detail_url="https://www.jalan.net/yad123456/",
        )
        config = {
            "hotel_price_region_retry_count": 2,
            "hotel_price_request_delay_seconds": 0.5,
            "hotel_price_max_pages_per_region": 50,
            "hotel_price_max_pages_per_shard": 50,
        }
        regions = [
            {
                "prefecture_code": "130000",
                "prefecture_name": "東京都",
                "region_url": "https://www.jalan.net/130000/LRG_136200/",
            }
        ]
        with (
            patch(
                "jalan_hotel_price_bot._collect_region_once",
                side_effect=[RuntimeError("count changed"), (1, 1, [hotel])],
            ) as collect_once,
            patch("jalan_hotel_price_bot.time.sleep"),
        ):
            result = collect_region_prices(config, regions, 0, 1, date(2026, 10, 22))
        self.assertEqual(collect_once.call_count, 2)
        self.assertEqual(result["hotels"]["123456"]["price_yen"], 1000)

    def test_region_scan_unions_alternate_sort_results(self):
        first = HotelPrice("111111", "宿A", "東京都", "東京都 > 新宿", 1000, "https://example/a")
        second = HotelPrice("222222", "宿B", "東京都", "東京都 > 新宿", 2000, "https://example/b")
        config = {
            "hotel_price_region_retry_count": 2,
            "hotel_price_request_delay_seconds": 0.5,
            "hotel_price_max_pages_per_region": 50,
            "hotel_price_max_pages_per_shard": 50,
        }
        regions = [
            {
                "prefecture_code": "130000",
                "prefecture_name": "東京都",
                "region_url": "https://www.jalan.net/130000/LRG_136200/",
            }
        ]
        with (
            patch(
                "jalan_hotel_price_bot._collect_region_once",
                side_effect=[(2, 1, [first]), (2, 1, [second])],
            ) as collect_once,
            patch("jalan_hotel_price_bot.time.sleep"),
        ):
            result = collect_region_prices(config, regions, 0, 1, date(2026, 10, 22))
        self.assertEqual(set(result["hotels"]), {"111111", "222222"})
        self.assertEqual(collect_once.call_args_list[0].args[-1], "1")
        self.assertIsNone(collect_once.call_args_list[1].args[-1])

    def test_region_scan_accepts_one_hotel_unique_shortfall(self):
        hotel = HotelPrice("111111", "宿A", "大阪府", "大阪府 > 大阪市", 1000, "https://example/a")
        config = {
            "hotel_price_region_retry_count": 2,
            "hotel_price_max_unique_shortfall": 1,
            "hotel_price_request_delay_seconds": 0.5,
            "hotel_price_max_pages_per_region": 50,
            "hotel_price_max_pages_per_shard": 50,
        }
        regions = [
            {
                "prefecture_code": "270000",
                "prefecture_name": "大阪府",
                "region_url": "https://www.jalan.net/270000/LRG_272000/",
            }
        ]
        with (
            patch("jalan_hotel_price_bot._collect_region_once", return_value=(2, 1, [hotel])) as collect_once,
            patch("jalan_hotel_price_bot.time.sleep"),
        ):
            result = collect_region_prices(config, regions, 0, 1, date(2026, 10, 26))
        self.assertEqual(collect_once.call_count, 2)
        self.assertEqual(set(result["hotels"]), {"111111"})

    def test_region_scan_rejects_shortfall_above_limit(self):
        hotel = HotelPrice("111111", "宿A", "大阪府", "大阪府 > 大阪市", 1000, "https://example/a")
        config = {
            "hotel_price_region_retry_count": 2,
            "hotel_price_max_unique_shortfall": 1,
            "hotel_price_request_delay_seconds": 0.5,
            "hotel_price_max_pages_per_region": 50,
            "hotel_price_max_pages_per_shard": 50,
        }
        regions = [
            {
                "prefecture_code": "270000",
                "prefecture_name": "大阪府",
                "region_url": "https://www.jalan.net/270000/LRG_272000/",
            }
        ]
        with (
            patch("jalan_hotel_price_bot._collect_region_once", return_value=(3, 1, [hotel])),
            patch("jalan_hotel_price_bot.time.sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, "Region scan failed"):
                collect_region_prices(config, regions, 0, 1, date(2026, 10, 26))

    def test_detects_drop_below_recorded_minimum_after_one_sample(self):
        today = date(2026, 10, 20)
        state = history_state(today, 1)
        anomalies = find_anomalies(observation(), state, today, 30, 0.9)
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0].historical_min_yen, 10000)
        self.assertAlmostEqual(anomalies[0].drop_rate, 0.9)
        self.assertEqual(count_comparable_hotels(observation(), state, today, 30), 1)

    def test_classifies_anomalies_into_highest_reached_tier(self):
        today = date(2026, 10, 22)
        thresholds = parse_drop_rate_thresholds({"hotel_price_drop_rates": [0.6, 0.7, 0.8, 0.9]})
        anomalies = find_anomalies(observation(2000), history_state(today, 1, 14400), today, 30, min(thresholds))
        self.assertEqual(len(anomalies), 1)
        self.assertAlmostEqual(anomalies[0].drop_rate, 1 - (2000 / 14400))
        self.assertEqual(count_anomaly_tiers(anomalies, thresholds), {60: 0, 70: 0, 80: 1, 90: 0})

    def test_same_day_low_is_not_used_as_historical_baseline(self):
        today = date(2026, 10, 22)
        state = history_state(today, 1, 14400)
        state["hotels"]["123456"]["prices"].append(
            {"date": today.isoformat(), "price_yen": 2000}
        )
        anomalies = find_anomalies(observation(2000), state, today, 30, 0.6)
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0].historical_min_yen, 14400)

    def test_uses_lowest_recorded_price_and_ignores_expired_history(self):
        today = date(2026, 10, 20)
        state = history_state(today, 2, 10000)
        state["hotels"]["123456"]["prices"][0]["price_yen"] = 5000
        self.assertEqual(find_anomalies(observation(), state, today, 30, 0.9), [])
        expired = {
            "hotels": {
                "123456": {
                    "prices": [{"date": (today - timedelta(days=40)).isoformat(), "price_yen": 10000}]
                }
            }
        }
        self.assertEqual(find_anomalies(observation(), expired, today, 30, 0.9), [])
        self.assertEqual(count_comparable_hotels(observation(), expired, today, 30), 0)

    def test_history_keeps_same_day_minimum_and_thirty_days(self):
        today = date(2026, 10, 20)
        state = history_state(today + timedelta(days=1), 30)
        update_history(state, observation(900), today, 30)
        update_history(state, observation(800), today, 30)
        update_history(state, observation(850), today, 30)
        prices = state["hotels"]["123456"]["prices"]
        self.assertEqual(len(prices), 30)
        self.assertEqual(prices[-1], {"date": today.isoformat(), "price_yen": 800})

    def test_complete_shards_are_required(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            for index in range(2):
                payload = {
                    "schema_version": 1,
                    "observed_date_jst": "2026-10-20",
                    "shard_index": index,
                    "shard_count": 2,
                    "pages_fetched": 3,
                    "hotels": {str(index): {"price_yen": 1000}},
                }
                (root / f"shard-{index}.json").write_text(json.dumps(payload), encoding="utf-8")
            observed, hotels, pages = load_complete_shards(root, 2)
            self.assertEqual(observed, date(2026, 10, 20))
            self.assertEqual(len(hotels), 2)
            self.assertEqual(pages, 6)
            (root / "shard-1.json").unlink()
            with self.assertRaises(RuntimeError):
                load_complete_shards(root, 2)

    def test_gzip_state_round_trip(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "state.json.gz"
            expected = {"schema_version": 1, "hotels": {"1": {"hotel_name": "宿"}}}
            atomic_write_price_state(path, expected)
            self.assertEqual(load_price_state(path), expected)

    def test_scan_summary_reports_counts(self):
        with patch("jalan_hotel_price_bot.post_discord") as post:
            notify_scan_summary(
                "https://example.test/webhook",
                datetime(2026, 10, 20, 21, 17, tzinfo=timezone(timedelta(hours=9))),
                23451,
                991,
                100,
                23000,
                2,
                2,
                {60: 0, 70: 0, 80: 2, 90: 0},
            )
        payload = post.call_args.args[1]
        self.assertIn("60%以上下落 2件", payload["content"])
        fields = payload["embeds"][0]["fields"]
        self.assertIn({"name": "取得宿数", "value": "23,451軒", "inline": True}, fields)
        self.assertIn({"name": "比較できた宿", "value": "23,000軒", "inline": True}, fields)
        self.assertIn({"name": "80-89%下落", "value": "2件", "inline": True}, fields)

    def test_anomaly_notifications_are_grouped_by_tier(self):
        today = date(2026, 10, 22)
        thresholds = (0.6, 0.7, 0.8, 0.9)
        anomalies = find_anomalies(observation(2000), history_state(today, 1, 14400), today, 30, 0.6)
        with patch("jalan_hotel_price_bot.post_discord") as post:
            notify_anomalies("https://example.test/webhook", anomalies, thresholds)
        payload = post.call_args.args[1]
        self.assertIn("80%以上安いホテルを1件", payload["content"])
        self.assertIn(
            {"name": "通知区分", "value": "80%以上下落", "inline": True},
            payload["embeds"][0]["fields"],
        )

    def test_export_facilities_csv(self):
        state = {
            "hotels": {
                "123456": {
                    "hotel_name": "テストホテル",
                    "prefecture": "東京都",
                    "area": "東京都 > 新宿",
                    "detail_url": "https://www.jalan.net/yad123456/",
                    "prices": [
                        {"date": "2026-10-20", "price_yen": 12000},
                        {"date": "2026-10-21", "price_yen": 9000},
                    ],
                }
            }
        }
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            output = Path(directory) / "hotels.csv"
            self.assertEqual(export_facilities_csv(state, output), 1)
            text = output.read_text(encoding="utf-8-sig")
        self.assertIn("hotel_id,hotel_name,prefecture", text)
        self.assertIn("123456,テストホテル,東京都", text)
        self.assertIn("2026-10-20,2026-10-21,9000,9000,2", text)


if __name__ == "__main__":
    unittest.main()
