# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 lifan-builds
"""Reproducible A/B benchmark runner comparing LLMs without vs. with poe2brain.

Usage:
    # Run full A/B benchmark via Codex CLI (or any OpenAI-compatible API)
    uv run python benchmarks/run_benchmark.py run --runner codex-cli \
        --out benchmarks/results/latest.json

    # Score saved prediction JSON files deterministically
    uv run python benchmarks/run_benchmark.py score benchmarks/results/latest.json
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BENCH_PATH = REPO_ROOT / "benchmarks" / "poe2_bench_0_5_5.json"

sys.path.insert(0, str(REPO_ROOT))
from benchmarks.scorer import render_comparison_markdown, score_dataset  # noqa: E402

BASELINE_SYSTEM_PROMPT = (
    "You are answering a Path of Exile 2 (Patch 0.5.5, Forbidden Rites league) question "
    "as a standalone LLM WITHOUT access to poe2brain, external tools, or the internet. "
    "If a local fixture file is explicitly referenced in the question (for example a `.xml` "
    "PoB2 build file or `.filter` file in the current directory), you may read that file directly. "
    "Do NOT search the web or run external game-data tools. Give a concise, direct, technically "
    "precise answer covering every sub-question with exact numbers and rules."
)

POE2BRAIN_SYSTEM_PROMPT = (
    "You are answering a Path of Exile 2 (Patch 0.5.5, Forbidden Rites league) question "
    "WITH access to the local offline `poe2brain` CLI (`uv run poe2 ... --json`). "
    "Before answering, ALWAYS verify every mechanic, ascendancy, base item, crafting probability, "
    "Waystone regex, headless PoB2 calculation, and loot filter validation by running the relevant "
    "`uv run poe2 ... --json` commands in this repository (do NOT search the web). "
    "Use `uv run poe2 --help` or `uv run poe2 <group> --help` if unsure of flags. "
    "Give a concise, direct, technically precise answer grounded strictly in `poe2brain` outputs."
)


def _stage_baseline_dir(fixture_files: list[str]) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="poe2bench_baseline_"))
    for rel in fixture_files:
        src = REPO_ROOT / rel
        dst = tmp / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    return tmp


def _run_codex_single(
    question: dict[str, Any],
    mode: str,
    model: str | None,
    timeout: int,
) -> dict[str, Any]:
    t0 = time.monotonic()
    system = BASELINE_SYSTEM_PROMPT if mode == "baseline" else POE2BRAIN_SYSTEM_PROMPT
    prompt = f"{system}\n\nQuestion ({question['id']}):\n{question['question']}\n"

    tmp_dir: Path | None = None
    if mode == "baseline":
        tmp_dir = _stage_baseline_dir(question.get("fixture_files", []))
        cwd = tmp_dir
        sandbox = "read-only"
    else:
        cwd = REPO_ROOT
        # Needs write access to ~/.cache/poe2brain for PoB2 slots / LuaJIT runtime
        sandbox = "danger-full-access"

    out_file = Path(tempfile.mktemp(prefix=f"poe2bench_{question['id']}_{mode}_", suffix=".txt"))
    cmd = ["codex", "exec", "--skip-git-repo-check", "--sandbox", sandbox, "-o", str(out_file)]
    if model:
        cmd.extend(["-m", model])
    cmd.append(prompt)

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        answer = (
            out_file.read_text(encoding="utf-8").strip()
            if out_file.exists()
            else proc.stdout.strip()
        )
        stderr_tail = proc.stderr[-800:] if proc.stderr else ""
        detected_model = model or ""
        for line in (proc.stderr or "").splitlines():
            if line.strip().startswith("model:"):
                detected_model = line.split(":", 1)[1].strip()
                break
        return {
            "id": question["id"],
            "mode": mode,
            "model": detected_model,
            "returncode": proc.returncode,
            "elapsed_s": round(time.monotonic() - t0, 2),
            "answer": answer,
            "stderr_tail": stderr_tail,
        }
    except subprocess.TimeoutExpired:
        return {
            "id": question["id"],
            "mode": mode,
            "model": model or "",
            "returncode": -1,
            "elapsed_s": round(time.monotonic() - t0, 2),
            "answer": "",
            "stderr_tail": f"TIMEOUT after {timeout}s",
        }
    finally:
        if out_file.exists():
            out_file.unlink(missing_ok=True)
        if tmp_dir and tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _run_openai_single(
    question: dict[str, Any],
    mode: str,
    model: str,
    timeout: int,
) -> dict[str, Any]:
    """Run baseline (no tools) or poe2brain (tool-augmented) against an OpenAI-compatible API."""
    import httpx

    api_key = os.environ.get("OPENAI_API_KEY", "")
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required for --runner openai")

    t0 = time.monotonic()
    user_content = question["question"]
    if mode == "baseline":
        for rel in question.get("fixture_files", []):
            fpath = REPO_ROOT / rel
            if fpath.exists():
                text = fpath.read_text(encoding="utf-8", errors="replace")
                user_content += f"\n\n--- File: {rel} ---\n{text[:32000]}"

    sys_prompt = BASELINE_SYSTEM_PROMPT if mode == "baseline" else POE2BRAIN_SYSTEM_PROMPT
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_content},
    ]

    tools = (
        [
            {
                "type": "function",
                "function": {
                    "name": "run_poe2_cli",
                    "description": (
                        "Execute a read-only `uv run poe2 ...` command and return stdout/stderr."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "args": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Args after `poe2`, e.g. ['wiki', 'mechanic', 'block']"
                                ),
                            }
                        },
                        "required": ["args"],
                    },
                },
            }
        ]
        if mode == "poe2brain"
        else None
    )

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    answer = ""
    with httpx.Client(timeout=timeout) as client:
        for _step in range(8):
            payload: dict[str, Any] = {"model": model, "messages": messages, "temperature": 0.0}
            if tools:
                payload["tools"] = tools
            resp = client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
            resp.raise_for_status()
            msg = resp.json()["choices"][0]["message"]
            messages.append(msg)
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                answer = (msg.get("content") or "").strip()
                break
            for tc in tool_calls:
                fn_args = json.loads(tc["function"]["arguments"])
                cli_args = [str(a) for a in fn_args.get("args", [])]
                proc = subprocess.run(
                    ["uv", "run", "poe2", *cli_args],
                    cwd=str(REPO_ROOT),
                    text=True,
                    capture_output=True,
                    timeout=60,
                )
                out = (proc.stdout or proc.stderr)[:16000]
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": out})

    return {
        "id": question["id"],
        "mode": mode,
        "model": model,
        "returncode": 0,
        "elapsed_s": round(time.monotonic() - t0, 2),
        "answer": answer,
        "stderr_tail": "",
    }


def run_benchmark(args: argparse.Namespace) -> int:
    bench_path = Path(args.bench)
    bench = json.loads(bench_path.read_text(encoding="utf-8"))
    questions = bench["questions"]
    if args.only:
        wanted = {x.strip().upper() for x in args.only.split(",") if x.strip()}
        questions = [q for q in questions if q["id"].upper() in wanted]

    modes = ["baseline", "poe2brain"] if args.mode == "both" else [args.mode]
    results_by_mode: dict[str, dict[str, dict[str, Any]]] = {m: {} for m in modes}

    for mode in modes:
        print(
            f"=== Running {len(questions)} questions | mode={mode} | runner={args.runner} ===",
            flush=True,
        )
        workers = args.workers if mode == "baseline" else min(args.workers, 2)

        def _worker(q: dict[str, Any], _mode: str = mode) -> dict[str, Any]:
            if args.runner == "codex-cli":
                return _run_codex_single(q, _mode, args.model, args.timeout)
            if args.runner == "openai":
                if not args.model:
                    raise RuntimeError("--model is required for --runner openai")
                return _run_openai_single(q, _mode, args.model, args.timeout)
            raise ValueError(f"Unsupported runner: {args.runner}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_worker, q): q for q in questions}
            for fut in concurrent.futures.as_completed(futs):
                q = futs[fut]
                rec = fut.result()
                results_by_mode[mode][q["id"]] = rec
                print(
                    f"  [{mode}] {q['id']} rc={rec['returncode']} "
                    f"{rec['elapsed_s']}s len={len(rec['answer'])}",
                    flush=True,
                )

    detected_model = args.model or ""
    for m in modes:
        for rec in results_by_mode[m].values():
            if rec.get("model"):
                detected_model = rec["model"]
                break

    payload: dict[str, Any] = {
        "benchmark_id": bench["benchmark_id"],
        "patch": bench["patch"],
        "run_at_utc": datetime.now(UTC).isoformat(),
        "runner": args.runner,
        "model": detected_model,
        "modes": {},
    }
    for mode in modes:
        preds = {qid: rec["answer"] for qid, rec in results_by_mode[mode].items()}
        scored = score_dataset(bench_path, preds)
        payload["modes"][mode] = {
            "summary": scored,
            "responses": results_by_mode[mode],
        }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nSaved full benchmark results to {out_path}")

    if "baseline" in payload["modes"] and "poe2brain" in payload["modes"]:
        md = render_comparison_markdown(
            payload["modes"]["baseline"]["summary"],
            payload["modes"]["poe2brain"]["summary"],
            detected_model or args.runner,
        )
        print("\n" + md)
    else:
        for mode in modes:
            s = payload["modes"][mode]["summary"]
            hal_cnt = f"{s['hallucinated_questions']}/{s['total_questions']}"
            print(
                f"{mode}: {s['questions_passed']}/{s['total_questions']} ({s['pass_rate_pct']}%) | "
                f"checks {s['checks_passed']}/{s['checks_total']} ({s['check_accuracy_pct']}%) | "
                f"hallucinations {hal_cnt} ({s['hallucination_rate_pct']}%)"
            )
    return 0


def rescore_file(args: argparse.Namespace) -> int:
    bench_path = Path(args.bench)
    data = json.loads(Path(args.results_file).read_text(encoding="utf-8"))
    for _mode, block in data.get("modes", {}).items():
        preds = {qid: rec["answer"] for qid, rec in block["responses"].items()}
        block["summary"] = score_dataset(bench_path, preds)

    if args.write:
        Path(args.results_file).write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    if "baseline" in data.get("modes", {}) and "poe2brain" in data.get("modes", {}):
        print(
            render_comparison_markdown(
                data["modes"]["baseline"]["summary"],
                data["modes"]["poe2brain"]["summary"],
                data.get("model") or data.get("runner", "unknown"),
            )
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="PoE2Bench-0.5.5 A/B Benchmark Runner")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="Run baseline vs poe2brain evaluation")
    p_run.add_argument("--bench", default=str(DEFAULT_BENCH_PATH))
    p_run.add_argument("--runner", choices=["codex-cli", "openai"], default="codex-cli")
    p_run.add_argument("--mode", choices=["baseline", "poe2brain", "both"], default="both")
    p_run.add_argument("--model", default=None)
    p_run.add_argument("--workers", type=int, default=4)
    p_run.add_argument("--timeout", type=int, default=120)
    p_run.add_argument("--only", default=None, help="Comma-separated question IDs (e.g. Q01,Q02)")
    p_run.add_argument("--out", default=str(REPO_ROOT / "benchmarks" / "results" / "latest.json"))

    p_score = sub.add_parser("score", help="Re-score a saved benchmark results JSON")
    p_score.add_argument("results_file")
    p_score.add_argument("--bench", default=str(DEFAULT_BENCH_PATH))
    p_score.add_argument("--write", action="store_true", help="Update summary in-place")

    args = parser.parse_args()
    if args.cmd == "run":
        return run_benchmark(args)
    return rescore_file(args)


if __name__ == "__main__":
    raise SystemExit(main())
