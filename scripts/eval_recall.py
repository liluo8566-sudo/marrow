#!/usr/bin/env python3
"""eval_recall.py — hit-rate evaluation harness for marrow recall.

Usage:
    python scripts/eval_recall.py [--out eval-results/baseline.json] [--tag baseline]

Judge: claude-haiku-4-5-20251001 via ANTHROPIC_API_KEY (env) or claude -p fallback.
Cache: eval-results/judge_cache.json keyed by (prompt_hash, recalled_id).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

DB_PATH = "/Users/Gabrielle/.config/marrow/marrow.db"

# Use storage.connect() so sqlite-vec extension is loaded automatically.
WORKTREE = Path(__file__).parent.parent
CACHE_PATH = WORKTREE / "eval-results" / "judge_cache.json"

JUDGE_MODEL = "claude-haiku-4-5-20251001"
BATCH_SIZE = 10


# ── sample prompts ─────────────────────────────────────────────────────────────

def sample_prompts(conn: sqlite3.Connection) -> list[dict]:
    """120 user prompts: 40 recent, 40 mid (id 700-1500), 40 old (<700)."""
    recent = conn.execute(
        "SELECT e.id, e.content, s.cwd, e.timestamp, e.session_id "
        "FROM events e LEFT JOIN sessions s ON s.sid = e.session_id "
        "WHERE e.role='user' AND e.content IS NOT NULL AND length(e.content) > 10 "
        "ORDER BY e.id DESC LIMIT 40"
    ).fetchall()

    mid = conn.execute(
        "SELECT e.id, e.content, s.cwd, e.timestamp, e.session_id "
        "FROM events e LEFT JOIN sessions s ON s.sid = e.session_id "
        "WHERE e.role='user' AND e.id BETWEEN 700 AND 1500 "
        "  AND e.content IS NOT NULL AND length(e.content) > 10 "
        "ORDER BY RANDOM() LIMIT 40"
    ).fetchall()

    old = conn.execute(
        "SELECT e.id, e.content, s.cwd, e.timestamp, e.session_id "
        "FROM events e LEFT JOIN sessions s ON s.sid = e.session_id "
        "WHERE e.role='user' AND e.id < 700 "
        "  AND e.content IS NOT NULL AND length(e.content) > 10 "
        "ORDER BY RANDOM() LIMIT 40"
    ).fetchall()

    out = []
    for strat, rows in [("recent", recent), ("mid", mid), ("old", old)]:
        for r in rows:
            out.append({
                "event_id": r["id"],
                "prompt": (r["content"] or "")[:500],
                "cwd": r["cwd"] or "/Users/Gabrielle/CC-Lab/marrow",
                "timestamp": r["timestamp"] or "",
                "stratum": strat,
            })
    return out


# ── recall runner ──────────────────────────────────────────────────────────────

def run_recall(conn: sqlite3.Connection, prompt: str, cwd: str) -> list[dict]:
    from marrow.recall import recall_with_config
    try:
        return recall_with_config(conn, prompt, limit=5, budget_chars=600, current_cwd=cwd)
    except Exception as e:
        print(f"  [warn] recall error: {e}", file=sys.stderr)
        return []


# ── judge ──────────────────────────────────────────────────────────────────────

def _prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode()).hexdigest()[:16]


def _judge_prompt(user_prompt: str, recalled_content: str) -> str:
    return f"""You are evaluating a memory recall system. Given a user prompt and a recalled memory item, score the relevance 0-2:

0 = unrelated noise (different topic, different person, irrelevant context)
1 = tangentially related (same person/topic but wrong context or timeframe)
2 = directly useful context for answering this prompt

User prompt: {user_prompt[:300]}

Recalled item: {recalled_content[:300]}

Reply with ONLY a single digit: 0, 1, or 2."""


async def _judge_one_sdk(client, user_prompt: str, recalled_content: str) -> int:
    """Judge via Anthropic SDK."""
    msg = await client.messages.create(
        model=JUDGE_MODEL,
        max_tokens=10,
        messages=[{"role": "user", "content": _judge_prompt(user_prompt, recalled_content)}],
    )
    text = msg.content[0].text.strip()
    for ch in text:
        if ch in "012":
            return int(ch)
    return 0


def _judge_one_cli(user_prompt: str, recalled_content: str) -> int:
    """Judge via claude -p fallback."""
    prompt = _judge_prompt(user_prompt, recalled_content)
    try:
        result = subprocess.run(
            ["claude", "-p", "--model", JUDGE_MODEL, prompt],
            capture_output=True, text=True, timeout=30,
        )
        text = result.stdout.strip()
        for ch in text:
            if ch in "012":
                return int(ch)
    except Exception:
        pass
    return 0


# ── async batch judging ────────────────────────────────────────────────────────

async def judge_batch_sdk(client, pairs: list[tuple[str, str, str, int]]) -> dict[tuple[str, int], int]:
    """Judge a batch of (cache_key_prompt, cache_key_id, prompt, content) via SDK.
    Returns {(prompt_hash, recalled_id): score}."""
    semaphore = asyncio.Semaphore(BATCH_SIZE)

    async def _judge(ph: str, rid: int, prompt: str, content: str) -> tuple[tuple[str, int], int]:
        async with semaphore:
            score = await _judge_one_sdk(client, prompt, content)
            return (ph, rid), score

    tasks = [_judge(ph, rid, p, c) for ph, rid, p, c in pairs]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    out = {}
    for r in results:
        if isinstance(r, Exception):
            continue
        k, v = r
        out[k] = v
    return out


def judge_batch_cli(pairs: list[tuple[str, str, str, int]]) -> dict[tuple[str, int], int]:
    """Sequential CLI fallback."""
    out = {}
    for i, (ph, rid, prompt, content) in enumerate(pairs):
        if i % 10 == 0:
            print(f"  CLI judge {i}/{len(pairs)}...", file=sys.stderr)
        score = _judge_one_cli(prompt, content)
        out[(ph, rid)] = score
    return out


# ── cache ──────────────────────────────────────────────────────────────────────

def load_cache() -> dict:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if CACHE_PATH.exists():
        try:
            raw = json.loads(CACHE_PATH.read_text())
            # keys are "hash|id" strings
            return {tuple(k.split("|", 1)): v for k, v in raw.items()}
        except Exception:
            pass
    return {}


def save_cache(cache: dict) -> None:
    serializable = {f"{k[0]}|{k[1]}": v for k, v in cache.items()}
    CACHE_PATH.write_text(json.dumps(serializable, indent=2))


# ── metrics ───────────────────────────────────────────────────────────────────

def compute_metrics(
    prompts: list[dict],
    recall_results: list[list[dict]],
    scores: dict[tuple[str, int], int],
) -> dict:
    hit_scores = []
    soft_scores = []
    total_chars = 0
    kind_counts: dict[str, int] = defaultdict(int)
    total_slots = 0

    per_prompt = []
    for p, results in zip(prompts, recall_results):
        ph = _prompt_hash(p["prompt"])
        prompt_scores = []
        for r in results:
            rid = r.get("id", 0)
            s = scores.get((ph, str(rid)), scores.get((ph, rid), 0))
            prompt_scores.append(s)
            total_chars += len(r.get("content", ""))
            kind = r.get("kind") or r.get("role") or "event"
            if kind in (None, "event"):
                kind = "event"
            kind_counts[kind] += 1
            total_slots += 1

        hit2 = sum(1 for s in prompt_scores if s >= 2)
        hit1 = sum(1 for s in prompt_scores if s >= 1)
        hit_scores.append(hit2 / 5.0)
        soft_scores.append(hit1 / 5.0)
        per_prompt.append({
            "event_id": p["event_id"],
            "prompt": p["prompt"][:120],
            "cwd": p["cwd"],
            "stratum": p["stratum"],
            "scores": prompt_scores,
            "hit2": hit2,
            "hit1": hit1,
            "results": [
                {"id": r.get("id"), "kind": r.get("kind") or r.get("role") or "event",
                 "content": (r.get("content") or "")[:80],
                 "score": scores.get((ph, str(r.get("id", 0))), scores.get((ph, r.get("id", 0)), 0))}
                for r in results
            ],
        })

    n = len(prompts)
    kind_pct = {k: round(v / max(total_slots, 1) * 100, 1) for k, v in kind_counts.items()}

    return {
        "n_prompts": n,
        "hit_rate_at5": round(sum(hit_scores) / n, 4),
        "soft_hit_at5": round(sum(soft_scores) / n, 4),
        "mean_chars_per_recall": round(total_chars / n, 1),
        "total_chars": total_chars,
        "kind_pct": kind_pct,
        "per_prompt": per_prompt,
    }


# ── main ───────────────────────────────────────────────────────────────────────

async def main_async(args: argparse.Namespace) -> None:
    out_path = WORKTREE / (args.out or "eval-results/baseline.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print("Opening DB...", file=sys.stderr)
    from marrow.storage import connect as _storage_connect
    conn = _storage_connect(DB_PATH)

    print("Sampling prompts...", file=sys.stderr)
    prompts = sample_prompts(conn)
    print(f"  {len(prompts)} prompts ({sum(1 for p in prompts if p['stratum']=='recent')} recent, "
          f"{sum(1 for p in prompts if p['stratum']=='mid')} mid, "
          f"{sum(1 for p in prompts if p['stratum']=='old')} old)", file=sys.stderr)

    print("Running recall for each prompt...", file=sys.stderr)
    recall_results = []
    for i, p in enumerate(prompts):
        if i % 20 == 0:
            print(f"  recall {i}/{len(prompts)}...", file=sys.stderr)
        results = run_recall(conn, p["prompt"], p["cwd"])
        recall_results.append(results)

    # Build judge pairs (skip cached)
    cache = load_cache()
    pairs_to_judge: list[tuple[str, str, str, str]] = []
    for p, results in zip(prompts, recall_results):
        ph = _prompt_hash(p["prompt"])
        for r in results:
            rid = r.get("id", 0)
            key = (ph, str(rid))
            if key not in cache:
                content = r.get("content") or ""
                pairs_to_judge.append((ph, str(rid), p["prompt"], content))

    print(f"Judge pairs: {len(pairs_to_judge)} new, {len(cache)} cached", file=sys.stderr)

    # Judge
    new_scores: dict[tuple[str, str], int] = {}
    if pairs_to_judge:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if api_key:
            try:
                import anthropic
                client = anthropic.AsyncAnthropic(api_key=api_key)
                print(f"  Using Anthropic SDK ({JUDGE_MODEL})...", file=sys.stderr)
                new_scores = await judge_batch_sdk(client, pairs_to_judge)
            except ImportError:
                print("  anthropic SDK not installed, falling back to CLI", file=sys.stderr)
                new_scores = judge_batch_cli(pairs_to_judge)
        else:
            print(f"  No ANTHROPIC_API_KEY, using claude -p CLI ({JUDGE_MODEL})...", file=sys.stderr)
            # Run CLI judges in async subprocess batches
            new_scores = await _judge_cli_async(pairs_to_judge)

        cache.update(new_scores)
        save_cache(cache)

    # Merge cache keys (str and int variants)
    merged_scores: dict = {}
    for k, v in cache.items():
        merged_scores[k] = v

    metrics = compute_metrics(prompts, recall_results, merged_scores)
    metrics["tag"] = args.tag
    metrics["model"] = JUDGE_MODEL

    out_path.write_text(json.dumps(metrics, indent=2, ensure_ascii=False))
    print(f"\nResults written to {out_path}", file=sys.stderr)

    # Summary
    print(f"\n=== {args.tag} ===")
    print(f"hit_rate@5:          {metrics['hit_rate_at5']:.4f}")
    print(f"soft_hit@5:          {metrics['soft_hit_at5']:.4f}")
    print(f"mean_chars_per_recall: {metrics['mean_chars_per_recall']:.1f}")
    print(f"kind_pct:            {metrics['kind_pct']}")


async def _judge_cli_async(pairs: list[tuple]) -> dict:
    """Async CLI judge — 10 concurrent subprocesses."""
    semaphore = asyncio.Semaphore(BATCH_SIZE)

    async def _one(ph: str, rid: str, prompt: str, content: str):
        async with semaphore:
            judge_text = _judge_prompt(prompt, content)
            try:
                proc = await asyncio.create_subprocess_exec(
                    "claude", "-p", "--model", JUDGE_MODEL, judge_text,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=45)
                text = stdout.decode().strip()
                for ch in text:
                    if ch in "012":
                        return (ph, rid), int(ch)
            except Exception as e:
                pass
            return (ph, rid), 0

    tasks = [_one(ph, rid, p, c) for ph, rid, p, c in pairs]
    total = len(tasks)
    done = 0
    results = {}
    for coro in asyncio.as_completed(tasks):
        k, v = await coro
        results[k] = v
        done += 1
        if done % 50 == 0:
            print(f"  judged {done}/{total}...", file=sys.stderr)
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="eval-results/baseline.json")
    parser.add_argument("--tag", default="baseline")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
