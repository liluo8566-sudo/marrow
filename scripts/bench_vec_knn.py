"""Synthetic KNN timing bench for sqlite-vec events_vec table.

Measures mean + p95 query latency (ms) at 10k / 25k / 50k / 100k rows
with 1024-dim float32 vectors. Uses a temp DB — no real data touched.

Usage:
    python scripts/bench_vec_knn.py

Outputs a table + a recommended vec_window_days value (largest bucket
whose mean < 100 ms, scaled to days at 550 events/day).
"""
from __future__ import annotations

import os
import random
import sqlite3
import struct
import tempfile
import time

import sqlite_vec

BUCKETS = [10_000, 25_000, 50_000, 100_000]
DIM = 1024
N_QUERIES = 20
K = 5
EVENTS_PER_DAY = 550  # real observed rate


def _rand_vec() -> bytes:
    return struct.pack(f"{DIM}f", *[random.gauss(0, 1) for _ in range(DIM)])


def _build_db(path: str, n_rows: int) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute(
        f"CREATE VIRTUAL TABLE events_vec USING vec0(embedding float[{DIM}])"
    )
    # Batch insert for speed
    batch = 500
    for start in range(0, n_rows, batch):
        end = min(start + batch, n_rows)
        conn.executemany(
            "INSERT INTO events_vec(rowid, embedding) VALUES (?, ?)",
            [(i + 1, _rand_vec()) for i in range(start, end)],
        )
    conn.commit()
    return conn


def _bench_queries(conn: sqlite3.Connection) -> list[float]:
    """Run N_QUERIES KNN queries, return list of elapsed ms per query."""
    times: list[float] = []
    for _ in range(N_QUERIES):
        q = _rand_vec()
        t0 = time.perf_counter()
        conn.execute(
            "SELECT rowid, distance FROM events_vec"
            " WHERE embedding MATCH ?"
            " AND k = ?",
            (q, K),
        ).fetchall()
        times.append((time.perf_counter() - t0) * 1000)
    return times


def _p95(vals: list[float]) -> float:
    s = sorted(vals)
    idx = int(len(s) * 0.95)
    return s[min(idx, len(s) - 1)]


def main() -> None:
    print(f"sqlite-vec KNN bench — {DIM}d float32, k={K}, {N_QUERIES} queries each")
    print(f"{'rows':>10}  {'days@550/d':>12}  {'mean ms':>10}  {'p95 ms':>10}")
    print("-" * 50)

    results: list[tuple[int, float, float]] = []

    for n in BUCKETS:
        fd, path = tempfile.mkstemp(suffix=".db", prefix="bench_vec_")
        os.close(fd)
        try:
            conn = _build_db(path, n)
            times = _bench_queries(conn)
            conn.close()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

        mean_ms = sum(times) / len(times)
        p95_ms = _p95(times)
        days = n / EVENTS_PER_DAY
        results.append((n, mean_ms, p95_ms))
        print(f"{n:>10,}  {days:>12.0f}  {mean_ms:>10.1f}  {p95_ms:>10.1f}")

    print()
    # Recommend: largest bucket with mean < 100 ms → days
    chosen_days = None
    for n, mean_ms, _ in reversed(results):
        if mean_ms < 100:
            chosen_days = int(n / EVENTS_PER_DAY)
            break
    if chosen_days is None:
        chosen_days = int(results[0][0] / EVENTS_PER_DAY)
        print("WARNING: even smallest bucket exceeded 100 ms mean — using minimum.")

    print(f"Recommendation: vec_window_days = {chosen_days}  "
          f"(largest bucket with mean < 100 ms @ {EVENTS_PER_DAY} events/day)")


if __name__ == "__main__":
    main()
