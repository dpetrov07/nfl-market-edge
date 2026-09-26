"""Synthetic soak for the Kalshi collector's long-lived state."""

from __future__ import annotations

import argparse
from collections import deque
import gc
import json
from pathlib import Path
import resource
from statistics import quantiles
import tempfile
import time
import tracemalloc

from nfl_market_edge.kalshi import MarketState, RecentSet
from scripts.collect_live_combo_slate import SlateRegistry, Writer


def mib(value: int) -> float:
    return round(value / 1024 / 1024, 2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=250_000)
    parser.add_argument("--sample-every", type=int, default=25_000)
    args = parser.parse_args()

    manifest = {
        "slate_id": "memory-soak",
        "event_games": {"GAME1": "A @ B", "GAME2": "C @ D"},
    }
    legs = [
        {"event_ticker": "SERIES-GAME1", "market_ticker": "leg-1"},
        {"event_ticker": "SERIES-GAME2", "market_ticker": "leg-2"},
    ]
    tracemalloc.start()
    with tempfile.TemporaryDirectory() as directory:
        writer = Writer(Path(directory) / "events.jsonl.gz")
        registry = SlateRegistry(
            manifest,
            writer,
            max_combos=1_000,
            max_components=100,
            combo_idle_seconds=3600,
        )
        state = MarketState([], max_tickers=1_100, dedupe_entries=10_000)
        communications = RecentSet(10_000)
        fills = RecentSet(10_000)
        latencies_ms = deque(maxlen=10_000)
        samples = []

        for index in range(args.iterations):
            ticker = f"combo-{index}"
            started = time.perf_counter()
            writer.write(
                {
                    "record_type": "communication",
                    "communication_type": "rfq_created",
                    "market_ticker": ticker,
                    "rfq_id": f"rfq-{index}",
                    "payload": {"mve_selected_legs": legs},
                },
                flush=True,
            )
            latencies_ms.append((time.perf_counter() - started) * 1000)
            communications.add(("rfq_created", f"rfq-{index}", index))
            fills.add(f"fill-{index}")
            new_tickers = registry.add_combo(
                {"ticker": ticker, "mve_selected_legs": legs}, "soak"
            )
            state.remove_tickers(registry.take_evicted_tickers())
            state.add_tickers(
                [value for value in new_tickers if value.startswith("leg-")],
                pinned=True,
            )
            state.add_tickers([ticker])
            state.process(
                {
                    "type": "orderbook_snapshot",
                    "msg": {
                        "market_ticker": ticker,
                        "yes": [[price, index % 100 + 1] for price in range(1, 50)],
                        "no": [[price, index % 100 + 1] for price in range(51, 100)],
                    },
                },
                "now",
            )
            state.process(
                {
                    "type": "trade",
                    "msg": {"market_ticker": ticker, "trade_id": f"trade-{index}"},
                },
                "now",
            )

            if (index + 1) % args.sample_every == 0:
                gc.collect()
                current, peak = tracemalloc.get_traced_memory()
                rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                if rss < 10_000_000:  # Linux reports KiB; macOS reports bytes.
                    rss *= 1024
                sample = {
                    "records": index + 1,
                    "traced_mib": mib(current),
                    "peak_mib": mib(peak),
                    "max_rss_mib": mib(rss),
                }
                samples.append(sample)
                print(json.dumps(sample), flush=True)

        writer.close()
        growth = samples[-1]["traced_mib"] - samples[0]["traced_mib"]
        latency_values = sorted(latencies_ms)
        report = {
            "result": "pass" if growth < 8 else "fail",
            "iterations": args.iterations,
            "post_warmup_growth_mib": round(growth, 2),
            "rfq_flush_p50_ms": round(latency_values[len(latency_values) // 2], 3),
            "rfq_flush_p99_ms": round(quantiles(latency_values, n=100)[98], 3),
            "active_combos": len(registry.combos),
            "market_state_tickers": len(state.tickers),
            "trade_dedupe_entries": len(state.seen_trade_ids),
            "communication_dedupe_entries": len(communications),
            "fill_dedupe_entries": len(fills),
            "records_written": writer.records_written,
        }
        print(json.dumps(report), flush=True)
        if report["result"] != "pass":
            raise SystemExit(1)


if __name__ == "__main__":
    main()
