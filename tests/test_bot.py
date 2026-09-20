import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jalan_coupon_bot import DEFAULT_USER_AGENT, matches, parse_coupons, post_discord


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
        self.assertTrue(matches(coupon, {"minimum_coupon_yen": 10000, "minimum_discount_rate": 0.5}))
        self.assertFalse(
            matches(
                coupon,
                {"minimum_coupon_yen": 10000, "minimum_discount_rate": 0.95, "match_mode": "all"},
            )
        )

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


if __name__ == "__main__":
    unittest.main()
