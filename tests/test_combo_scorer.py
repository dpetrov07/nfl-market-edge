import unittest

from nfl_market_edge.combo import FROZEN_BENCHMARK, score_snapshot


class FrozenComboScorerTest(unittest.TestCase):
    def test_strict_below_ten_cent_boundary_is_unchanged(self):
        common = {
            "scope": "cross_game",
            "leg_count": 2,
            "distinct_leg_games": 2,
            "component_bid_product": 0.06,
            "component_mid_product": 0.075,
            "component_ask_product": 0.09,
            "max_quote_age_seconds": 12,
            "max_leg_spread": 0.04,
        }
        below = score_snapshot(yes_price=0.09, **common)
        boundary = score_snapshot(yes_price=0.10, **common)

        self.assertEqual(below["recommendation"], "consider")
        self.assertEqual(
            below["expected_net_edge"],
            FROZEN_BENCHMARK["equal_combo_net_edge"],
        )
        self.assertEqual(boundary["recommendation"], "pass")
        self.assertIsNone(boundary["expected_net_edge"])

    def test_fresh_combo_book_remains_the_fair_value_preference(self):
        result = score_snapshot(
            yes_price=0.08,
            scope="cross_game",
            leg_count=2,
            distinct_leg_games=2,
            component_bid_product=0.06,
            component_mid_product=0.07,
            component_ask_product=0.08,
            max_quote_age_seconds=5,
            max_leg_spread=0.03,
            combo_bid=0.075,
            combo_ask=0.085,
            combo_quote_age_seconds=4,
        )

        self.assertEqual(result["fair_value_method"], "combo_book_midpoint")
        self.assertEqual(result["fair_value"], 0.08)


if __name__ == "__main__":
    unittest.main()
