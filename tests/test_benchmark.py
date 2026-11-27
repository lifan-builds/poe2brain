# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 lifan-builds
"""Unit tests for the PoE2Bench-0.5.5 benchmark suite, scorer, and Round-1/2 engine fixes."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmarks.scorer import render_comparison_markdown, score_answer, score_dataset  # noqa: E402

from poe2brain.knowledge.service import KnowledgeService  # noqa: E402
from poe2brain.knowledge.waystone_regex import WaystoneRegexEngine  # noqa: E402
from poe2brain.trade.advisor import TradeAdvisor  # noqa: E402

BENCH_PATH = REPO_ROOT / "benchmarks" / "poe2_bench_0_5_5.json"


def test_benchmark_schema_and_fixtures_exist() -> None:
    bench = json.loads(BENCH_PATH.read_text(encoding="utf-8"))
    assert bench["benchmark_id"] == "PoE2Bench-0.5.5"
    assert bench["patch"] == "0.5.5"
    questions = bench["questions"]
    assert len(questions) == 22
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
    q21 = next(q for q in bench["questions"] if q["id"] == "Q21")

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

    bad_q12 = score_answer(q12, 'Use `"!buil|ever|of l|pene|weak|player r"` (36 chars).')
    assert not bad_q12["passed"]
    assert bad_q12["hallucinated"]

    good_q12 = score_answer(q12, 'Use `!"buil|ever|of l|pene|weak|player r"` (36 characters).')
    assert good_q12["passed"]
    assert not good_q12["hallucinated"]

    good_q21 = score_answer(
        q21,
        'Juicing regex: `"ce:..(1[2-9]|[2-9]\\d|\\d{3})\\d|ze:..([2-9]|1\\d)\\d"` (50 chars). '
        "Elemental damage taken increases by 12% to 32%.",
    )
    assert good_q21["passed"]


def test_render_comparison_markdown() -> None:
    bench = json.loads(BENCH_PATH.read_text(encoding="utf-8"))
    empty_score = score_dataset(BENCH_PATH, {})
    perfect_score = score_dataset(
        BENCH_PATH, {q["id"]: q["ground_truth_summary"] for q in bench["questions"]}
    )
    md = render_comparison_markdown(empty_score, perfect_score, "test-model")
    assert "Exact Task Pass Rate" in md
    assert "100.0%" in md
    assert "0/22 (0.0%)" in md


def test_engine_regression_fixes_from_benchmark_loop() -> None:
    ks = KnowledgeService()
    # 1. wiki mechanic relevance pruning & compact mode eliminate Negative-IIR noise on 'quality'
    q_full = ks.explain_mechanic("quality")
    assert [f.slug for f in q_full.facts] == ["quality_multiplicative_more"]
    q_compact = ks.explain_mechanic("quality", compact=True)
    assert q_compact.keywords == [] and q_compact.trap_warnings == []

    # 2. Chinese gem lookup preserves original query in resolved_from
    gem = ks.inspect_gem("拉基亚塔的流光")
    assert gem.name == "Rakiata's Flow"
    assert gem.resolved_from == "拉基亚塔的流光"

    # 3. Waystone regex: es_ci detects CI, 12-32% max-res copy, exact 125% & 100%/1000% bounds
    eng = WaystoneRegexEngine()
    plan = eng.build_plan(archetypes=["es_ci"], min_waystone_drop_pct=120, min_pack_size_pct=20)
    assert "ci" in plan.detected_archetypes
    mm = next(h for h in plan.hazards if h.hazard_id == "minus_max_resists")
    assert "12–32%" in mm.reason
    jrx = plan.juicing_regex.regex.strip('"')
    assert plan.juicing_regex.within_client_limit
    assert re.search(jrx, "Waystone Drop Chance: +1000% (augmented)", re.I)
    assert re.search(jrx, "Pack Size: +100% (augmented)", re.I)
    assert not re.search(jrx, "Pack Size: +19% (augmented)", re.I)

    plan_125 = eng.build_plan(archetypes=["es_ci"], min_waystone_drop_pct=125)
    jrx_125 = plan_125.juicing_regex.regex.strip('"')
    assert re.search(jrx_125, "Waystone Drop Chance: +125% (augmented)", re.I)
    assert not re.search(jrx_125, "Waystone Drop Chance: +124% (augmented)", re.I)

    # 4. Offline triage respects live_trade=False and counts Normal base open 3P/3S
    dump_text = (REPO_ROOT / "benchmarks" / "fixtures" / "triage_dump_0_5_5.txt").read_text(
        encoding="utf-8"
    )
    advisor = TradeAdvisor()
    rep = advisor.triage_clipboard_dump(dump_text, live_trade=False)
    assert len(rep.entries) == 2
    assert all(e.price_source == "heuristic_offline" for e in rep.entries)
    assert rep.entries[0].open_prefixes == 3 and rep.entries[0].open_suffixes == 3
    assert rep.entries[1].bucket == "salvage_or_vendor"


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
