"""Collect change-only Bovada NFL player props over its public WebSocket."""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import math
import os
import re
import signal
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import websockets
from curl_cffi import requests

from collect_live_sportsbook_props import (
    BOVADA_URL,
    bovada_prop_markets,
    bovada_teams,
    game_matches,
    iso_from_epoch_ms,
    parse_american,
    parse_float,
)
from sportsbook_schema import SCHEMA_VERSION, selection_state_record


WS_BASE = "wss://services.bovada.lv/services/sports/subscription"
ET = ZoneInfo("America/New_York")
STATUS = {"O": "open", "S": "suspended", "D": "disabled", "U": "unavailable"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def local_game_date(event: dict):
    start = datetime.fromtimestamp(int(event["startTime"]) / 1000, tz=timezone.utc)
    return start.astimezone(ET).date()


def semantic_threshold(side: str, line: float) -> tuple[str, int | float]:
    if not line.is_integer():
        return (
            ("at_least", math.floor(line) + 1)
            if side == "over"
            else ("at_most", math.ceil(line) - 1)
        )
    return ("greater_than", line) if side == "over" else ("less_than", line)


def normalized_state(market_status: str | None, selection_status: str | None) -> str:
    if "REMOVED" in {market_status, selection_status}:
        return "removed"
    if market_status == "O" and selection_status == "O":
        return "open"
    if "S" in {market_status, selection_status}:
        return "suspended"
    if "D" in {market_status, selection_status}:
        return "disabled"
    if "U" in {market_status, selection_status}:
        return "unavailable"
    return "unavailable"


def event_metadata(event: dict) -> dict:
    away, home = bovada_teams(event)
    return {
        "game": event.get("description"),
        "game_date": local_game_date(event).isoformat(),
        "event_id": str(event.get("id")),
        "scheduled_start": iso_from_epoch_ms(event.get("startTime")),
        "event_status": event.get("status"),
        "is_live": bool(event.get("live")),
        "away_team": away.get("name"),
        "away_team_id": str(away.get("id")) if away.get("id") else None,
        "home_team": home.get("name"),
        "home_team_id": str(home.get("id")) if home.get("id") else None,
    }


def selection_rows(event: dict) -> list[dict]:
    rows = []
    for market in bovada_prop_markets(event, include_inactive=True):
        for outcome in market["outcomes"]:
            operator, threshold = semantic_threshold(outcome["side"], outcome["threshold"])
            rows.append(
                {
                    "player": market["player"],
                    "player_id": outcome["player_id"],
                    "player_team": market["player_team"],
                    "prop_type": market["prop_type"],
                    "is_alternate": market["is_alternate"],
                    "side": outcome["side"],
                    "line": outcome["threshold"],
                    "semantic_operator": operator,
                    "semantic_threshold": threshold,
                    "threshold": threshold,
                    "market_id": market["market_id"],
                    "selection_id": outcome["selection_id"],
                    "american_odds": outcome["american_odds"],
                    "decimal_odds": outcome["decimal_odds"],
                    "market_state": STATUS.get(market["market_status"], market["market_status"]),
                    "selection_state": STATUS.get(
                        outcome["selection_status"], outcome["selection_status"]
                    ),
                    "state": normalized_state(
                        market["market_status"], outcome["selection_status"]
                    ),
                    "_market_status": market["market_status"],
                    "_selection_status": outcome["selection_status"],
                }
            )
    return rows


def fetch_sunday_events(date_text: str, games: list[str]) -> tuple[list[dict], str]:
    response = requests.get(BOVADA_URL, impersonate="chrome120", timeout=20)
    response.raise_for_status()
    received_at = utc_now()
    payload = response.json()
    wanted_date = datetime.strptime(date_text, "%Y-%m-%d").date()
    events = payload[0].get("events", []) if isinstance(payload, list) and payload else []
    return [
        event
        for event in events
        if local_game_date(event) == wanted_date
        and (not games or any(game_matches(event.get("description", ""), game) for game in games))
        and bovada_prop_markets(event, include_inactive=True)
    ], received_at


class GameCollector:
    tracked_fields = (
        "american_odds",
        "decimal_odds",
        "line",
        "state",
        "market_state",
        "selection_state",
    )

    def __init__(self, event: dict, received_at: str, output_dir: Path, session_id: str):
        self.event = event_metadata(event)
        self.event_id = self.event["event_id"]
        self.session_id = session_id
        self.selections: dict[str, dict] = {}
        self.market_members: dict[str, set[str]] = {}
        self.frames = 0
        self.records = 0
        self.ws_records = 0
        self.pending_target: dict | None = None
        output_dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "_", self.event["game"].lower()).strip("_")
        day = self.event["game_date"]
        self.path = output_dir / f"bovada_{day}_{slug}_{self.event_id}.jsonl.gz"
        self.handle = gzip.open(self.path, "at", encoding="utf-8", compresslevel=6)
        for row in selection_rows(event):
            self._remember(row)
            self._write(row, received_at, "initial", "http", list(self.tracked_fields))

    def _remember(self, row: dict) -> None:
        selection_id = row["selection_id"]
        old = self.selections.get(selection_id, {})
        self.selections[selection_id] = {**old, **row}
        self.market_members.setdefault(row["market_id"], set()).add(selection_id)

    def _write(
        self, row: dict, received_at: str, change_type: str, source: str, changed: list[str]
    ) -> None:
        record = selection_state_record(
            sportsbook="bovada",
            session_id=self.session_id,
            received_at=received_at,
            source=source,
            change_type=change_type,
            event=self.event,
            selection=row,
            changed=changed,
        )
        self.handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        self.handle.flush()
        self.records += 1
        self.ws_records += source == "websocket"

    def update(self, candidate: dict, received_at: str) -> bool:
        selection_id = str(candidate["selection_id"])
        old = self.selections.get(selection_id)
        if old is None:
            self._remember(candidate)
            self._write(candidate, received_at, "added", "websocket", list(self.tracked_fields))
            return True
        new = {**old, **candidate}
        changed = [field for field in self.tracked_fields if old.get(field) != new.get(field)]
        if not changed:
            return False
        if old.get("state") == "removed" and new.get("state") != "removed":
            change_type = "reappeared"
        elif new.get("state") == "removed":
            change_type = "removed"
        elif new.get("state") == "suspended" and old.get("state") != "suspended":
            change_type = "suspended"
        else:
            price_fields = {"american_odds", "decimal_odds"} & set(changed)
            line_changed = "line" in changed
            change_type = "odds_line" if price_fields and line_changed else "line" if line_changed else "odds" if price_fields else "state"
        self._remember(new)
        self._write(new, received_at, change_type, "websocket", changed)
        return True

    def update_status(self, object_id: str, status: str, received_at: str) -> int:
        object_id = str(object_id)
        if object_id in self.selections:
            old = self.selections[object_id]
            return int(
                self.update(
                    {
                        **old,
                        "_selection_status": status,
                        "selection_state": STATUS.get(status, status),
                        "state": normalized_state(old.get("_market_status"), status),
                    },
                    received_at,
                )
            )
        changed = 0
        for selection_id in self.market_members.get(object_id, ()):
            old = self.selections[selection_id]
            changed += self.update(
                {
                    **old,
                    "_market_status": status,
                    "market_state": STATUS.get(status, status),
                    "state": normalized_state(status, old.get("_selection_status")),
                },
                received_at,
            )
        return changed

    def remove(self, object_id: str, received_at: str) -> int:
        object_id = str(object_id)
        ids = [object_id] if object_id in self.selections else self.market_members.get(object_id, ())
        changed = 0
        for selection_id in ids:
            old = self.selections[selection_id]
            changed += self.update(
                {
                    **old,
                    "_selection_status": "REMOVED",
                    "state": "removed",
                    "selection_state": "removed",
                },
                received_at,
            )
        return changed

    def apply_market(self, market: dict, received_at: str) -> int:
        wrapper = {"displayGroups": [{"markets": [market]}]}
        parsed = bovada_prop_markets(wrapper, include_inactive=True)
        if not parsed:
            return 0
        market_id = str(market.get("id"))
        before = set(self.market_members.get(market_id, ()))
        seen = set()
        changed = 0
        for row in selection_rows({"displayGroups": [{"markets": [market]}]}):
            seen.add(row["selection_id"])
            changed += self.update(row, received_at)
        for missing in before - seen:
            changed += self.remove(missing, received_at)
        return changed

    def apply_object(self, obj: dict, received_at: str) -> int:
        if not isinstance(obj, dict):
            return 0
        if {"eventId", "type", "target"} <= obj.keys():
            self.pending_target = obj
            return 0

        target, self.pending_target = self.pending_target, None
        object_id = str(obj.get("id")) if obj.get("id") is not None else ""
        mode = str((target or {}).get("mode", "")).upper()
        if mode in {"DELETE", "REMOVE", "REMOVED"}:
            target_id = object_id if object_id and object_id != "0" else str(
                (target or {}).get("parentId", "")
            )
            return self.remove(target_id, received_at)
        if obj == {"id": 0} or "enabled" in obj:
            return 0
        if object_id == self.event_id:
            if obj.get("status") is not None:
                self.event["event_status"] = obj["status"]
            if obj.get("live") is not None:
                self.event["is_live"] = bool(obj["live"])
        changed = 0
        if "displayGroups" in obj:
            markets = [
                market
                for group in obj.get("displayGroups", [])
                for market in group.get("markets", [])
            ]
            for market in markets:
                changed += self.apply_market(market, received_at)
            return changed
        if "markets" in obj:
            for market in obj.get("markets", []):
                changed += self.apply_market(market, received_at)
            return changed
        if "outcomes" in obj and object_id:
            return self.apply_market(obj, received_at)
        if object_id in self.selections and "price" in obj:
            old = self.selections[object_id]
            price = obj.get("price") or {}
            line = parse_float(price.get("handicap"))
            side = old["side"]
            candidate = {
                **old,
                "american_odds": parse_american(price.get("american")),
                "decimal_odds": parse_float(price.get("decimal")),
            }
            if line is not None:
                operator, threshold = semantic_threshold(side, line)
                candidate.update(
                    line=line,
                    semantic_operator=operator,
                    semantic_threshold=threshold,
                )
            if obj.get("status"):
                status = obj["status"]
                candidate.update(
                    _selection_status=status,
                    selection_state=STATUS.get(status, status),
                    state=normalized_state(old.get("_market_status"), status),
                )
            return int(self.update(candidate, received_at))
        if object_id and obj.get("status"):
            return self.update_status(object_id, obj["status"], received_at)
        return 0

    def apply_frame(self, message: str | bytes, received_at: str, verbose: bool) -> int:
        self.frames += 1
        if isinstance(message, bytes):
            message = message.decode("utf-8")
        changed = 0
        for part in message.split("|"):
            try:
                obj = json.loads(part)
            except json.JSONDecodeError:
                continue
            changed += self.apply_object(obj, received_at)
        if verbose and changed:
            print(
                f"{received_at} {self.event['game']}: "
                f"{changed} selection change(s) from WebSocket",
                flush=True,
            )
        return changed

    def close(self) -> None:
        self.handle.close()


async def stream_game(collector: GameCollector, verbose: bool) -> None:
    backoff = 1
    while True:
        subscription_id = str(uuid.uuid4()).upper()
        try:
            async with websockets.connect(
                f"{WS_BASE}/{subscription_id}",
                open_timeout=20,
                ping_interval=20,
                ping_timeout=20,
                max_size=8 * 1024 * 1024,
            ) as websocket:
                await websocket.send(
                    f"SUBSCRIBE|A|/events/{collector.event_id}.{int(time.time() * 1000)}?delta=true"
                )
                print(f"subscribed {collector.event['game']} ({collector.event_id})", flush=True)
                backoff = 1
                async for message in websocket:
                    received_at = utc_now()
                    collector.apply_frame(message, received_at, verbose)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(
                f"{collector.event['game']} WebSocket disconnected: {type(exc).__name__}: {exc}; retrying in {backoff}s",
                flush=True,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


def parse_args() -> argparse.Namespace:
    volume = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
    default_output = os.getenv("BOVADA_OUTPUT_DIR") or (
        str(Path(volume) / "bovada_live") if volume else "data/raw/bovada_live"
    )
    today = os.getenv("BOVADA_GAME_DATE", datetime.now(ET).date().isoformat())
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=today, help="Sunday game date in America/New_York")
    parser.add_argument(
        "--game", action="append", help="Optional game filter; repeat for multiple games"
    )
    parser.add_argument("--output-dir", type=Path, default=Path(default_output))
    parser.add_argument("--run-seconds", type=float, help="Stop after this many seconds")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    if not args.game:
        args.game = [
            game.strip() for game in os.getenv("BOVADA_GAMES", "").split(",") if game.strip()
        ]
    return args


async def async_main(args: argparse.Namespace) -> None:
    backoff = 1
    while True:
        try:
            events, received_at = await asyncio.to_thread(
                fetch_sunday_events, args.date, args.game
            )
            if events:
                break
            print(
                f"No Bovada NFL player props found for {args.date}; "
                f"retrying in {backoff}s",
                flush=True,
            )
        except Exception as exc:
            print(f"Bovada discovery error: {exc}; retrying in {backoff}s", flush=True)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)
    session_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S.%fZ}-{uuid.uuid4().hex[:8]}"
    collectors = []
    for event in events:
        try:
            collectors.append(GameCollector(event, received_at, args.output_dir, session_id))
        except Exception as exc:
            print(f"{event.get('description')} setup failed: {exc}", flush=True)
    if not collectors:
        raise SystemExit("No Bovada game collectors could start")
    print(
        f"discovered {len(collectors)} game(s), {sum(len(c.selections) for c in collectors)} exact selections",
        flush=True,
    )
    tasks = [asyncio.create_task(stream_game(collector, args.verbose)) for collector in collectors]
    try:
        if args.run_seconds:
            await asyncio.sleep(args.run_seconds)
        else:
            await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for collector in collectors:
            collector.close()
            print(
                f"{collector.event['game']}: {collector.frames} frame(s), "
                f"{collector.ws_records} WebSocket change record(s) -> {collector.path}",
                flush=True,
            )


def main() -> None:
    args = parse_args()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            loop.add_signal_handler(
                getattr(signal, name),
                lambda: [task.cancel() for task in asyncio.all_tasks(loop)],
            )
    try:
        loop.run_until_complete(async_main(args))
    except asyncio.CancelledError:
        pass
    finally:
        loop.close()


if __name__ == "__main__":
    main()
