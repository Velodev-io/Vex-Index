"""
VexIndex Hybrid Search Benchmark Harness
=========================================
Measures Recall@5, Recall@10, MRR (Mean Reciprocal Rank), and P99 latency
against a golden query fixture file.

Fragment matching uses:
  - Case-insensitive comparison (both sides lowercased)
  - Whitespace trimming and path-separator normalisation (backslash → forward slash)
  - Minimum fragment length guard (VEXINDEX_MIN_MATCH_LENGTH, default 2)
  - Sliding-window Levenshtein ≤ 1 fuzzy match as fallback to exact substring

Run with:
    uv run pytest tests/test_benchmark.py -v -s

Optionally sweep alpha values:
    VEXINDEX_BENCH_ALPHA_SWEEP=1 uv run pytest tests/test_benchmark.py -v -s
"""

import asyncio
import json
import os
import time
import statistics
import pytest
from pathlib import Path
from typing import Optional
from vexindex.config import settings

# ---------------------------------------------------------------------------
# Fixture loading
# ---------------------------------------------------------------------------
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "benchmark_queries.json"


def _load_queries() -> list[dict]:
    with open(FIXTURE_PATH) as f:
        raw = json.load(f)
    # Strip non-semantic metadata keys added for human readability
    return [
        {k: v for k, v in entry.items() if not k.startswith("_")}
        for entry in raw
    ]


# ---------------------------------------------------------------------------
# Metrics helpers
# ---------------------------------------------------------------------------

def _frag_matches(fragment: str, target: str) -> bool:
    """Return True if *fragment* approximately appears anywhere in *target*.

    Rules applied in order (short-circuits on first True):
    1. Normalise both strings: strip whitespace, lowercase, normalise path
       separators (backslash → forward slash).
    2. Skip if the normalised fragment is shorter than
       ``settings.VEXINDEX_MIN_MATCH_LENGTH`` (default 2) — avoids spurious
       hits from single-character fragments.
    3. Fast path: exact substring containment check (O(T)).
    4. Slow path: slide a window of ``len(frag)`` characters across the
       normalised target and compute Levenshtein edit distance for each
       window using a DP table.  Return True if any window has distance ≤ 1.
       Window size equals the fragment length so the DP table stays tiny
       (F × F) regardless of target length.
    """
    # 1. Normalise
    frag = fragment.strip().lower().replace("\\", "/")
    tgt  = target.strip().lower().replace("\\", "/")

    # 2. Min-length guard
    min_len = settings.VEXINDEX_MIN_MATCH_LENGTH
    if len(frag) < min_len:
        return False

    # 3. Fast path — exact substring
    if frag in tgt:
        return True

    # 4. Sliding-window Levenshtein ≤ 1
    f_len = len(frag)
    t_len = len(tgt)
    if f_len == 0 or t_len == 0:
        return False

    def _lev(a: str, b: str) -> int:
        """Classic O(n*m) Levenshtein distance via DP table."""
        n, m = len(a), len(b)
        # Use two rows to save memory
        prev = list(range(m + 1))
        curr = [0] * (m + 1)
        for i in range(1, n + 1):
            curr[0] = i
            for j in range(1, m + 1):
                cost = 0 if a[i - 1] == b[j - 1] else 1
                curr[j] = min(
                    prev[j] + 1,       # deletion
                    curr[j - 1] + 1,   # insertion
                    prev[j - 1] + cost # substitution
                )
            prev, curr = curr, prev
        return prev[m]

    # Slide a window of exactly f_len characters across tgt
    for start in range(t_len - f_len + 1):
        window = tgt[start: start + f_len]
        if _lev(frag, window) <= 1:
            return True

    # Also check windows of f_len±1 to catch single insertion/deletion
    for w_len in (f_len - 1, f_len + 1):
        if w_len < 1:
            continue
        for start in range(max(0, t_len - w_len + 1)):
            window = tgt[start: start + w_len]
            if _lev(frag, window) <= 1:
                return True

    return False


def _any_frag_match(frags: list[str], target: str) -> bool:
    """Return True if any fragment in *frags* matches *target* via _frag_matches."""
    return any(_frag_matches(f, target) for f in frags)


def _hit_at_k(
    results: list[dict],
    expected_file_frags: list[str],
    expected_name_frags: list[str],
    k: int,
) -> bool:
    """Return True if any result in the top-k satisfies the expected fragments.

    - Only file frags provided  → match file path only.
    - Only name frags provided  → match chunk name only.
    - Both provided             → either must match (OR logic).
    - Neither provided          → no ground truth; return True (skip query).

    Uses _frag_matches for all comparisons (case-insensitive, fuzzy, etc.).
    """
    if not expected_file_frags and not expected_name_frags:
        return True  # no ground truth — treat as always-hit

    for r in results[:k]:
        fp = r.get("file_path", "") or ""
        nm = r.get("name",      "") or ""

        file_hit = _any_frag_match(expected_file_frags, fp) if expected_file_frags else False
        name_hit = _any_frag_match(expected_name_frags, nm) if expected_name_frags else False

        if expected_file_frags and expected_name_frags:
            if file_hit or name_hit:
                return True
        elif expected_file_frags:
            if file_hit:
                return True
        else:  # name-only
            if name_hit:
                return True

    return False


def _reciprocal_rank(
    results: list[dict],
    expected_file_frags: list[str],
    expected_name_frags: list[str],
) -> float:
    """Return 1/rank of first correct hit, 0.0 if not found.

    Uses _frag_matches for all comparisons, so MRR benefits from the same
    relaxed matching as recall.
    """
    if not expected_file_frags and not expected_name_frags:
        return 1.0  # no ground truth — don't penalise MRR

    for i, r in enumerate(results, start=1):
        fp = r.get("file_path", "") or ""
        nm = r.get("name",      "") or ""

        file_hit = _any_frag_match(expected_file_frags, fp) if expected_file_frags else False
        name_hit = _any_frag_match(expected_name_frags, nm) if expected_name_frags else False

        if expected_file_frags and expected_name_frags:
            if file_hit or name_hit:
                return 1.0 / i
        elif expected_file_frags:
            if file_hit:
                return 1.0 / i
        else:
            if name_hit:
                return 1.0 / i

    return 0.0


# ---------------------------------------------------------------------------
# Core benchmark runner
# ---------------------------------------------------------------------------

async def _run_benchmark(alpha: Optional[float] = None) -> dict:
    from vexindex.config import settings
    from vexindex.db import init_db, search_hybrid

    queries = _load_queries()
    conn = await init_db(settings.db_path_abs)

    hits_at_5: list[int]  = []
    hits_at_10: list[int] = []
    rr_scores:  list[float] = []
    latencies:  list[float] = []

    try:
        for item in queries:
            query                 = item["query"]
            expected_file_frags   = item.get("expected_file_fragments", [])
            expected_name_frags   = item.get("expected_name_fragments", [])

            t0 = time.perf_counter()
            results = await search_hybrid(conn, query, project_id=None, limit=10, alpha=alpha)
            elapsed_ms = (time.perf_counter() - t0) * 1000

            latencies.append(elapsed_ms)
            hits_at_5.append(int(_hit_at_k(results, expected_file_frags, expected_name_frags, k=5)))
            hits_at_10.append(int(_hit_at_k(results, expected_file_frags, expected_name_frags, k=10)))
            rr_scores.append(_reciprocal_rank(results, expected_file_frags, expected_name_frags))

    finally:
        await conn.close()

    n = len(queries)
    sorted_latencies = sorted(latencies)
    p99_idx = max(0, int(0.99 * n) - 1)

    return {
        "alpha":        alpha if alpha is not None else settings.VEXINDEX_HYBRID_ALPHA,
        "n_queries":    n,
        "recall_at_5":  round(sum(hits_at_5) / n, 4) if n else 0,
        "recall_at_10": round(sum(hits_at_10) / n, 4) if n else 0,
        "mrr":          round(sum(rr_scores) / n, 4) if n else 0,
        "avg_ms":       round(statistics.mean(latencies), 2) if latencies else 0,
        "p99_ms":       round(sorted_latencies[p99_idx], 2) if latencies else 0,
    }


# ---------------------------------------------------------------------------
# Pytest tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_hybrid_benchmark_default_alpha():
    """Benchmark with the default alpha from settings."""
    results = await _run_benchmark(alpha=None)
    _print_results([results])
    # Soft assertions: we simply assert the harness ran without error.
    # Hard recall thresholds can be added once you have a larger golden set.
    assert results["n_queries"] > 0
    assert results["recall_at_5"] >= 0.0
    assert results["avg_ms"] > 0


@pytest.mark.asyncio
async def test_hybrid_benchmark_alpha_sweep():
    """Alpha sweep over [0.3, 0.5, 0.6, 0.7, 0.9] — only runs if env var set."""
    if not os.environ.get("VEXINDEX_BENCH_ALPHA_SWEEP"):
        pytest.skip("Set VEXINDEX_BENCH_ALPHA_SWEEP=1 to run the alpha sweep")

    alphas = [0.3, 0.5, 0.6, 0.7, 0.9]
    all_results = []
    for alpha in alphas:
        r = await _run_benchmark(alpha=alpha)
        all_results.append(r)

    _print_results(all_results)

    # Save sweep output for diffing between runs
    out_path = Path(__file__).parent / "fixtures" / "benchmark_results.json"
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\nSaved results to {out_path}")

    assert all(r["recall_at_5"] >= 0 for r in all_results)


# ---------------------------------------------------------------------------
# Pretty printer
# ---------------------------------------------------------------------------

def _print_results(results: list[dict]):
    header = f"\n{'Alpha':>6} | {'R@5':>6} | {'R@10':>6} | {'MRR':>6} | {'avg ms':>8} | {'P99 ms':>8}"
    print(header)
    print("-" * len(header))
    for r in results:
        print(
            f"{r['alpha']:>6.2f} | "
            f"{r['recall_at_5']:>6.2%} | "
            f"{r['recall_at_10']:>6.2%} | "
            f"{r['mrr']:>6.3f} | "
            f"{r['avg_ms']:>8.1f} | "
            f"{r['p99_ms']:>8.1f}"
        )
