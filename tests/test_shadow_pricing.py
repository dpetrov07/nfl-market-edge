import gzip
import json
import tempfile
import unittest
from pathlib import Path

from nfl_market_edge.shadow import component_identity, price_external_combo
from scripts.build_shadow_pricing import SportsbookHistory, build_decision_rows, parse_time


class ShadowPricingTest(unittest.TestCase):
    def test_series_maps_to_sportsbook_prop(self):
        leg, error = component_identity(
            {"event_ticker": "KXNFLPASSTDS-GAME", "side": "yes"},
            {"title": "Josh Allen: 2+"},
        )

        self.assertIsNone(error)
        self.assertEqual(leg["prop_type"], "passing_touchdowns")

    def test_game_line_uses_full_team_from_leg_game(self):
        leg, error = component_identity(
            {
                "event_ticker": "KXNFLSPREAD-GAME",
                "side": "yes",
                "game": "Philadelphia Eagles @ Tennessee Titans",
            },
            {"yes_sub_title": "Philadelphia wins by over 6.5 points", "floor_strike": 6.5},
        )

        self.assertIsNone(error)
        self.assertEqual(leg["player_key"], "philadelphiaeagles")
        self.assertEqual(leg["prop_type"], "spread")
        self.assertEqual(leg["line"], 6.5)

    def test_two_books_produce_a_conservative_quote(self):
        legs = [
            {
                "book_quotes": [
                    {
                        "sportsbook": book,
                        "devig_probability": probability,
                        "age_seconds": 10,
                    }
                    for book in ("bovada", "fanduel")
                ]
            }
            for probability in (0.5, 0.4)
        ]

        result = price_external_combo(legs)

        self.assertAlmostEqual(result["fair_value"], 0.2)
        self.assertIsNone(result["skip_reason"])
        self.assertGreater(result["proposed_yes_sell_price"], result["fair_value_high"])

    def test_one_book_is_not_enough_to_quote(self):
        result = price_external_combo(
            [{"book_quotes": [{
                "sportsbook": "bovada",
                "devig_probability": 0.5,
                "age_seconds": 5,
            }]}]
        )

        self.assertIsNone(result["proposed_yes_sell_price"])
        self.assertEqual(result["skip_reason"], "leg_1_only_1_book")

    def test_quote_specific_uncertainty_widens_the_leg_range(self):
        result = price_external_combo(
            [{"book_quotes": [
                {
                    "sportsbook": book,
                    "devig_probability": 0.5,
                    "probability_uncertainty": 0.03,
                    "age_seconds": 5,
                }
                for book in ("bovada", "fanduel")
            ]}]
        )

        self.assertAlmostEqual(result["fair_value_low"], 0.46)
        self.assertAlmostEqual(result["fair_value_high"], 0.54)

    def test_sportsbook_history_does_not_look_past_the_rfq(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "book.jsonl.gz"
            rows = [
                self.selection(side, odds, at)
                for at, over, under in (
                    ("2026-09-20T12:00:00Z", 2.0, 2.0),
                    ("2026-09-20T12:00:20Z", 1.25, 5.0),
                )
                for side, odds in (("over", over), ("under", under))
            ]
            self.write(path, rows)

            history = SportsbookHistory([path], "slate-1")
            quotes = history.quotes(
                {
                    "player_key": "player",
                    "prop_type": "receiving_yards",
                    "line": 49.5,
                    "sportsbook_side": "over",
                },
                parse_time("2026-09-20T12:00:10Z"),
            )

        self.assertEqual(len(quotes), 1)
        self.assertAlmostEqual(quotes[0]["devig_probability"], 0.5)
        self.assertEqual(quotes[0]["price_method"], "exact_two_way")

    def test_sportsbook_history_interpolates_surrounding_alt_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "book.jsonl.gz"
            rows = [
                self.selection(
                    "over",
                    odds,
                    "2026-09-20T12:00:00Z",
                    line=line,
                    market_id=f"market-{line}",
                )
                for line, odds in ((39.5, 1.5), (49.5, 2.5))
            ]
            self.write(path, rows)
            history = SportsbookHistory([path], "slate-1")

            quotes = history.quotes(
                {
                    "player_key": "player",
                    "prop_type": "receiving_yards",
                    "line": 44.5,
                    "sportsbook_side": "over",
                },
                parse_time("2026-09-20T12:00:10Z"),
            )

        self.assertEqual(quotes[0]["price_method"], "interpolated_alt_lines")
        self.assertEqual(quotes[0]["source_lines"], [39.5, 49.5])
        self.assertAlmostEqual(quotes[0]["devig_probability"], (1 / 1.5 + 1 / 2.5) / 2)
        self.assertAlmostEqual(quotes[0]["probability_uncertainty"], 0.05)

    def test_same_game_pipeline_never_quotes(self):
        legs = [
            {
                "market_ticker": ticker,
                "event_ticker": series,
                "side": "yes",
                "game": "A @ B",
            }
            for ticker, series in (
                ("LEG-A", "KXNFLRECYDS-GAME"),
                ("LEG-B", "KXNFLRSHYDS-GAME"),
            )
        ]
        records = [
            {
                "record_type": "combo_discovery",
                "received_at": "2026-09-20T12:00:00Z",
                "combo": {"ticker": "COMBO", "mve_selected_legs": legs},
            },
            {
                "record_type": "communication",
                "communication_type": "rfq_created",
                "received_at": "2026-09-20T12:00:10Z",
                "market_ticker": "COMBO",
                "rfq_id": "RFQ-1",
            },
        ]

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl.gz"
            self.write(path, records)
            rows = build_decision_rows(
                {"slate_id": "slate-1", "combo_scope": "same_game"}, path, []
            )

        self.assertEqual(rows[0]["shadow_action"], "skip")
        self.assertIn("same_game_correlation_not_modeled", rows[0]["skip_reason"])

    @staticmethod
    def selection(side, decimal_odds, received_at, *, line=49.5, market_id="market"):
        return {
            "record_type": "selection_state",
            "slate_id": "slate-1",
            "received_at": received_at,
            "sportsbook": "bovada",
            "player": "Player",
            "prop_type": "receiving_yards",
            "line": line,
            "side": side,
            "market_id": market_id,
            "decimal_odds": decimal_odds,
            "state": "open",
        }

    @staticmethod
    def write(path, rows):
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")


if __name__ == "__main__":
    unittest.main()
