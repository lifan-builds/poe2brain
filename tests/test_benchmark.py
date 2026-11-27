# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 lifan-builds
"""Unit tests for the PoE2Bench-0.5.5 benchmark suite and deterministic scorer."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmarks.scorer import render_comparison_markdown, score_answer, score_dataset  # noqa: E402

BENCH_PATH = REPO_ROOT / "benchmarks" / "poe2_bench_0_5_5.json"


def test_benchmark_schema_and_fixtures_exist() -> None:
    bench = json.loads(BENCH_PATH.read_text(encoding="utf-8"))
    assert bench["benchmark_id"] == "PoE2Bench-0.5.5"
    assert bench["patch"] == "0.5.5"
    questions = bench["questions"]
    assert len(questions) == 16
    seen_ids: set[str] = set()
    for q in questions:
        assert q["id"] not in seen_ids
        seen_ids.add(q["id"])
        assert q["checks"], f"{q['id']} must define deterministic checks"
        assert q["poe2brain_commands"], f"{q['id']} must list poe2brain verification commands"
        for rel in q.get("fixture_files", []):
            assert (REPO_ROOT / rel).exists(), f"Missing benchmark fixture {rel}"


def test_ground_truth_summaries_score_100_percent_with_zero_hallucinations() -> None:
    bench = json.loads(BENCH_PATH.read_text(encoding="utf-8"))
    preds = {q["id"]: q["ground_truth_summary"] for q in bench["questions"]}
    scored = score_dataset(BENCH_PATH, preds)
    failed = [r for r in scored["per_question"] if not r["passed"]]
    assert not failed, f"Ground truth failed checks: {failed}"
    assert scored["pass_rate_pct"] == 100.0
    assert scored["check_accuracy_pct"] == 100.0
    assert scored["hallucination_rate_pct"] == 0.0


def test_scorer_catches_poe1_hallucinations_without_false_positives_on_negation() -> None:
    bench = json.loads(BENCH_PATH.read_text(encoding="utf-8"))
    q01 = next(q for q in bench["questions"] if q["id"] == "Q01")
    q02 = next(q for q in bench["questions"] if q["id"] == "Q02")
    q12 = next(q for q in bench["questions"] if q["id"] == "Q12")

    bad_q01 = score_answer(
        q01,
        "In PoE2, Chaos hit damage bypasses Energy Shield directly. Bleeding hits ES first, "
        "and a 1-Life CI character dies to Bleeding.",
    )
    assert not bad_q01["passed"]
    assert bad_q01["hallucinated"]
    assert len(bad_q01["triggered_traps"]) == 2

    good_q02 = score_answer(
        q02,
        "Base maximum passive Block chance is 50% (not 75% like PoE1). "
        "Glancing Blows does not double Block chance; it makes Chance to Evade Unlucky and "
        "Chance to Deflect Lucky. Deflection replaced Spell Suppression with 40% mitigation "
        "and 95% cap.",
    )
    assert good_q02["passed"]
    assert not good_q02["hallucinated"]

    bad_q12 = score_answer(q12, 'Use `"!ever|pene|weak|player r"` (26 chars).')
    assert not bad_q12["passed"]
    assert bad_q12["hallucinated"]

    good_q12 = score_answer(q12, 'Use `!"ever|pene|weak|player r"` (26 characters).')
    assert good_q12["passed"]
    assert not good_q12["hallucinated"]


def test_render_comparison_markdown() -> None:
    bench = json.loads(BENCH_PATH.read_text(encoding="utf-8"))
    empty_score = score_dataset(BENCH_PATH, {})
    perfect_score = score_dataset(
        BENCH_PATH, {q["id"]: q["ground_truth_summary"] for q in bench["questions"]}
    )
    md = render_comparison_markdown(empty_score, perfect_score, "test-model")
    assert "Exact Task Pass Rate" in md
    assert "100.0%" in md
    assert "0/16 (0.0%)" in md


def test_archived_codex_results_rescore_consistently() -> None:
    results_path = REPO_ROOT / "benchmarks" / "results" / "codex_0_5_5.json"
    assert results_path.exists()
    data = json.loads(results_path.read_text(encoding="utf-8"))
    for mode in ("baseline", "poe2brain"):
        block = data["modes"][mode]
        preds = {qid: rec["answer"] for qid, rec in block["responses"].items()}
        rescored = score_dataset(BENCH_PATH, preds)
        assert rescored["questions_passed"] == block["summary"]["questions_passed"]
        assert rescored["checks_passed"] == block["summary"]["checks_passed"]
        assert rescored["hallucinated_questions"] == block["summary"]["hallucinated_questions"]
    assert data["modes"]["poe2brain"]["summary"]["pass_rate_pct"] == 100.0
    assert data["modes"]["poe2brain"]["summary"]["hallucination_rate_pct"] == 0.0

