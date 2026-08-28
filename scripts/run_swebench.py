#!/usr/bin/env python
r"""Heal a slice of SWE-bench (test-entry) and emit predictions for the scorer.

SWE-bench is pure test-entry: each instance ships ``FAIL_TO_PASS`` tests that
fail at ``base_commit`` and pass once the bug is fixed.  This runner maps an
instance onto the system's ``TEST_FAILURE`` trigger, runs the same graph the
benchmark uses, and writes a ``predictions.jsonl`` for the official evaluator.

It exercises **Corrector + validate + Reporter** but NOT the Tester (immunize
is skipped on test-entry) — present it as a Corrector scaling test, not a
whole-system test.

Per instance: clone at ``base_commit`` → apply ``test_patch`` (commit T) →
reproduce the failing tests in the instance's Docker image → run the pipeline
(mounting the host clone at ``/testbed``) → capture the source diff (T..HEAD,
minus the test files) as the ``model_patch``.

Usage
-----
    pip install datasets
    set CDD_AGENT_MODE=real  CDD_LLM_MODEL=openai/gpt-4.1-mini
    set CDD_SANDBOX_WORKDIR=/testbed  CDD_SWEAGENT_USE_DOCKER=true
    python scripts/swebench_select.py --limit 8 --out slice.json
    python scripts/run_swebench.py --instances-file slice.json --sanity      # no LLM
    python scripts/run_swebench.py --instances-file slice.json  # preds -> reports/swebench/

Score with the official harness (separate, network + GBs of images). Run it from
inside reports/ so its logs/ and <model>.<run_id>.json land there (reports/ is
gitignored) instead of littering the repo root:
    cd reports/swebench/official && python -m swebench.harness.run_evaluation \\
        --dataset_name princeton-nlp/SWE-bench_Lite \\
        --predictions_path ../predictions.jsonl --run_id selfheal_slice --max_workers 4
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Reuse the benchmark's git + wiring helpers (scripts/ is on sys.path).
from run_benchmark import (
    _git,
    _purge_readonly_tree,
    build_real_deps,
)
from swebench_select import parse_fail_to_pass

from src.config import configure_logging, load_settings
from src.core.domain import (
    FailingTest,
    HealingState,
    SandboxVerdict,
    TriggerEvent,
    TriggerType,
)
from src.orchestrator import build_graph

_DEFAULT_DATASET = "princeton-nlp/SWE-bench_Lite"
_IMAGE_TEMPLATE = "sweb.eval.x86_64.{instance_id}:latest"
_DIFF_GIT_RE = re.compile(r"^diff --git a/(?P<a>\S+) b/(?P<b>\S+)", re.MULTILINE)


# ===================================================================
# Pure helpers (unit-tested without Docker or the dataset)
# ===================================================================

def parse_test_paths(test_patch: str) -> list[str]:
    """Workspace-relative paths the ``test_patch`` touches (its ``b/`` sides)."""
    seen: dict[str, None] = {}
    for m in _DIFF_GIT_RE.finditer(test_patch):
        seen.setdefault(m.group("b"), None)
    return list(seen)


def model_patch_diff_args(base_ref: str, test_paths: list[str]) -> list[str]:
    """Argv (after ``git``) for the source-only diff: base..HEAD, tests excluded."""
    args = ["diff", base_ref, "HEAD", "--", "."]
    args.extend(f":(exclude){p}" for p in test_paths)
    return args


def write_prediction(path: Path, instance_id: str, model_patch: str) -> None:
    """Append one SWE-bench prediction line."""
    line = json.dumps({
        "instance_id": instance_id,
        "model_name_or_path": "selfheal",
        "model_patch": model_patch,
    })
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def image_for(instance_id: str, template: str) -> str:
    """Container image tag for an instance (verify the real name with `docker images`)."""
    return template.format(instance_id=instance_id)


# ===================================================================
# Instance preparation (host git)
# ===================================================================

def _git_apply(repo_dir: Path, patch_text: str) -> None:
    """Apply a unified diff to the working tree via ``git apply`` (stdin)."""
    proc = subprocess.run(
        ["git", "apply", "--whitespace=nowarn"],
        cwd=repo_dir, input=patch_text, text=True, capture_output=True, timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git apply failed (rc={proc.returncode}): {proc.stderr[:500]}")


def prepare_instance(repo_dir: Path, instance: dict[str, Any]) -> list[str]:
    """Clone at ``base_commit``, apply the test patch, commit it (T).

    Returns the test-file paths (for excluding them from the model patch).
    """
    repo, base = instance["repo"], instance["base_commit"]
    url = f"https://github.com/{repo}.git"
    repo_dir.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=repo_dir)
    _git("config", "user.email", "selfheal@local", cwd=repo_dir)
    _git("config", "user.name", "selfheal", cwd=repo_dir)
    try:
        _git("remote", "add", "origin", url, cwd=repo_dir)
    except RuntimeError:
        _git("remote", "set-url", "origin", url, cwd=repo_dir)
    # GitHub allows fetching an arbitrary SHA shallowly; fall back to a full fetch.
    try:
        _git("fetch", "-q", "--depth", "1", "origin", base, cwd=repo_dir)
    except RuntimeError:
        _git("fetch", "-q", "origin", cwd=repo_dir)
    _git("checkout", "-q", base, cwd=repo_dir)
    _git_apply(repo_dir, instance["test_patch"])
    _git("add", "-A", cwd=repo_dir)
    _git("commit", "-q", "-m", "swebench: apply test patch", cwd=repo_dir)
    return parse_test_paths(instance["test_patch"])


def read_test_source(repo_dir: Path, node_id: str) -> str:
    """Read the source of the file behind a pytest node id (best-effort)."""
    rel = node_id.split("::", 1)[0]
    try:
        return (repo_dir / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def extract_model_patch(repo_dir: Path, base_commit: str, test_paths: list[str]) -> str:
    """Source-only diff base_commit..HEAD (the test patch and test files excluded)."""
    return _git(*model_patch_diff_args(base_commit, test_paths), cwd=repo_dir)


# ===================================================================
# Sandbox helpers
# ===================================================================

def _make_sandbox(settings: Any) -> Any:
    """Build a bare DockerSandbox (sanity mode needs no agents)."""
    from src.infrastructure.docker_sandbox import DockerSandbox

    return DockerSandbox(settings)


async def _run_pytest(
    sandbox: Any, workspace: str, image: str, nodes: list[str], py: str = "python"
) -> Any:
    """Run the given pytest nodes in *image* with interpreter *py*; return the SandboxResult."""
    cmd = (py, "-m", "pytest", *nodes, "-x", "--tb=short")
    return await sandbox.run_tests(workspace_path=workspace, image=image, command=cmd)


# ===================================================================
# Per-instance: sanity and heal
# ===================================================================

async def sanity_one(instance: dict[str, Any], settings: Any, image: str) -> dict[str, Any]:
    """Validate image + mount with no LLM: FAIL_TO_PASS must fail, then pass on the gold patch."""
    repo_dir = _PROJECT_ROOT / ".workspaces" / "swebench" / "repo"
    _wipe(repo_dir.parent)
    test_paths = prepare_instance(repo_dir, instance)
    workspace = str(repo_dir.resolve())
    nodes = parse_fail_to_pass(instance["FAIL_TO_PASS"])
    sandbox = _make_sandbox(settings)

    before = await _run_pytest(sandbox, workspace, image, nodes, settings.sandbox_python)
    if before.verdict not in (SandboxVerdict.PASSED, SandboxVerdict.FAILED):
        # INFRASTRUCTURE_ERROR / TIMEOUT — the environment is unreachable, most
        # often because the instance image is not present locally.
        return {
            "instance_id": instance["instance_id"], "ok": False,
            "env_error": f"{before.verdict.value}: {before.logs_tail[:200]}",
            "test_paths": test_paths,
        }
    fails_before = before.verdict is SandboxVerdict.FAILED
    _git_apply(repo_dir, instance["patch"])  # the golden fix
    after = await _run_pytest(sandbox, workspace, image, nodes, settings.sandbox_python)
    passes_after = after.verdict is SandboxVerdict.PASSED

    ok = fails_before and passes_after
    row = {
        "instance_id": instance["instance_id"], "ok": ok,
        "fails_before": fails_before, "passes_after": passes_after,
        "before": before.verdict.value, "after": after.verdict.value,
        "test_paths": test_paths,
    }
    if not ok:
        # Surface the gold-patch run's output so we can see WHY it did not pass:
        # a network error (sandbox runs network-disabled), an unapplied fix, a
        # collection error, etc. Trimmed to keep the summary readable.
        row["after_log"] = after.logs_tail[-1500:]
    return row


async def heal_one(
    instance: dict[str, Any], settings: Any, image: str, predictions_path: Path
) -> dict[str, Any]:
    """Heal one instance and append its prediction. Returns a result row."""
    incident = instance["instance_id"]
    repo_dir = _PROJECT_ROOT / ".workspaces" / "swebench" / "repo"
    _wipe(repo_dir.parent)
    test_paths = prepare_instance(repo_dir, instance)
    workspace = str(repo_dir.resolve())
    nodes = parse_fail_to_pass(instance["FAIL_TO_PASS"])
    protected = parse_fail_to_pass(instance.get("PASS_TO_PASS"))

    deps = build_real_deps(settings, workspace, image_override=image)

    # Reproduce the failing tests IN THE IMAGE (the host lacks the repo's deps).
    failure = await _run_pytest(deps.sandbox, workspace, image, nodes, settings.sandbox_python)
    if failure.verdict is SandboxVerdict.PASSED:
        return {"instance_id": incident, "resolved": False, "skipped": "tests already pass",
                "cost_usd": 0.0}
    if failure.verdict is not SandboxVerdict.FAILED:
        # INFRASTRUCTURE_ERROR / TIMEOUT: the environment is unreachable (most
        # often the instance image is absent). Skip — don't spend an LLM call.
        return {"instance_id": incident, "resolved": False, "cost_usd": 0.0,
                "skipped": f"cannot reproduce ({failure.verdict.value}); "
                           f"image present? {failure.logs_tail[:160]}"}

    trigger = TriggerEvent(
        trigger_type=TriggerType.TEST_FAILURE,
        failing_test=FailingTest(
            node_id=nodes[0],
            source=read_test_source(repo_dir, nodes[0]),
            last_failure_output=failure.logs_tail[-4096:],
        ),
    )
    state = HealingState(
        trigger=trigger, workspace_path=workspace, sandbox_image=image,
        attempt_count=0, failed_attempts=[], logs=[], is_resolved=False,
    )
    state["reproduce_cmd"] = (settings.sandbox_python, "-m", "pytest", *nodes, "-x", "--tb=short")
    if protected:
        # No-regression gate: PASS_TO_PASS were green at base and must stay green
        # after the fix, or validate_node rolls the attempt back as a regression.
        state["regression_cmd"] = (
            settings.sandbox_python, "-m", "pytest", *protected, "-x", "--tb=short"
        )

    from src.observability.llm import using_llm_sink

    graph = build_graph(deps)
    with using_llm_sink(deps.telemetry):
        final = cast(
            HealingState,
            await graph.ainvoke(
                state,
                config={
                    "configurable": {"thread_id": incident},
                    # LangGraph's default (25) is below what the budgets allow:
                    # cycles * (3 * retries + 4) + 2 = 67 with the defaults. A
                    # 4-chained-error instance hit it mid-progress and aborted.
                    "recursion_limit": 150,
                },
            ),
        )
        await asyncio.sleep(0.2)  # flush litellm's detached token/cost callback

    resolved = bool(final.get("is_resolved"))
    model_patch = (
        extract_model_patch(repo_dir, instance["base_commit"], test_paths) if resolved else ""
    )
    if resolved and model_patch.strip():
        write_prediction(predictions_path, incident, model_patch)

    # Surface the full telemetry the pipeline already collected (the benchmark
    # runner keeps all of this; SWE-bench used to drop everything but the cost):
    # retries, chained-error count, LLM calls + in/out/cached tokens + cost, and
    # the per-agent breakdown (corrector / tester / reporter).
    aggregate = getattr(deps.telemetry, "aggregate", lambda: {})()
    llm = aggregate.get("llm", {})
    total = llm.get("total", {})
    return {
        "instance_id": incident,
        "resolved": resolved,
        "attempts": final.get("attempt_count", 0),
        "chained_errors": final.get("error_cycle_index", 0),
        "patch_bytes": len(model_patch),
        "llm_calls": int(total.get("calls", 0)),
        "prompt_tokens": int(total.get("prompt_tokens", 0)),
        "cached_tokens": int(total.get("cached_tokens", 0)),
        "completion_tokens": int(total.get("completion_tokens", 0)),
        "cost_usd": round(float(total.get("cost_usd", 0.0)), 4),
        # Run trace: the ordered path through the graph (for plotting) plus the
        # per-node timings, alongside the per-agent LLM breakdown.
        "node_path": aggregate.get("node_path", []),
        "by_agent": llm.get("by_agent", {}),
        "by_node": {
            name.split(".", 1)[1]: stats
            for name, stats in (aggregate.get("by_name") or {}).items()
            if isinstance(name, str) and name.startswith("node.")
        },
    }


# ===================================================================
# Orchestration
# ===================================================================

def _wipe(path: Path) -> None:
    if path.exists():
        _purge_readonly_tree(path)
    path.mkdir(parents=True, exist_ok=True)


def load_instances(dataset: str, split: str, instance_ids: list[str]) -> list[dict[str, Any]]:
    """Load full instance dicts for *instance_ids* (lazy ``datasets`` import)."""
    try:
        from datasets import load_dataset
    except ImportError as exc:  # pragma: no cover - environment guard
        raise SystemExit("The 'datasets' package is required: pip install datasets") from exc
    wanted = set(instance_ids)
    by_id = {r["instance_id"]: dict(r) for r in load_dataset(dataset, split=split)
             if r["instance_id"] in wanted}
    missing = wanted - by_id.keys()
    if missing:
        print(f"[warn] not found in {dataset}: {sorted(missing)}", file=sys.stderr)
    return [by_id[i] for i in instance_ids if i in by_id]


def _resolve_ids(args: argparse.Namespace) -> list[str]:
    if args.instances_file:
        payload = json.loads(Path(args.instances_file).read_text(encoding="utf-8"))
        return list(payload.get("instance_ids", payload if isinstance(payload, list) else []))
    if args.instance_ids:
        return [s.strip() for s in args.instance_ids.split(",") if s.strip()]
    raise SystemExit("Provide --instances-file or --instance-ids.")


async def _amain(args: argparse.Namespace) -> int:
    from src.agents import MiniSWEFixer

    settings = load_settings()
    configure_logging(level=settings.log_level, json_mode=settings.log_json)
    if settings.sandbox_workdir == "/workspace":
        print("[warn] CDD_SANDBOX_WORKDIR is /workspace; SWE-bench images expect /testbed.",
              file=sys.stderr)

    ids = _resolve_ids(args)
    if args.limit:
        ids = ids[: args.limit]
    instances = load_instances(args.dataset, args.split, ids)
    predictions_path = Path(args.predictions_out)
    # Sequential CLI: blocking Path calls in this async fn are harmless.
    predictions_path.parent.mkdir(parents=True, exist_ok=True)
    if not args.sanity and predictions_path.exists():  # noqa: ASYNC240
        predictions_path.unlink()  # noqa: ASYNC240

    results: list[dict[str, Any]] = []
    for i, inst in enumerate(instances, 1):
        image = image_for(inst["instance_id"], args.image_template)
        print(f"\n[{i}/{len(instances)}] {inst['instance_id']}  ({inst['repo']})  image={image}")
        t0 = time.monotonic()
        try:
            if args.sanity:
                row = await sanity_one(inst, settings, image)
            else:
                row = await heal_one(inst, settings, image, predictions_path)
        except Exception as exc:
            row = {"instance_id": inst["instance_id"], "error": f"{type(exc).__name__}: {exc}"}
        row["duration_s"] = round(time.monotonic() - t0, 1)
        results.append(row)
        print("   ", json.dumps(row, ensure_ascii=False))
        MiniSWEFixer.cleanup_all_containers()

    _write_summary(results, sanity=args.sanity)
    return 0


def _write_summary(results: list[dict[str, Any]], *, sanity: bool) -> None:
    out_dir = _PROJECT_ROOT / "reports" / "swebench"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    (out_dir / f"swebench_{'sanity_' if sanity else ''}{ts}.json").write_text(
        json.dumps(results, indent=2, default=str), encoding="utf-8"
    )
    if sanity:
        ok = sum(1 for r in results if r.get("ok"))
        print(f"\n=== sanity: {ok}/{len(results)} instances fail→pass on the gold patch ===")
    else:
        solved = sum(1 for r in results if r.get("resolved"))
        cost = sum(r.get("cost_usd", 0.0) for r in results)
        calls = sum(r.get("llm_calls", 0) for r in results)
        prompt = sum(r.get("prompt_tokens", 0) for r in results)
        cached = sum(r.get("cached_tokens", 0) for r in results)
        completion = sum(r.get("completion_tokens", 0) for r in results)
        print(
            f"\n=== healed (our validate): {solved}/{len(results)} · ${cost:.4f} · "
            f"{calls} calls · {prompt} prompt ({cached} cached) + {completion} out ==="
        )
        print("Score officially (cd into reports/ first so logs/ + report stay gitignored):")
        print("  cd reports/swebench/official && python -m swebench.harness.run_evaluation "
              "--predictions_path ../predictions.jsonl --run_id selfheal_slice --max_workers 4")


def main() -> int:
    """Parse args and run the slice (heal or sanity)."""
    ap = argparse.ArgumentParser(description="Heal a SWE-bench slice (test-entry).")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--instances-file", help="JSON from swebench_select.py (instance_ids)")
    src.add_argument("--instance-ids", help="comma-separated instance ids")
    ap.add_argument("--dataset", default=_DEFAULT_DATASET)
    ap.add_argument("--split", default="test")
    ap.add_argument("--image-template", default=_IMAGE_TEMPLATE,
                    help="container image per instance; verify with `docker images | grep sweb`")
    ap.add_argument(
        "--predictions-out",
        default=str(_PROJECT_ROOT / "reports" / "swebench" / "predictions.jsonl"),
        help="predictions.jsonl path (default under reports/, which is gitignored)",
    )
    ap.add_argument("--sanity", action="store_true",
                    help="no-LLM env check: FAIL_TO_PASS must fail, then pass on the gold patch")
    ap.add_argument("--limit", type=int, default=0, help="cap the number of instances")
    return asyncio.run(_amain(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
