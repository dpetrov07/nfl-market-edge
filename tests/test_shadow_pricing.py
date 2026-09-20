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
    def selection(side, decimal_odds, received_at):
        return {
            "record_type": "selection_state",
            "slate_id": "slate-1",
            "received_at": received_at,
            "sportsbook": "bovada",
            "player": "Player",
            "prop_type": "receiving_yards",
            "line": 49.5,
            "side": side,
            "market_id": "market",
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
