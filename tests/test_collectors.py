import argparse
import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from nfl_market_edge.kalshi import MarketState, load_private_key
from nfl_market_edge.sportsbook import validate_selection_state
from scripts.collect_combo_slate import commands
from scripts.collect_live_combo_slate import (
    Writer,
    communication_identity,
    communication_record,
    fill_record,
    is_slate_combo,
    subscribe_market_data,
)
from scripts.collect_live_sportsbook_props import selection_records
from scripts.collect_live_sportsbook_props import records_to_persist


class CollectorTest(unittest.TestCase):
    def test_kalshi_private_key_loads_from_raw_pem(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()

        with patch.dict("os.environ", {"KALSHI_PRIVATE_KEY": pem}, clear=True):
            loaded = load_private_key(argparse.Namespace(private_key_path=None))

        self.assertEqual(
            loaded.public_key().public_numbers(), key.public_key().public_numbers()
        )

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

    def test_local_runner_starts_one_worker_per_source(self):
        manifest = {
            "slate_id": "slate-1",
            "league": "nfl",
            "date": "2026-09-20",
            "events": [
                {"ticker": "KXNFLGAME-26SEP20MINCHI", "game": "MIN @ CHI"},
                {"ticker": "KXNFLGAME-26SEP20CARATL", "game": "CAR @ ATL"},
            ],
            "sportsbooks": {
                "books": ["bovada", "fanduel", "betrivers"],
                "interval_seconds": 30,
            },
        }
        args = argparse.Namespace(
            manifest=Path("config/slate.json"),
            output_dir=Path("data/live/combo_slates"),
            duration=None,
        )

        workers = commands(args, manifest)

        self.assertEqual(len(workers), 4)
        self.assertEqual(
            {worker[worker.index("--book") + 1] for worker in workers[1:]},
            {"bovada", "fanduel", "betrivers"},
        )

    def test_kalshi_fill_keeps_exchange_and_receive_times(self):
        record = fill_record(
            {
                "ticker": "KXMVECROSS-1",
                "trade_id": "trade-1",
                "order_id": "order-1",
                "created_time": "2026-09-16T12:00:00Z",
                "yes_price": 7,
                "no_price": 93,
                "count": 2,
            },
            "slate-1",
        )

        self.assertEqual(record["exchange_timestamp"], "2026-09-16T12:00:00Z")
        self.assertIsNotNone(record["received_at"])
        self.assertEqual(record["yes_price_dollars"], 0.07)

    def test_rfq_record_has_lag_and_stable_dedupe_identity(self):
        raw = {
            "type": "rfq_created",
            "msg": {
                "id": "rfq-1",
                "created_ts": "2026-09-20T12:00:00Z",
                "market_ticker": "combo-1",
            },
        }

        record = communication_record(raw, "2026-09-20T12:00:00.125000+00:00")

        self.assertEqual(record["persistence_lag_ms"], 125.0)
        self.assertNotIn("contracts", record)
        self.assertNotIn("payload", record)
        self.assertEqual(
            communication_identity(raw),
            ("rfq_created", "rfq-1", "2026-09-20T12:00:00Z"),
        )

    def test_writer_flushes_and_reports_storage_growth(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl.gz"
            writer = Writer(
                path,
                gzip_member_bytes=1,
                gzip_member_seconds=3600,
                volume_capacity_bytes=1_000_000,
                archive_retention_bytes=500_000,
                target_storage_bytes=750_000,
            )
            writer.write({"record_type": "communication"}, flush=True)
            metrics = writer.storage_metrics()
            writer.close()

            rows = []
            for part in sorted(Path(directory).glob("events*.jsonl.gz")):
                with gzip.open(part, "rt") as handle:
                    rows.extend(json.loads(line) for line in handle)

        self.assertEqual(rows, [{"record_type": "communication"}])
        self.assertGreater(metrics["output_bytes"], 0)
        self.assertGreaterEqual(metrics["gzip_members_opened"], 2)
        self.assertTrue(metrics["retention_protected"])

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
