"""Collect compact ESPN NFL play changes for multiple games concurrently."""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import os
import re
import signal
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
PLAYS_URL = (
    "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/events/"
    "{event_id}/competitions/{event_id}/plays"
)
STOP = False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ref_id(value) -> str | None:
    ref = value.get("$ref", "") if isinstance(value, dict) else ""
    match = re.search(r"/(?:athletes|teams)/(\d+)(?:\?|$)", ref)
    return match.group(1) if match else None


def get_json(url: str, params: dict | None = None) -> dict:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=20) as response:
        return json.load(response)


def discover_games(date_text: str) -> list[dict]:
    payload = get_json(
        SCOREBOARD_URL,
        {"dates": date_text.replace("-", ""), "limit": 100},
    )
    games = []
    for event in payload.get("events", []):
        competition = event.get("competitions", [{}])[0]
        teams = {}
        for competitor in competition.get("competitors", []):
            team = competitor.get("team", {})
            teams[competitor.get("homeAway")] = {
                "id": str(team.get("id")),
                "abbreviation": team.get("abbreviation"),
                "name": team.get("displayName"),
            }
        if not teams.get("away") or not teams.get("home"):
            continue
        games.append({
            "event_id": str(event["id"]),
            "name": event.get("name"),
            "kickoff": event.get("date"),
            "away": teams["away"],
            "home": teams["home"],
        })
    return games


def participant_ids(play: dict) -> dict[str, str | None]:
    roles: dict[str, str | None] = {"passer": None, "rusher": None, "receiver": None}
    for participant in play.get("participants", []):
        role = participant.get("type")
        if role in roles and roles[role] is None:
            roles[role] = ref_id(participant.get("athlete"))
    return roles


def participant_names(play: dict) -> dict[str, str | None]:
    short = play.get("shortText") or play.get("shortAlternativeText") or ""
    names: dict[str, str | None] = {"passer": None, "rusher": None, "receiver": None}
    match = re.match(r"(.+?) (?:Pass Complete|Incomplete Pass|Pass Incomplete)\b", short, re.I)
    if match:
        names["passer"] = match.group(1).strip()
    match = re.search(r"(?:\bto|Intended For) (.+?)(?: for -?\d|$)", short, re.I)
    if match:
        names["receiver"] = match.group(1).strip().rstrip(".")
    match = re.match(r"(.+?) -?\d+ Yd Rush\b", short, re.I)
    if match:
        names["rusher"] = match.group(1).strip()
    match = re.match(r"(.+?) -?\d+ Yd pass from (.+?)(?: \(|$)", short, re.I)
    if match:
        names["receiver"], names["passer"] = match.group(1).strip(), match.group(2).strip()
    return names


def normalize_play(play: dict, game: dict) -> dict:
    start, end = play.get("start", {}), play.get("end", {})
    ids = participant_ids(play)
    names = participant_names(play)
    team_id = ref_id(play.get("team")) or ref_id(start.get("team"))
    team_by_id = {game["away"]["id"]: game["away"]["abbreviation"],
                  game["home"]["id"]: game["home"]["abbreviation"]}
    kickoff = datetime.fromisoformat(game["kickoff"].replace("Z", "+00:00"))
    return {
        "provider": "espn",
        "provider_event_id": game["event_id"],
        "game": f"{game['away']['abbreviation']} @ {game['home']['abbreviation']}",
        "game_date": kickoff.astimezone(ET).date().isoformat(),
        "away_team": game["away"]["abbreviation"],
        "home_team": game["home"]["abbreviation"],
        "provider_play_id": str(play.get("id")),
        "sequence_number": int(play.get("sequenceNumber") or 0),
        "provider_wallclock": play.get("wallclock"),
        "provider_modified_at": play.get("modified"),
        "quarter": play.get("period", {}).get("number"),
        "clock": play.get("clock", {}).get("displayValue"),
        "clock_seconds": play.get("clock", {}).get("value"),
        "away_score": play.get("awayScore"),
        "home_score": play.get("homeScore"),
        "possession_team_id": team_id,
        "possession": team_by_id.get(team_id),
        "down": start.get("down"),
        "distance": start.get("distance"),
        "field_position": start.get("possessionText"),
        "yard_line": start.get("yardLine"),
        "yards_to_endzone": start.get("yardsToEndzone"),
        "end_down": end.get("down"),
        "end_distance": end.get("distance"),
        "end_field_position": end.get("possessionText"),
        "play_type_id": play.get("type", {}).get("id"),
        "play_type": play.get("type", {}).get("text"),
        "description": play.get("text"),
        "short_description": play.get("shortText"),
        "yards": play.get("statYardage"),
        "scoring_play": play.get("scoringPlay", False),
        "turnover": play.get("isTurnover", False),
        "penalty": play.get("isPenalty", False),
        "passer_id": ids["passer"],
        "passer": names["passer"],
        "rusher_id": ids["rusher"],
        "rusher": names["rusher"],
        "receiver_id": ids["receiver"],
        "receiver": names["receiver"],
    }


def fingerprint(play: dict) -> str:
    # ESPN often touches modified timestamps after a game without changing the play.
    stable = {key: value for key, value in play.items() if key != "provider_modified_at"}
    raw = json.dumps(stable, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def player_stats(plays: dict[str, dict]) -> dict[str, dict]:
    stats: dict[str, dict] = {}

    def row(player_id: str | None, name: str | None) -> dict | None:
        key = player_id or name
        if not key:
            return None
        return stats.setdefault(key, {
            "player_id": player_id,
            "player": name,
            "receiving_yards": 0,
            "receptions": 0,
            "targets": 0,
            "rushing_yards": 0,
            "carries": 0,
        })

    for play in plays.values():
        description = play.get("description") or ""
        if "no play" in description.lower():
            continue
        play_type = (play.get("play_type") or "").lower()
        receiver = row(play.get("receiver_id"), play.get("receiver"))
        if receiver and "pass" in play_type and "sack" not in play_type:
            receiver["targets"] += 1
            completed = play_type in {"pass reception", "passing touchdown"} or (
                "completion" in play_type and "incompletion" not in play_type
            )
            if completed:
                receiver["receptions"] += 1
                receiver["receiving_yards"] += int(play.get("yards") or 0)
        rusher = row(play.get("rusher_id"), play.get("rusher"))
        if rusher and "rush" in play_type:
            rusher["carries"] += 1
            rusher["rushing_yards"] += int(play.get("yards") or 0)
    return stats


class GameWriter:
    def __init__(self, output_dir: Path, game: dict):
        output_dir.mkdir(parents=True, exist_ok=True)
        kickoff = datetime.fromisoformat(game["kickoff"].replace("Z", "+00:00")).astimezone(ET)
        game_key = f"{kickoff:%Y-%m-%d}_{game['away']['abbreviation']}_{game['home']['abbreviation']}"
        self.path = output_dir / f"espn_pbp_{game_key}.jsonl.gz"
        self.seen: dict[str, str] = {}
        self.revisions: dict[str, int] = {}
        if self.path.exists():
            try:
                with gzip.open(self.path, "rt", encoding="utf-8") as existing:
                    for line in existing:
                        record = json.loads(line)
                        play_id = record.get("provider_play_id")
                        if play_id and record.get("source_fingerprint"):
                            self.seen[play_id] = record["source_fingerprint"]
                            self.revisions[play_id] = int(record.get("revision", 1))
            except (EOFError, gzip.BadGzipFile, json.JSONDecodeError):
                pass
        self.handle = gzip.open(self.path, "at", encoding="utf-8")

    def write(self, record: dict) -> None:
        self.handle.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
        self.handle.flush()

    def status(self, status: str, **details) -> None:
        self.write({"record_type": "collector_status", "received_at": utc_now(),
                    "status": status, **details})

    def close(self) -> None:
        self.handle.close()


async def sleep_or_stop(seconds: float) -> None:
    end = time.monotonic() + seconds
    while not STOP and time.monotonic() < end:
        await asyncio.sleep(min(1, end - time.monotonic()))


async def collect_game(game: dict, args: argparse.Namespace) -> None:
    writer = GameWriter(args.output_dir, game)
    latest: dict[str, dict] = {}
    failures = polls = 0
    last_error_write = 0.0
    final_seen_at = None
    kickoff = datetime.fromisoformat(game["kickoff"].replace("Z", "+00:00"))
    writer.status("started", game=game, poll_seconds=args.poll_seconds)
    print(f"{game['event_id']} {game['away']['abbreviation']} @ "
          f"{game['home']['abbreviation']} -> {writer.path}", flush=True)
    try:
        while not STOP:
            now = datetime.now(timezone.utc)
            diagnostic = args.once or args.max_polls
            if not diagnostic and now < kickoff - timedelta(minutes=args.start_minutes_before):
                await sleep_or_stop(min(60, (kickoff - now).total_seconds()))
                continue
            if not diagnostic and now > kickoff + timedelta(hours=args.stop_hours_after):
                writer.status("stopped", reason="game_window_complete")
                return

            poll_started = time.monotonic()
            try:
                payload = await asyncio.to_thread(
                    get_json,
                    PLAYS_URL.format(event_id=game["event_id"]),
                    {"limit": 300},
                )
                received_at = utc_now()
                failures_before = failures
                failures = 0
                if failures_before:
                    writer.status("source_recovered", previous_failures=failures_before)

                source_plays = sorted(
                    (normalize_play(play, game) for play in payload.get("items", [])),
                    key=lambda play: play["sequence_number"],
                )
                current_ids = {play["provider_play_id"] for play in source_plays}
                if writer.seen and not latest:
                    latest.update({play["provider_play_id"]: play for play in source_plays})

                changed = 0
                for play in source_plays:
                    play_id = play["provider_play_id"]
                    source_fingerprint = fingerprint(play)
                    old_fingerprint = writer.seen.get(play_id)
                    latest[play_id] = play
                    if source_fingerprint == old_fingerprint:
                        continue
                    changed += 1
                    revision = writer.revisions.get(play_id, 0) + 1
                    stats = player_stats(latest)
                    receiver_state = stats.get(play.get("receiver_id") or play.get("receiver"), {})
                    rusher_state = stats.get(play.get("rusher_id") or play.get("rusher"), {})
                    is_receiving = bool(play.get("receiver_id") or play.get("receiver"))
                    is_rushing = bool(play.get("rusher_id") or play.get("rusher"))
                    writer.write({
                        "record_type": "play_new" if revision == 1 else "play_correction",
                        "received_at": received_at,
                        "revision": revision,
                        "source_fingerprint": source_fingerprint,
                        **play,
                        "receiver_receiving_yards": receiver_state.get("receiving_yards"),
                        "receiver_receptions": receiver_state.get("receptions"),
                        "receiver_targets": receiver_state.get("targets"),
                        "rusher_rushing_yards": rusher_state.get("rushing_yards"),
                        "rusher_carries": rusher_state.get("carries"),
                        "player_id": play.get("receiver_id") if is_receiving
                        else play.get("rusher_id") if is_rushing else None,
                        "player": play.get("receiver") if is_receiving
                        else play.get("rusher") if is_rushing else None,
                        "prop_type": "receiving_yards" if is_receiving
                        else "rushing_yards" if is_rushing else None,
                        "threshold": None,
                        "stat_value": receiver_state.get("receiving_yards") if is_receiving
                        else rusher_state.get("rushing_yards") if is_rushing else None,
                    })
                    writer.seen[play_id] = source_fingerprint
                    writer.revisions[play_id] = revision

                if latest and len(current_ids) >= max(1, int(len(latest) * 0.8)):
                    for missing_id in sorted(set(latest) - current_ids):
                        old = latest.pop(missing_id)
                        writer.write({
                            "record_type": "play_removed",
                            "received_at": received_at,
                            "provider_event_id": game["event_id"],
                            "provider_play_id": missing_id,
                            "previous_sequence_number": old.get("sequence_number"),
                            "game": old.get("game"),
                            "game_date": old.get("game_date"),
                            "away_team": old.get("away_team"),
                            "home_team": old.get("home_team"),
                            "player_id": old.get("receiver_id") or old.get("rusher_id"),
                            "player": old.get("receiver") or old.get("rusher"),
                            "prop_type": "receiving_yards" if old.get("receiver_id") or old.get("receiver")
                            else "rushing_yards" if old.get("rusher_id") or old.get("rusher")
                            else None,
                            "threshold": None,
                        })
                        writer.seen.pop(missing_id, None)
                polls += 1
                if changed:
                    print(f"{received_at} {game['event_id']}: {changed} new/changed plays", flush=True)
                if any(play.get("play_type") == "End of Game" for play in source_plays):
                    if final_seen_at is None:
                        final_seen_at = time.monotonic()
                        writer.status("final_seen", final_grace_minutes=args.final_grace_minutes)
                    elif time.monotonic() - final_seen_at >= args.final_grace_minutes * 60:
                        writer.status("stopped", reason="final_grace_complete")
                        return
            except Exception as exc:
                failures += 1
                if failures == 1 or time.monotonic() - last_error_write >= 30:
                    writer.status("source_error", consecutive_failures=failures,
                                  error=f"{type(exc).__name__}: {exc}")
                    last_error_write = time.monotonic()
                print(f"{game['event_id']} ESPN error ({failures}): {exc}", flush=True)

            if args.once or (args.max_polls and polls >= args.max_polls):
                writer.status("stopped", reason="requested_poll_limit")
                return
            await sleep_or_stop(max(0, args.poll_seconds - (time.monotonic() - poll_started)))
    finally:
        writer.close()


def parse_args() -> argparse.Namespace:
    volume = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
    default_output = os.getenv("NFL_LIVE_OUTPUT_DIR") or (
        str(Path(volume) / "nfl_live") if volume else "data/raw/nfl_live"
    )
    tomorrow = (datetime.now(ET).date() + timedelta(days=1)).isoformat()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=os.getenv("NFL_LIVE_DATE", tomorrow))
    parser.add_argument("--game-ids", default=os.getenv("NFL_LIVE_GAME_IDS"),
                        help="Optional comma-separated ESPN event IDs")
    parser.add_argument("--poll-seconds", type=float,
                        default=float(os.getenv("NFL_LIVE_POLL_SECONDS", "1.5")))
    parser.add_argument("--output-dir", type=Path, default=Path(default_output))
    parser.add_argument("--start-minutes-before", type=float,
                        default=float(os.getenv("NFL_LIVE_START_MINUTES_BEFORE", "10")))
    parser.add_argument("--stop-hours-after", type=float,
                        default=float(os.getenv("NFL_LIVE_STOP_HOURS_AFTER", "8")))
    parser.add_argument("--final-grace-minutes", type=float,
                        default=float(os.getenv("NFL_LIVE_FINAL_GRACE_MINUTES", "10")))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--max-polls", type=int)
    return parser.parse_args()


async def run(args: argparse.Namespace) -> None:
    backoff = 1
    while not STOP:
        try:
            games = await asyncio.to_thread(discover_games, args.date)
            break
        except Exception as exc:
            print(f"ESPN discovery error: {exc}; retrying in {backoff}s", flush=True)
            await sleep_or_stop(backoff)
            backoff = min(backoff * 2, 30)
    else:
        return
    wanted = {item.strip() for item in args.game_ids.split(",")} if args.game_ids else None
    if wanted:
        games = [game for game in games if game["event_id"] in wanted]
    if not games:
        raise SystemExit(f"No ESPN NFL games found for {args.date}")
    print(f"Discovered {len(games)} ESPN NFL games for {args.date}", flush=True)
    results = await asyncio.gather(
        *(supervise_game(game, args) for game in games),
        return_exceptions=True,
    )
    for game, result in zip(games, results):
        if isinstance(result, BaseException):
            print(f"{game['event_id']} collector stopped unexpectedly: {result}", flush=True)


async def supervise_game(game: dict, args: argparse.Namespace) -> None:
    backoff = 1
    while not STOP:
        try:
            await collect_game(game, args)
            return
        except Exception as exc:
            print(
                f"{game['event_id']} collector error: {exc}; retrying in {backoff}s",
                flush=True,
            )
            await sleep_or_stop(backoff)
            backoff = min(backoff * 2, 30)


def request_stop(_signum, _frame) -> None:
    global STOP
    STOP = True


def main() -> None:
    args = parse_args()
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
