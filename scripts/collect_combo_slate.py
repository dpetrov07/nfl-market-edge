"""Launch synchronized Kalshi and configured sportsbook collectors for one slate."""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from pathlib import Path


def bovada_commands(manifest: dict, root: Path, duration: float | None, config: dict) -> list[list[str]]:
    if manifest["league"].lower() != "nfl":
        raise ValueError("the current Bovada adapter supports NFL only")
    command = [
        sys.executable,
        "-m",
        "scripts.collect_live_bovada_ws",
        "--date",
        manifest["date"],
        "--output-dir",
        str(root / manifest["slate_id"] / "sportsbooks" / "bovada"),
    ]
    for game in config.get("games", []):
        command.extend(("--game", game))
    if duration:
        command.extend(("--run-seconds", str(duration)))
    return [command]


def polling_commands(manifest: dict, root: Path, _duration: float | None, config: dict) -> list[list[str]]:
    sport = "ncaaf" if manifest["league"].lower() == "cfb" else "nfl"
    books = config.get("books", ["bovada"])
    commands = []
    for game in config.get("games", []):
        slug = "_".join(part.lower() for part in game.replace("@", " ").split())
        commands.append(
            [
                sys.executable,
                "-m",
                "scripts.collect_live_sportsbook_props",
                "--game",
                game,
                "--sport",
                sport,
                "--books",
                ",".join(books),
                "--interval",
                str(config.get("interval_seconds", 30)),
                "--output-dir",
                str(root / manifest["slate_id"] / "sportsbooks" / "polling" / slug),
            ]
        )
    return commands


SPORTSBOOK_COMMANDS = {"bovada": bovada_commands, "multi_book": polling_commands}


def commands(args: argparse.Namespace, manifest: dict) -> list[list[str]]:
    result = [
        [
            sys.executable,
            "-m",
            "scripts.collect_live_combo_slate",
            "--manifest",
            str(args.manifest),
            "--output-dir",
            str(args.output_dir),
        ]
    ]
    if args.duration:
        result[0].extend(("--duration", str(args.duration)))
    for name, config in manifest.get("sportsbooks", {}).items():
        if not config.get("enabled", True):
            continue
        try:
            builder = SPORTSBOOK_COMMANDS[name]
        except KeyError as exc:
            raise ValueError(
                f"unknown sportsbook {name!r}; add one command builder to SPORTSBOOK_COMMANDS"
            ) from exc
        result.extend(builder(manifest, args.output_dir, args.duration, config))
    return result


async def run(args: argparse.Namespace, manifest: dict) -> None:
    processes = []
    for command in commands(args, manifest):
        print("starting:", " ".join(command), flush=True)
        processes.append(await asyncio.create_subprocess_exec(*command))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            loop.add_signal_handler(getattr(signal, name), stop.set)
    waits = [asyncio.create_task(process.wait()) for process in processes]
    stop_task = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait(waits + [stop_task], return_when=asyncio.FIRST_COMPLETED)
        if stop_task in done:
            return
        first = next(task for task in done if task is not stop_task)
        if first.result() != 0:
            raise RuntimeError(f"collector exited with status {first.result()}")
        if not args.duration:
            raise RuntimeError("collector stopped before the slate runner")
    finally:
        stop_task.cancel()
        for process in processes:
            if process.returncode is None:
                process.terminate()
        await asyncio.gather(*(process.wait() for process in processes), return_exceptions=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("data/live/combo_slates"))
    parser.add_argument("--duration", type=float)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    manifest = json.loads(args.manifest.read_text())
    asyncio.run(run(args, manifest))


if __name__ == "__main__":
    main()
