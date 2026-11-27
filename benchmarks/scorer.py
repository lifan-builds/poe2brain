# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 lifan-builds
"""Deterministic scorer and report generator for PoE2Bench-0.5.5."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9_])-?\d{1,3}(?:,\d{3})*(?:\.\d+)?|-?\d+(?:\.\d+)?")
_NEGATION_WINDOW_RE = re.compile(
    r"\b(?:not|no|never|doesn't|does\s+not|don't|do\s+not|isn't|is\s+not|aren't|are\s+not|"
    r"won't|cannot|can't|no\s+longer|removed|phased\s+out|unlike\s+poe\s*1|in\s+poe\s*1|"
    r"poe\s*1\s+had|formerly|previously|instead\s+of|rather\s+than|false|wrong|incorrect|"
    r"trap|myth|misconception|not\s+true)\b",
    re.IGNORECASE,
)
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_QUOTED_EXCL_RE = re.compile(r'!"([^"\n]+)"')


def _normalize_text(text: str) -> str:
    """Normalize markdown emphasis, Unicode dashes/minus signs, and smart quotes."""
    s = text.replace("\u2212", "-").replace("\u2013", "-").replace("\u2014", "-")
    s = s.replace("\u2018", "'").replace("\u2019", "'")
    s = s.replace("\u201c", '"').replace("\u201d", '"')
    s = s.replace("**", "").replace("__", "")
    # Keep single backticks for regex extraction checks via raw text; strip bold/italic markers
    return s


def _extract_numbers(text: str) -> list[float]:
    vals: list[float] = []
    for raw in _NUMBER_RE.findall(text):
        cleaned = raw.replace(",", "")
        try:
            vals.append(float(cleaned))
        except ValueError:
            continue
    return vals


def _eval_poe2_waystone_regex(answer: str, check: dict[str, Any]) -> tuple[bool, str]:
    max_chars = int(check.get("max_chars", 50))
    must_match = check.get("must_match_mods", [])
    must_not_match = check.get("must_not_match_mods", [])

    candidates: list[str] = []
    for m in _BACKTICK_RE.finditer(answer):
        s = m.group(1).strip()
        if s.startswith("!") and '"' in s:
            candidates.append(s)
    for m in _QUOTED_EXCL_RE.finditer(answer):
        candidates.append(m.group(0).strip())

    seen: set[str] = set()
    uniq: list[str] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            uniq.append(c)

    if not uniq:
        return False, 'No PoE2 negated stash regex (!"...") found in answer'

    best_err = ""
    for cand in uniq:
        if len(cand) > max_chars:
            best_err = f"Candidate `{cand}` has length {len(cand)} > {max_chars}"
            continue
        m = _QUOTED_EXCL_RE.fullmatch(cand)
        if not m:
            best_err = f'Candidate `{cand}` is not in strict PoE2 `!"..."` syntax'
            continue
        inner = m.group(1)
        try:
            compiled = re.compile(inner, re.IGNORECASE)
        except re.error as exc:
            best_err = f"Invalid regex `{inner}`: {exc}"
            continue

        missed = [mod for mod in must_match if not compiled.search(mod)]
        false_pos = [mod for mod in must_not_match if compiled.search(mod)]
        if not missed and not false_pos:
            return True, f"Valid {len(cand)}-char PoE2 regex `{cand}`"
        best_err = (
            f"Candidate `{cand}` (len={len(cand)}) missed={len(missed)} false_pos={len(false_pos)}"
        )

    return False, best_err or "No valid PoE2 regex matched all targets"


def _eval_check(raw_answer: str, norm_answer: str, check: dict[str, Any]) -> dict[str, Any]:
    ctype = check["type"]
    cid = check["id"]
    label = check["label"]

    if ctype == "regex_any":
        matched = any(re.search(p, norm_answer, re.IGNORECASE) for p in check["patterns"])
        return {
            "id": cid,
            "label": label,
            "passed": matched,
            "detail": "matched" if matched else "missing",
        }

    if ctype == "regex_all":
        missing = [p for p in check["patterns"] if not re.search(p, norm_answer, re.IGNORECASE)]
        detail = "matched" if not missing else f"missing {len(missing)}/{len(check['patterns'])}"
        return {"id": cid, "label": label, "passed": not missing, "detail": detail}

    if ctype == "number_present":
        target = float(check["target"])
        rel_tol = check.get("rel_tol")
        abs_tol = check.get("abs_tol")
        suffix = check.get("suffix", "")
        if suffix:
            pat = re.compile(
                rf"(?<![A-Za-z0-9_])(-?\d{{1,3}}(?:,\d{{3}})*(?:\.\d+)?|-?\d+(?:\.\d+)?)\s*{suffix}",
                re.IGNORECASE,
            )
            nums = [float(m.group(1).replace(",", "")) for m in pat.finditer(norm_answer)]
        else:
            nums = _extract_numbers(norm_answer)
        best_diff: float | None = None
        for val in nums:
            diff = abs(val - target)
            ok = False
            if abs_tol is not None and diff <= float(abs_tol):
                ok = True
            if rel_tol is not None and target != 0 and (diff / abs(target)) <= float(rel_tol):
                ok = True
            if ok:
                return {
                    "id": cid,
                    "label": label,
                    "passed": True,
                    "detail": f"found {val} (target {target})",
                }
            if best_diff is None or diff < best_diff:
                best_diff = diff
        return {
            "id": cid,
            "label": label,
            "passed": False,
            "detail": f"target {target} not within tolerance (closest diff={best_diff})",
        }

    if ctype == "poe2_waystone_regex":
        ok, detail = _eval_poe2_waystone_regex(raw_answer, check)
        return {"id": cid, "label": label, "passed": ok, "detail": detail}

    raise ValueError(f"Unknown check type: {ctype}")


def _eval_trap(norm_answer: str, trap: dict[str, Any]) -> bool:
    """Return True if the answer affirms the hallucination trap without negating it."""
    pattern = re.compile(trap["pattern"], re.IGNORECASE)
    for m in pattern.finditer(norm_answer):
        start = max(0, m.start() - 55)
        end = min(len(norm_answer), m.end() + 35)
        before = norm_answer[start : m.start()].split("\n")[-1].split(".")[-1]
        after = norm_answer[m.end() : end].split("\n")[0].split(".")[0]
        local_clause = f"{before} {m.group(0)} {after}"
        if not _NEGATION_WINDOW_RE.search(local_clause):
            return True
    return False


def score_answer(question: dict[str, Any], answer: str) -> dict[str, Any]:
    norm = _normalize_text(answer)
    check_results = [_eval_check(answer, norm, c) for c in question["checks"]]
    checks_passed = sum(1 for r in check_results if r["passed"])
    total_checks = len(check_results)

    triggered_traps = [
        {"id": t["id"], "label": t["label"]}
        for t in question.get("hallucination_traps", [])
        if _eval_trap(norm, t)
    ]

    all_passed = checks_passed == total_checks and len(triggered_traps) == 0
    return {
        "id": question["id"],
        "title": question["title"],
        "pillar": question["pillar"],
        "passed": all_passed,
        "checks_passed": checks_passed,
        "total_checks": total_checks,
        "check_accuracy": round(checks_passed / total_checks, 4) if total_checks else 1.0,
        "hallucinated": len(triggered_traps) > 0,
        "triggered_traps": triggered_traps,
        "check_results": check_results,
    }


def score_dataset(
    bench_path: Path,
    predictions: dict[str, str],
) -> dict[str, Any]:
    bench = json.loads(bench_path.read_text(encoding="utf-8"))
    questions = bench["questions"]
    per_question: list[dict[str, Any]] = []
    by_pillar: dict[str, dict[str, int]] = {}

    for q in questions:
        ans = predictions.get(q["id"], "")
        res = score_answer(q, ans)
        per_question.append(res)
        p = by_pillar.setdefault(
            q["pillar"],
            {"total": 0, "passed": 0, "checks_passed": 0, "checks_total": 0, "hallucinated": 0},
        )
        p["total"] += 1
        p["passed"] += int(res["passed"])
        p["checks_passed"] += res["checks_passed"]
        p["checks_total"] += res["total_checks"]
        p["hallucinated"] += int(res["hallucinated"])

    total_q = len(per_question)
    passed_q = sum(1 for r in per_question if r["passed"])
    total_c = sum(r["total_checks"] for r in per_question)
    passed_c = sum(r["checks_passed"] for r in per_question)
    halluc_q = sum(1 for r in per_question if r["hallucinated"])

    return {
        "benchmark_id": bench["benchmark_id"],
        "patch": bench["patch"],
        "total_questions": total_q,
        "questions_passed": passed_q,
        "pass_rate_pct": round(100.0 * passed_q / total_q, 1) if total_q else 0.0,
        "checks_passed": passed_c,
        "checks_total": total_c,
        "check_accuracy_pct": round(100.0 * passed_c / total_c, 1) if total_c else 0.0,
        "hallucinated_questions": halluc_q,
        "hallucination_rate_pct": round(100.0 * halluc_q / total_q, 1) if total_q else 0.0,
        "by_pillar": by_pillar,
        "per_question": per_question,
    }


def render_comparison_markdown(
    baseline_score: dict[str, Any],
    treatment_score: dict[str, Any],
    model_label: str,
) -> str:
    b, t = baseline_score, treatment_score
    d_pass = round(t["pass_rate_pct"] - b["pass_rate_pct"], 1)
    d_chk = round(t["check_accuracy_pct"] - b["check_accuracy_pct"], 1)
    d_hal = round(t["hallucination_rate_pct"] - b["hallucination_rate_pct"], 1)

    lines = [
        f"## PoE2Bench-0.5.5 Comparison (`{model_label}`)",
        "",
        "| Metric | Without `poe2brain` | With `poe2brain` | Delta |",
        "| :--- | :---: | :---: | :---: |",
        (
            f"| **Exact Task Pass Rate** | {b['questions_passed']}/{b['total_questions']} "
            f"({b['pass_rate_pct']}%) | **{t['questions_passed']}/{t['total_questions']} "
            f"({t['pass_rate_pct']}%)** | **+{d_pass} pp** |"
        ),
        (
            f"| **Sub-Check Factual Accuracy** | {b['checks_passed']}/{b['checks_total']} "
            f"({b['check_accuracy_pct']}%) | **{t['checks_passed']}/{t['checks_total']} "
            f"({t['check_accuracy_pct']}%)** | **+{d_chk} pp** |"
        ),
        (
            f"| **PoE1 / Stale-Patch Hallucinations** | "
            f"{b['hallucinated_questions']}/{b['total_questions']} "
            f"({b['hallucination_rate_pct']}%) | "
            f"**{t['hallucinated_questions']}/{t['total_questions']} "
            f"({t['hallucination_rate_pct']}%)** | **{d_hal} pp** |"
        ),
        "",
        "### By Pillar",
        "",
        "| Pillar | Questions | Without `poe2brain` | With `poe2brain` |",
        "| :--- | :---: | :---: | :---: |",
    ]
    for pillar, b_stats in b["by_pillar"].items():
        t_stats = t["by_pillar"][pillar]
        b_pct = round(100 * b_stats["passed"] / b_stats["total"], 1)
        t_pct = round(100 * t_stats["passed"] / t_stats["total"], 1)
        lines.append(
            f"| {pillar} | {b_stats['total']} | "
            f"{b_stats['passed']}/{b_stats['total']} ({b_pct}%) | "
            f"**{t_stats['passed']}/{t_stats['total']} ({t_pct}%)** |"
        )

    lines.extend([
        "",
        "### Per-Question Breakdown",
        "",
        "| ID | Pillar | Task | Without `poe2brain` | With `poe2brain` |",
        "| :--- | :--- | :--- | :---: | :---: |",
    ])
    t_map = {r["id"]: r for r in t["per_question"]}
    for bq in b["per_question"]:
        tq = t_map[bq["id"]]
        b_cnt = f"{bq['checks_passed']}/{bq['total_checks']}"
        t_cnt = f"{tq['checks_passed']}/{tq['total_checks']}"
        b_badge = f"PASS ({b_cnt})" if bq["passed"] else f"FAIL ({b_cnt})"
        if bq["hallucinated"]:
            b_badge += " [Hallucination]"
        t_badge = f"**PASS** ({t_cnt})" if tq["passed"] else f"FAIL ({t_cnt})"
        lines.append(f"| {bq['id']} | {bq['pillar']} | {bq['title']} | {b_badge} | {t_badge} |")
    return "\n".join(lines) + "\n"
