import gzip
import json
from pathlib import Path
import tempfile
import unittest

from nfl_market_edge.shadow import component_identity, devig_probability, price_external_combo
from scripts.build_shadow_pricing import build_decision_rows


class ShadowPricingTest(unittest.TestCase):
    def test_live_kalshi_series_aliases_map_to_sportsbook_props(self):
        for series, expected in (
            ("KXNFLREC", "receptions"),
            ("KXNFLPASSTDS", "passing_touchdowns"),
        ):
            leg, error = component_identity(
                {"event_ticker": f"{series}-GAME", "side": "yes"},
                {"title": "Josh Allen: 2+", "floor_strike": 1.5},
            )
            self.assertIsNone(error)
            self.assertEqual(leg["prop_type"], expected)

    def test_proportional_devig_and_conservative_quote(self):
        self.assertAlmostEqual(devig_probability(2.0, 2.0, "over"), 0.5)
        legs = [
            {
                "book_quotes": [
                    {"sportsbook": "bovada", "devig_probability": 0.50, "age_seconds": 10},
                    {"sportsbook": "fanduel", "devig_probability": 0.52, "age_seconds": 20},
                ]
            },
            {
                "book_quotes": [
                    {"sportsbook": "bovada", "devig_probability": 0.40, "age_seconds": 10},
                    {"sportsbook": "fanduel", "devig_probability": 0.42, "age_seconds": 20},
                ]
            },
        ]

        result = price_external_combo(legs)

        self.assertAlmostEqual(result["fair_value"], 0.51 * 0.41)
        self.assertEqual(result["skip_reason"], None)
        self.assertIsNotNone(result["proposed_yes_sell_price"])
        self.assertGreater(result["proposed_yes_sell_price"], result["fair_value_high"])

    def test_one_book_is_valued_but_not_quoted(self):
        result = price_external_combo(
            [{"book_quotes": [{
                "sportsbook": "bovada",
                "devig_probability": 0.5,
                "age_seconds": 5,
            }]}]
        )

        self.assertEqual(result["fair_value"], 0.5)
        self.assertIsNone(result["proposed_yes_sell_price"])
        self.assertEqual(result["skip_reason"], "leg_1_only_1_book")

    def test_pipeline_uses_only_sportsbook_prices_available_at_rfq(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            kalshi = root / "events.jsonl.gz"
            sportsbook_paths = []
            legs = [
                {
                    "market_ticker": "LEG-A",
                    "event_ticker": "KXNFLRECYDS-GAME-A",
                    "side": "yes",
                    "game": "A @ B",
                },
                {
                    "market_ticker": "LEG-B",
                    "event_ticker": "KXNFLRSHYDS-GAME-B",
                    "side": "yes",
                    "game": "C @ D",
                },
            ]
            records = [
                {
                    "record_type": "combo_discovery",
                    "received_at": "2026-09-20T12:00:00Z",
                    "combo": {"ticker": "COMBO", "mve_selected_legs": legs},
                },
                {
                    "record_type": "component_discovery",
                    "received_at": "2026-09-20T12:00:00Z",
                    "markets": [
                        {
                            "ticker": "LEG-A",
                            "event_ticker": "KXNFLRECYDS-GAME-A",
                            "title": "Alpha Player: 50+ receiving yards",
                        },
                        {
                            "ticker": "LEG-B",
                            "event_ticker": "KXNFLRSHYDS-GAME-B",
                            "title": "Beta Player: 40+ rushing yards",
                        },
                    ],
                },
            ]
            for ticker in ("LEG-A", "LEG-B"):
                records.append(self.top(ticker, "2026-09-20T12:00:00Z", 0.45, 0.55))
            records.extend(
                [
                    self.top("COMBO", "2026-09-20T12:00:00Z", 0.28, 0.30),
                    {
                        "record_type": "communication",
                        "communication_type": "rfq_created",
                        "received_at": "2026-09-20T12:00:10Z",
                        "exchange_timestamp": "2026-09-20T12:00:09Z",
                        "market_ticker": "COMBO",
                        "rfq_id": "RFQ-1",
                        "contracts": "10",
                    },
                    {
                        "record_type": "trade",
                        "received_at": "2026-09-20T12:00:15Z",
                        "market_ticker": "COMBO",
                        "trade_id": "TRADE-1",
                        "yes_price_dollars": 0.30,
                    },
                    self.top("COMBO", "2026-09-20T12:00:20Z", 0.24, 0.26),
                    {
                        "record_type": "market_status",
                        "received_at": "2026-09-20T20:00:00Z",
                        "market_ticker": "COMBO",
                        "result": "no",
                    },
                ]
            )
            self.write(kalshi, records)

            for book in ("bovada", "fanduel"):
                path = root / f"{book}.jsonl.gz"
                sportsbook_paths.append(path)
                rows = []
                for player, prop, line, market in (
                    ("Alpha Player", "receiving_yards", 49.5, "M-A"),
                    ("Beta Player", "rushing_yards", 39.5, "M-B"),
                ):
                    rows.extend(
                        [
                            self.selection(book, player, prop, line, market, "over", 2.0, "2026-09-20T12:00:00Z"),
                            self.selection(book, player, prop, line, market, "under", 2.0, "2026-09-20T12:00:00Z"),
                            self.selection(book, player, prop, line, market, "over", 1.25, "2026-09-20T12:00:20Z"),
                            self.selection(book, player, prop, line, market, "under", 4.0, "2026-09-20T12:00:20Z"),
                        ]
                    )
                self.write(path, rows)

            rows = build_decision_rows(
                {"slate_id": "slate-1"}, kalshi, sportsbook_paths
            )

            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertAlmostEqual(row["external_fair_value"], 0.25)
            self.assertEqual(row["books_available"], "bovada,fanduel")
            self.assertEqual(row["shadow_action"], "quote")
            self.assertEqual(row["observed_trade_yes_price"], 0.30)
            self.assertEqual(row["market_midpoint_10s"], 0.25)
            self.assertEqual(row["settlement_value"], 0.0)
            self.assertFalse(row["frozen_below_10c_candidate"])

    def test_same_game_pipeline_keeps_inputs_but_does_not_quote(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            kalshi = root / "events.jsonl.gz"
            legs = [
                {
                    "market_ticker": ticker,
                    "event_ticker": series,
                    "side": "yes",
                    "game": "A @ B",
                }
                for ticker, series in (
                    ("LEG-A", "KXNFLRECYDS-GAME-A"),
                    ("LEG-B", "KXNFLRSHYDS-GAME-A"),
                )
            ]
            self.write(kalshi, [
                {
                    "record_type": "combo_discovery",
                    "received_at": "2026-09-20T12:00:00Z",
                    "combo": {"ticker": "COMBO", "mve_selected_legs": legs},
                },
                {
                    "record_type": "component_discovery",
                    "received_at": "2026-09-20T12:00:00Z",
                    "markets": [
                        {"ticker": "LEG-A", "title": "Alpha: 50+ receiving yards"},
                        {"ticker": "LEG-B", "title": "Beta: 40+ rushing yards"},
                    ],
                },
                {
                    "record_type": "communication",
                    "communication_type": "rfq_created",
                    "received_at": "2026-09-20T12:00:10Z",
                    "market_ticker": "COMBO",
                    "rfq_id": "RFQ-1",
                },
            ])

            rows = build_decision_rows(
                {"slate_id": "slate-1", "combo_scope": "same_game"},
                kalshi,
                [],
            )

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["scope"], "same_game")
            self.assertEqual(rows[0]["shadow_action"], "skip")
            self.assertIn("same_game_correlation_not_modeled", rows[0]["skip_reason"])

    @staticmethod
    def top(ticker, at, bid, ask):
        return {
            "record_type": "top_of_book",
            "received_at": at,
            "market_ticker": ticker,
            "yes_bid_dollars": bid,
            "yes_ask_dollars": ask,
            "yes_bid_size": 100,
            "yes_ask_size": 100,
        }

    @staticmethod
    def selection(book, player, prop, line, market, side, decimal, at):
        return {
            "record_type": "selection_state",
            "slate_id": "slate-1",
            "received_at": at,
            "sportsbook": book,
            "game": "game",
            "player": player,
            "prop_type": prop,
            "line": line,
            "side": side,
            "market_id": market,
            "decimal_odds": decimal,
            "state": "open",
        }

    @staticmethod
    def write(path, rows):
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")


if __name__ == "__main__":
    unittest.main()
