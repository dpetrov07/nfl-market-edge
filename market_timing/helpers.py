"""Shared Bovada/Kalshi timing helpers."""

from __future__ import annotations

import bisect
import heapq
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyarrow.parquet as pq


def no_vig(over_odds, under_odds):
    try:
        over, under = 1 / float(over_odds), 1 / float(under_odds)
        return over / (over + under) if over > 0 and under > 0 else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def read_mapping(path: Path):
    grouped = defaultdict(lambda: defaultdict(list))
    for row in pq.read_table(path).to_pylist():
        key = (row["player_key"], row["prop_type"], row["market_threshold"])
        grouped[row["game"]][key].append(row)
    return grouped


def canonical_pairs(mapping_rows, bovada):
    counts = Counter(zip(bovada["selection_id"], bovada["threshold"]))
    pairs = {}
    for key, rows in mapping_rows.items():
        chosen = {}
        for side in ("over", "under"):
            options = [row for row in rows if row["bovada_side"] == side]
            if options:
                chosen[side] = min(
                    options,
                    key=lambda row: (
                        bool(row["bovada_is_alternate"]),
                        -counts[(row["bovada_selection_id"], row["market_threshold"])],
                        row["bovada_selection_id"],
                    ),
                )
        if len(chosen) == 2:
            pairs[key] = chosen
    return pairs


def bovada_price_events(
    path: Path,
    mapping_rows,
    min_move: float = 0.02,
    burst_seconds: float = 0.25,
    isolation_seconds: float = 12.0,
):
    """Return clean two-sided price-to-price changes; suspensions/reopens are excluded."""
    columns = [
        "received_at", "threshold", "side", "decimal_odds", "state",
        "market_id", "selection_id",
    ]
    data = pq.read_table(path, columns=columns).to_pydict()
    pairs = canonical_pairs(mapping_rows, data)
    selected = {}
    for key, pair in pairs.items():
        for side, row in pair.items():
            selected[(row["bovada_selection_id"], row["market_threshold"])] = (key, side)

    rows = []
    for values in zip(*(data[column] for column in columns)):
        row = dict(zip(columns, values))
        match = selected.get((row["selection_id"], row["threshold"]))
        if match:
            row["key"], row["side"] = match
            rows.append(row)
    rows.sort(key=lambda row: row["received_at"])

    bursts, current = [], []
    for row in rows:
        if current and (row["received_at"] - current[-1]["received_at"]).total_seconds() > burst_seconds:
            bursts.append(current)
            current = []
        current.append(row)
    if current:
        bursts.append(current)

    states, last_good, suspension_started, candidates = {}, {}, {}, []
    for burst in bursts:
        before, touched = {}, set()
        for row in burst:
            key = row["key"]
            if key not in before:
                pair = states.get(key, {})
                before[key] = no_vig(
                    pair.get("over", {}).get("decimal_odds")
                    if pair.get("over", {}).get("state") == "open" else None,
                    pair.get("under", {}).get("decimal_odds")
                    if pair.get("under", {}).get("state") == "open" else None,
                )
            states.setdefault(key, {})[row["side"]] = row
            touched.add(key)

        for key in touched:
            pair = states[key]
            after = no_vig(
                pair.get("over", {}).get("decimal_odds")
                if pair.get("over", {}).get("state") == "open" else None,
                pair.get("under", {}).get("decimal_odds")
                if pair.get("under", {}).get("state") == "open" else None,
            )
            old = before[key]
            at = max(row["received_at"] for row in burst if row["key"] == key)
            kind = None
            prior = old
            if old is not None and after is None:
                suspension_started[key] = at
            elif old is None and after is not None and key in suspension_started:
                prior = last_good.get(key)
                kind = "reopen"
                suspension_started.pop(key, None)
            elif old is not None and after is not None:
                kind = "price"
            if kind and prior is not None and abs(after - prior) >= min_move:
                candidates.append({
                    "key": key,
                    "at": at,
                    "old_prob": prior,
                    "new_prob": after,
                    "delta": after - prior,
                    "direction": 1 if after > prior else -1,
                    "kind": kind,
                    "pair": pairs[key],
                })
            if after is not None:
                last_good[key] = after

    by_key = defaultdict(list)
    for event in sorted(candidates, key=lambda row: row["at"]):
        collapsed = by_key[event["key"]]
        if (
            collapsed
            and collapsed[-1]["direction"] == event["direction"]
            and (event["at"] - collapsed[-1]["at"]).total_seconds() <= 1.0
        ):
            collapsed[-1]["new_prob"] = event["new_prob"]
            collapsed[-1]["delta"] = event["new_prob"] - collapsed[-1]["old_prob"]
            collapsed[-1]["at"] = event["at"]
        else:
            collapsed.append(event)

    clean = []
    for events in by_key.values():
        for index, event in enumerate(events):
            prior_ok = index == 0 or (
                event["at"] - events[index - 1]["at"]
            ).total_seconds() > isolation_seconds
            next_ok = index + 1 == len(events) or (
                events[index + 1]["at"] - event["at"]
            ).total_seconds() > isolation_seconds
            if prior_ok and next_ok:
                clean.append(event)
    clean.sort(key=lambda row: row["at"])
    return [event for event in clean if event["kind"] == "price"], len(pairs)


def annotate_bovada_payloads(
    path: Path,
    events,
    burst_seconds: float = 0.25,
    game_wide_selections: int = 10,
):
    """Classify the source burst carrying each price event."""
    rows = pq.read_table(path, columns=["received_at", "selection_id"]).to_pylist()
    rows.sort(key=lambda row: row["received_at"])
    clusters, current, start = [], [], None
    for row in rows:
        if current and (row["received_at"] - current[-1]["received_at"]).total_seconds() > burst_seconds:
            clusters.append((start, current[-1]["received_at"], len({item["selection_id"] for item in current})))
            current = []
        if not current:
            start = row["received_at"]
        current.append(row)
    if current:
        clusters.append((start, current[-1]["received_at"], len({item["selection_id"] for item in current})))

    starts = [cluster[0] for cluster in clusters]
    game = events[0]["pair"]["over"]["game"] if events else ""
    for event in events:
        index = max(0, bisect.bisect_right(starts, event["at"]) - 1)
        cluster = clusters[index]
        if not cluster[0] <= event["at"] <= cluster[1]:
            cluster = min(
                clusters,
                key=lambda item: min(
                    abs((event["at"] - item[0]).total_seconds()),
                    abs((event["at"] - item[1]).total_seconds()),
                ),
            )
        count = cluster[2]
        event["payload_selection_count"] = count
        event["batch_class"] = (
            "isolated" if count <= 2
            else "small_batch" if count < game_wide_selections
            else "game_wide"
        )
        event["episode_id"] = f"{game}|{cluster[0].isoformat()}"


def scan_kalshi_horizons(path: Path, events, horizons=(1, 3, 5, 10, 30)):
    """Attach executable quote state, horizon moves, and initial quote availability."""
    horizons = tuple(sorted(horizons))
    events.sort(key=lambda event: event["at"])
    for scan_id, event in enumerate(events):
        event["_scan_id"] = scan_id
        event["ticker"] = event["pair"]["over"]["kalshi_market_ticker"]

    event_index, serial = 0, 0
    state, due, lookup = {}, [], {event["_scan_id"]: event for event in events}
    active = defaultdict(set)

    def snapshot(event, horizon):
        quote = state.get(event["ticker"])
        if not quote:
            event["outcomes"][horizon] = None
            return
        price, size = quote[event["exec_side"]], quote[event["exec_side"] + "_size"]
        event["outcomes"][horizon] = (
            None if price is None or size is None or size <= 0
            else (price - event["initial_price"]) * event["direction"]
        )

    def activate(event):
        nonlocal serial
        quote = state.get(event["ticker"])
        side = "ask" if event["direction"] > 0 else "bid"
        if not quote:
            return
        price, size = quote[side], quote[side + "_size"]
        if price is None or size is None or size <= 0:
            return
        event.update({
            "exec_side": side,
            "initial_price": price,
            "initial_size": size,
            "spread": quote["ask"] - quote["bid"]
            if quote["ask"] is not None and quote["bid"] is not None else None,
            "quote_age_seconds": (event["at"] - quote["at"]).total_seconds(),
            "residual": (event["new_prob"] - price) * event["direction"],
            "outcomes": {},
            "available_until": None,
            "availability_censored": False,
            "min_size_while_available": size,
        })
        event["neighbor_quotes"] = {}
        for label, neighbor in event.get("neighbor_tickers", {}).items():
            neighbor_quote = state.get(neighbor["ticker"])
            if neighbor_quote:
                event["neighbor_quotes"][label] = {
                    **neighbor,
                    "yes_bid": neighbor_quote["bid"],
                    "yes_ask": neighbor_quote["ask"],
                    "quote_age_seconds": (
                        event["at"] - neighbor_quote["at"]
                    ).total_seconds(),
                }
        active[event["ticker"]].add(event["_scan_id"])
        for horizon in horizons:
            serial += 1
            heapq.heappush(due, (event["at"] + timedelta(seconds=horizon), serial, event, horizon))

    def flush(now):
        while due and due[0][0] < now:
            at, _, event, horizon = heapq.heappop(due)
            snapshot(event, horizon)
            if horizon == horizons[-1]:
                if event["available_until"] is None:
                    event["available_until"] = at
                    event["availability_censored"] = True
                active[event["ticker"]].discard(event["_scan_id"])

    columns = [
        "received_at", "event_type", "market_ticker", "yes_bid", "yes_bid_size",
        "yes_ask", "yes_ask_size",
    ]
    for batch in pq.ParquetFile(path).iter_batches(batch_size=65_536, columns=columns):
        for row in batch.to_pylist():
            at = row["received_at"]
            flush(at)
            while event_index < len(events) and events[event_index]["at"] <= at:
                activate(events[event_index])
                event_index += 1
            flush(at)
            if row["event_type"] != "top_of_book":
                continue
            ticker = row["market_ticker"]
            quote = {
                "at": at,
                "bid": row["yes_bid"], "bid_size": row["yes_bid_size"],
                "ask": row["yes_ask"], "ask_size": row["yes_ask_size"],
            }
            state[ticker] = quote
            for scan_id in list(active.get(ticker, ())):
                event = lookup[scan_id]
                side = event["exec_side"]
                price, size = quote[side], quote[side + "_size"]
                if event["available_until"] is not None:
                    continue
                if (
                    price is None or size is None or size <= 0
                    or (price - event["initial_price"]) * event["direction"] > 1e-12
                ):
                    event["available_until"] = at
                else:
                    event["min_size_while_available"] = min(
                        event["min_size_while_available"], size
                    )

    while event_index < len(events):
        activate(events[event_index])
        event_index += 1
    flush(datetime.max.replace(tzinfo=timezone.utc))

    for event in events:
        if "initial_price" in event:
            event["available_seconds"] = (
                event["available_until"] - event["at"]
            ).total_seconds()
    return events
