import sys
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jalan_coupon_bot import (
    DEFAULT_USER_AGENT,
    build_snapshot,
    build_listing_url,
    compare_snapshots,
    coupon_key,
    daily_status_due,
    matches,
    notify_daily_status,
    parse_coupons,
    parse_total_results,
    post_discord,
)


FIXTURE = """
<ul class="cassetteList-list">
  <li class="item">
    <h2 class="item-title">9,000円テストクーポン</h2>
    <div class="item-photo"><img alt="テストホテル" src="x.jpg"></div>
    <dl>
      <dt>宿名</dt><dd>テストホテル</dd>
      <dt>エリア</dt><dd>東京</dd>
      <dt>配布期間</dt><dd>2026年9月19日〜2026年9月20日</dd>
      <dt>予約期間</dt><dd>2026年9月19日〜2026年9月20日</dd>
      <dt>宿泊対象期間</dt><dd><a href="//www.jalan.net/uw/uwp7800/uww7832.do?x=1">カレンダー</a></dd>
      <dt>予約金額</dt><dd>10,000円（税込）以上</dd>
      <dt>クーポン額</dt><dd><span>9,000円分</span></dd>
    </dl>
    <a href="javascript:doPromotionDtl('COU123','456')">詳細・クーポンGET</a>
  </li>
</ul>
"""


class ParserTests(unittest.TestCase):
    def test_parse_coupon(self):
        coupons = parse_coupons(FIXTURE)
        self.assertEqual(len(coupons), 1)
        coupon = coupons[0]
        self.assertEqual(coupon.coupon_id, "COU123")
        self.assertEqual(coupon.hotel, "テストホテル")
        self.assertEqual(coupon.minimum_reservation_yen, 10000)
        self.assertEqual(coupon.discount_yen, 9000)
        self.assertAlmostEqual(coupon.discount_rate, 0.9)
        self.assertIn("discountCouponId=COU123", coupon.detail_url)

    def test_thresholds(self):
        coupon = parse_coupons(FIXTURE)[0]
        config = {"minimum_discount_rate": 0.5, "match_mode": "rate"}
        self.assertTrue(matches(coupon, config))
        self.assertFalse(matches(replace(coupon, discount_yen=5000), config))
        self.assertFalse(matches(replace(coupon, discount_yen=4900), config))

    def test_listing_url_preserves_search_filters(self):
        url = build_listing_url(7)
        self.assertIn("screenId=UWW7862", url)
        self.assertIn("searchType=2", url)
        self.assertIn("couponPriceMin=0", url)
        self.assertIn("activeSort=2", url)
        self.assertIn("pageIdx=7", url)

    def test_total_count_and_coupon_key(self):
        coupon = parse_coupons(FIXTURE)[0]
        self.assertEqual(parse_total_results('<span>（1,000件中）</span>'), 1000)
        self.assertEqual(coupon_key(coupon), "COU123:456")

    def test_full_snapshot_detects_added_changed_and_removed(self):
        coupon = parse_coupons(FIXTURE)[0]
        old_coupon = replace(coupon, discount_yen=8000)
        removed_coupon = replace(coupon, coupon_id="COU999")
        added_coupon = replace(coupon, coupon_id="COU456")
        previous = build_snapshot([old_coupon, removed_coupon])
        current = build_snapshot([coupon, added_coupon])

        added, changed, removed = compare_snapshots(previous, current)

        self.assertEqual(added, {"COU456:456"})
        self.assertEqual(changed, {"COU123:456"})
        self.assertEqual(removed, {"COU999:456"})

    def test_discord_request_uses_user_agent(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b""

        with patch("jalan_coupon_bot.urlopen", return_value=Response()) as mocked_urlopen:
            post_discord("https://discord.example/webhook", {"content": "test"})

        request = mocked_urlopen.call_args.args[0]
        self.assertEqual(request.get_header("User-agent"), DEFAULT_USER_AGENT)

    def test_daily_status_is_due_once_after_21_jst(self):
        before_report = datetime(2026, 9, 22, 11, 59, tzinfo=timezone.utc)
        after_report = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
        self.assertFalse(daily_status_due({}, before_report, 21))
        self.assertTrue(daily_status_due({}, after_report, 21))
        self.assertFalse(
            daily_status_due({"last_daily_status_jst": "2026-09-22"}, after_report, 21)
        )

    def test_daily_status_message_reports_no_new_coupon(self):
        with patch("jalan_coupon_bot.post_discord") as mocked_post:
            notify_daily_status(
                "https://discord.example/webhook",
                fetched_count=995,
                matching_count=5,
                added_count=0,
                changed_count=0,
                removed_count=3,
                checked_at=datetime(2026, 9, 22, 12, 30, tzinfo=timezone.utc),
            )

        payload = mocked_post.call_args.args[1]
        self.assertIn("現時点では新しい該当クーポンはありません", payload["content"])
        self.assertIn("掲載: 995宿分 / 割引率50%超: 5件", payload["content"])
        self.assertIn("追加 0 / 変更 0 / 掲載終了 3", payload["content"])


if __name__ == "__main__":
    unittest.main()
