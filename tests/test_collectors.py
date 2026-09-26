import time
import unittest

from nfl_market_edge.kalshi import MarketState, RecentSet
from nfl_market_edge.sportsbook import validate_selection_state
from scripts.collect_live_combo_slate import (
    SlateRegistry,
    is_slate_combo,
    subscribe_market_data,
)
from scripts.collect_live_sportsbook_props import (
    fanduel_runner_line,
    fanduel_runner_subject,
    records_to_persist,
    selection_records,
)


class CollectorTest(unittest.TestCase):
    def test_recent_set_has_a_fixed_replay_window(self):
        values = RecentSet(2)

        self.assertTrue(values.add("one"))
        self.assertFalse(values.add("one"))
        values.add("two")
        values.add("three")

        self.assertEqual(len(values), 2)
        self.assertNotIn("one", values)
        self.assertIn("three", values)

    def test_cross_game_filter_uses_game_suffix_not_series(self):
        event_games = {
            "26SEP20MINCHI": "MIN @ CHI",
            "26SEP20CARATL": "CAR @ ATL",
        }
        same_game = {
            "mve_selected_legs": [
                {"event_ticker": "KXNFLPASSYDS-26SEP20MINCHI"},
                {"event_ticker": "KXNFLTD-26SEP20MINCHI"},
            ]
        }
        cross_game = {
            "mve_selected_legs": [
                {"event_ticker": "KXNFLPASSYDS-26SEP20MINCHI"},
                {"event_ticker": "KXNFLTD-26SEP20CARATL"},
            ]
        }

        self.assertFalse(is_slate_combo(same_game, event_games))
        self.assertTrue(is_slate_combo(cross_game, event_games))
        self.assertTrue(is_slate_combo(same_game, event_games, "same_game"))
        self.assertFalse(is_slate_combo(cross_game, event_games, "same_game"))

    def test_poll_quotes_expand_to_normalized_selections(self):
        rows = [{
            "sportsbook": "fanduel",
            "poll_id": "poll-1",
            "fetched_at": "2026-09-16T12:00:00+00:00",
            "source_update_at": None,
            "game": "A @ B",
            "event_id": "event-1",
            "scheduled_start": "2026-09-20T17:00:00Z",
            "event_status": "scheduled",
            "is_live": False,
            "away_team": "A",
            "home_team": "B",
            "player": "Player One",
            "player_id": None,
            "player_team": "A",
            "prop_type": "receiving_yards",
            "is_alternate": False,
            "market_id": "market-1",
            "threshold": 59.5,
            "over_odds": -110,
            "under_odds": 105,
            "over_selection_id": "over-1",
            "under_selection_id": "under-1",
        }]

        records = selection_records(rows, "session-1", "slate-1", "nfl")

        self.assertEqual({row["side"] for row in records}, {"over", "under"})
        self.assertTrue(all(row["slate_id"] == "slate-1" for row in records))
        for row in records:
            validate_selection_state(row)

    def test_unchanged_selections_are_periodically_refreshed(self):
        record = {
            "sportsbook": "fanduel",
            "event_id": "event-1",
            "market_id": "market-1",
            "selection_id": "selection-1",
            "line": 50.5,
            "american_odds": -110,
            "decimal_odds": 1.91,
            "state": "open",
            "event_status": "scheduled",
            "is_live": False,
        }
        previous = {}

        first = records_to_persist([{**record}], previous, refresh=True)
        duplicate = records_to_persist([{**record}], previous, refresh=False)
        refresh = records_to_persist([{**record}], previous, refresh=True)
        changed = records_to_persist(
            [{**record, "american_odds": -105}], previous, refresh=False
        )

        self.assertEqual(first[0]["change_type"], "snapshot")
        self.assertEqual(duplicate, [])
        self.assertEqual(refresh[0]["change_type"], "refresh")
        self.assertEqual(changed[0]["change_type"], "update")
        self.assertEqual(changed[0]["changed"], ["american_odds"])

    def test_fanduel_alternate_line_comes_from_runner_name(self):
        runner = {"handicap": 0, "runnerName": "Chicago Bears (+3.5)"}

        self.assertEqual(fanduel_runner_line(runner), 3.5)
        self.assertEqual(fanduel_runner_subject(runner), "Chicago Bears")

    def test_market_state_dedupes_snapshots_and_trade_ids(self):
        state = MarketState(["ticker"])
        snapshot = {
            "type": "orderbook_snapshot",
            "msg": {"market_ticker": "ticker", "yes": [[10, 1]], "no": [[80, 1]]},
        }
        trade = {
            "type": "trade",
            "msg": {"market_ticker": "ticker", "trade_id": "trade-1", "count": 1},
        }

        self.assertEqual(len(state.process(snapshot, "now")), 1)
        self.assertEqual(state.process(snapshot, "later"), [])
        self.assertEqual(len(state.process(trade, "now")), 1)
        self.assertEqual(state.process(trade, "later"), [])

    def test_empty_book_snapshot_is_not_persisted(self):
        state = MarketState(["ticker"])
        empty = {
            "type": "orderbook_snapshot",
            "msg": {"market_ticker": "ticker", "yes": [], "no": []},
        }

        self.assertEqual(state.process(empty, "now"), [])

    def test_market_state_evicts_old_books_and_trade_ids(self):
        state = MarketState([], max_tickers=2, dedupe_entries=2)
        state.add_tickers(["component"], pinned=True)
        state.add_tickers(["combo-1", "combo-2"])
        for index in range(3):
            state.process(
                {
                    "type": "trade",
                    "msg": {
                        "market_ticker": "combo-2",
                        "trade_id": f"trade-{index}",
                    },
                },
                "now",
            )

        self.assertEqual(state.tickers, {"component", "combo-2"})
        self.assertNotIn("combo-1", state.books)
        self.assertEqual(len(state.seen_trade_ids), 2)

    def test_registry_streams_discovery_and_evicts_old_combos(self):
        class Writer:
            def __init__(self):
                self.records = []

            def write(self, record):
                self.records.append(record)

        manifest = {
            "slate_id": "slate",
            "event_games": {"GAME1": "A @ B", "GAME2": "C @ D"},
        }
        writer = Writer()
        registry = SlateRegistry(
            manifest, writer, max_combos=2, combo_idle_seconds=10
        )
        for index in range(3):
            registry.add_combo(
                {
                    "ticker": f"combo-{index}",
                    "mve_selected_legs": [
                        {"event_ticker": "SERIES-GAME1", "market_ticker": "leg-1"},
                        {"event_ticker": "SERIES-GAME2", "market_ticker": "leg-2"},
                    ],
                },
                "test",
            )

        self.assertEqual(list(registry.combos), ["combo-1", "combo-2"])
        self.assertEqual(registry.take_evicted_tickers(), ["combo-0"])
        self.assertEqual(len(writer.records), 3)
        expired = registry.evict_stale(now=time.monotonic() + 11)
        self.assertEqual(expired, ["combo-1", "combo-2"])


class SubscriptionTest(unittest.IsolatedAsyncioTestCase):
    async def test_component_markets_do_not_subscribe_to_trades(self):
        class WebSocket:
            def __init__(self):
                self.sent = []

            async def send(self, value):
                import json

                self.sent.append(json.loads(value))

        class Registry:
            @staticmethod
            def role(ticker):
                return "combo" if ticker == "combo" else "component"

        websocket = WebSocket()
        await subscribe_market_data(
            websocket, 1, Registry(), ["combo", "component"]
        )
        trade_subscriptions = [
            message
            for message in websocket.sent
            if message["params"]["channels"] == ["trade"]
        ]

        self.assertEqual(
            trade_subscriptions[0]["params"]["market_tickers"], ["combo"]
        )


if __name__ == "__main__":
    unittest.main()
