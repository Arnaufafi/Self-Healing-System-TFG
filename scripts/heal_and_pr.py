#!/usr/bin/env python
r"""Heal a real GitHub repository and open a Pull Request (never merges).

Real-environment entry point — the "GitHub PR-on-CI" pivot.  It clones a repo at
a base branch, reproduces the failure, runs the self-healing pipeline in a Docker
sandbox, and — on success — pushes a ``selfheal/<id>`` branch and opens a PR
against the base branch.  **Human review is the merge gate; nothing is merged.**

Three trigger modes:
  * **crash-entry** (default): reproduce a crashing ``python main.py``.
  * **test-entry** (``--test``): reproduce a failing pytest node — the CI signal.
  * **auto** (``--auto``): probe for failing tests first, then a crash; exit 0
    when everything is green.  This is what the on-push workflow uses.

Usage
-----
    GITHUB_TOKEN=ghp_...  OPENAI_API_KEY=...  OPENAI_API_BASE=...  \
    CDD_AGENT_MODE=real  CDD_LLM_MODEL=openai/gpt-4.1-mini  CDD_SWEAGENT_USE_DOCKER=1 \
        python scripts/heal_and_pr.py --repo owner/name --base main

    # auto: detect what is broken (the on-push trigger); exits 0 when green
    python scripts/heal_and_pr.py --repo owner/name --base main --auto

    # test-entry: heal the test CI flagged as failing
    python scripts/heal_and_pr.py --repo owner/name --base main \
        --test tests/test_calc.py::test_divide

    # heal locally and inspect, without pushing or opening a PR:
    python scripts/heal_and_pr.py --repo owner/name --base main --no-pr
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import cast

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Reuse the benchmark's clone / crash-detection / wiring helpers.
from run_benchmark import (  # sibling import; scripts/ is on sys.path
    _git,
    _purge_readonly_tree,
    build_crash_report,
    build_real_deps,
    build_trigger,
    clone_repo,
    detect_crash,
)

from src.config import load_settings
from src.config.logging_config import configure_logging
from src.core.domain import (
    FailingTest,
    HealingState,
    TriggerEvent,
    TriggerType,
)
from src.orchestrator import build_graph


def detect_failing_test(repo_dir: Path, node_id: str) -> tuple[str, str]:
    """Run a failing pytest node and capture its source + failure output.

    The host-side counterpart of :func:`detect_crash` for the test-entry mode:
    it confirms the test really fails (so there is something to heal) and grabs
    the traceback that seeds the initial error signature.  Returns
    ``(test_source, failure_output)``.

    Raises ``FileNotFoundError`` if the node's file is missing and
    ``RuntimeError`` if the test passes (rc=0 — nothing to fix).
    """
    test_file = repo_dir / node_id.split("::", 1)[0]
    if not test_file.exists():
        raise FileNotFoundError(f"Test file not found for node '{node_id}': {test_file}")

    result = subprocess.run(
        [sys.executable, "-m", "pytest", node_id, "-x", "--tb=short", "-q"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode == 0:
        raise RuntimeError(
            f"'{node_id}' passed (rc=0).  Expected a failing test.\n"
            f"stdout: {result.stdout[:500]}"
        )
    output = result.stdout or result.stderr
    if not output.strip():
        output = f"pytest exited with code {result.returncode} but produced no output."
    source = test_file.read_text(encoding="utf-8", errors="replace")
    return source, output


def build_test_trigger(node_id: str, source: str, output: str) -> TriggerEvent:
    """Wrap a failing pytest node in a ``TEST_FAILURE`` trigger."""
    return TriggerEvent(
        trigger_type=TriggerType.TEST_FAILURE,
        failing_test=FailingTest(
            node_id=node_id,
            source=source,
            last_failure_output=output[-4096:],  # keep the last 4 KB
        ),
    )


def shield_runtime_artifacts(repo_dir: Path) -> list[str]:
    """Restore and skip-worktree the tracked files the detection probe dirtied.

    The probe executes the target's own code on the host (pytest / main.py),
    which may rewrite tracked runtime artifacts — e.g. a JSON "database"
    seeded in the branch.  Left alone, the commit's ``git add -A`` would sweep
    those modifications into the heal commit and the PR (defect D16).
    Restoring the seed and marking each file skip-worktree also protects the
    commit from the later executions (Corrector container, validation
    sandbox) re-dirtying the same files.

    Accepted trade-off (same as the benchmark's guard): a legitimate fix that
    must edit one of these runtime files would be hidden from the commit too.
    Returns the shielded paths.
    """
    # ``diff --name-only`` lists tracked files modified or deleted in the
    # working tree, one clean path per line (untracked files never appear —
    # those are the clone excludes' job).  Deliberately NOT ``status
    # --porcelain``: its two-column prefix is positional and ``_git`` strips
    # the output, eating the first line's leading space.
    raw = _git("diff", "--name-only", cwd=repo_dir)
    shielded: list[str] = []
    for path in (line.strip() for line in raw.splitlines()):
        if not path:
            continue
        _git("checkout", "--", path, cwd=repo_dir)
        _git("update-index", "--skip-worktree", path, cwd=repo_dir)
        shielded.append(path)
    if shielded:
        print(f"[i] Shielded {len(shielded)} probe-dirtied runtime artifact(s): {shielded}")
    return shielded


# Pytest's short-summary lines identify the failing item:
#   FAILED tests/test_x.py::test_y - ExcType: msg
#   ERROR tests/test_x.py  (collection failure: no ``::`` part)
_FAILED_NODE_RE = re.compile(r"^(?:FAILED|ERROR)\s+(?P<node>\S+)", re.MULTILINE)


def detect_any_failure(
    repo_dir: Path, base: str = "auto"
) -> tuple[TriggerEvent, tuple[str, ...]] | None:
    """Probe the repo for something to heal: failing tests first, then a crash.

    The detection phase of the on-push trigger.  Returns ``(trigger,
    reproduce_cmd)`` — an empty ``reproduce_cmd`` means "let bootstrap derive
    it" — or ``None`` when everything is green, so the CI run can succeed
    quietly with nothing to do.
    """
    # 1) Failing test — the canonical CI signal.
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--tb=short", "-q"],
        cwd=repo_dir, capture_output=True, text=True, timeout=300,
    )
    if result.returncode not in (0, 5):  # 5 = no tests collected
        node_match = _FAILED_NODE_RE.search(result.stdout or result.stderr)
        if node_match:
            node = node_match.group("node")
            try:
                source, node_output = detect_failing_test(repo_dir, node)
            except RuntimeError:
                pass  # fails in the suite but passes alone (flaky / ordering)
            else:
                return (
                    build_test_trigger(node, source, node_output),
                    ("python", "-m", "pytest", node, "-x", "--tb=short"),
                )
        # Suite failed but no node could be pinned down — try the crash probe.

    # 2) Crashing ``main.py`` — the production signal.
    if (repo_dir / "main.py").exists():
        try:
            crash_output = detect_crash(repo_dir)
        except RuntimeError:
            return None  # main.py exits cleanly
        return build_trigger(build_crash_report(base, crash_output, repo_dir)), ()

    return None


async def heal_and_pr(
    repo: str,
    base: str,
    *,
    open_pr: bool,
    draft: bool,
    test_node: str | None = None,
    auto: bool = False,
) -> int:
    """Clone *repo* at *base*, heal the failure, then push + open the PR."""
    settings = load_settings()
    configure_logging(level=settings.log_level, json_mode=settings.log_json)

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("GITHUB_TOKEN is required (clone + push).", file=sys.stderr)
        return 1

    # 1) Fresh workspace + clone at the base branch.
    workspace_root = _PROJECT_ROOT / ".workspaces" / "deploy"
    repo_dir = workspace_root / "repo"
    if workspace_root.exists():
        _purge_readonly_tree(workspace_root)
    workspace_root.mkdir(parents=True, exist_ok=True)
    clone_repo(token, repo, repo_dir)
    _git("checkout", base, cwd=repo_dir)

    # 2) Branch the heal so the pipeline's commits land off the base branch.
    incident = f"selfheal-{uuid.uuid4().hex[:8]}"
    heal_branch = f"selfheal/{incident}"
    _git("checkout", "-b", heal_branch, cwd=repo_dir)

    # 3) Reproduce the failure (crash or failing test) and build the trigger.
    workspace = str(repo_dir.resolve())
    reproduce_cmd: tuple[str, ...] = ()
    if auto:
        # Auto-entry (the on-push trigger): probe tests first, then main.py.
        # Green across the board is a *success* for CI — exit 0, no PR.
        detected = detect_any_failure(repo_dir, base)
        if detected is None:
            # ASCII on purpose: Windows consoles default to cp1252, where
            # emoji output raises UnicodeEncodeError.
            print("[OK] Nothing to heal: tests pass and main.py exits cleanly.")
            return 0
        trigger, reproduce_cmd = detected
    elif test_node:
        # Test-entry: heal a failing CI test.  The failing test *is* the
        # regression test, so the pipeline skips immunization and reproduces
        # this specific node (not the generic suite) in the sandbox.
        source, output = detect_failing_test(repo_dir, test_node)
        trigger = build_test_trigger(test_node, source, output)
        reproduce_cmd = ("python", "-m", "pytest", test_node, "-x", "--tb=short")
    else:
        # Crash-entry: heal a crashing ``python main.py``.
        crash_output = detect_crash(repo_dir)  # raises if main.py exits cleanly
        trigger = build_trigger(build_crash_report(base, crash_output, repo_dir))

    # The probe just executed the target's code; keep whatever tracked
    # runtime artifacts it rewrote out of the heal commit (D16).
    shield_runtime_artifacts(repo_dir)

    state = HealingState(
        trigger=trigger, workspace_path=workspace, attempt_count=0,
        failed_attempts=[], logs=[], is_resolved=False,
    )
    if reproduce_cmd:
        state["reproduce_cmd"] = reproduce_cmd
    deps = build_real_deps(settings, workspace)
    graph = build_graph(deps)

    from src.observability.llm import using_llm_sink

    with using_llm_sink(deps.telemetry):
        final = cast(
            HealingState,
            await graph.ainvoke(state, config={"configurable": {"thread_id": incident}}),
        )
        await asyncio.sleep(0.2)  # flush litellm's detached token/cost callback

    if not final.get("is_resolved"):
        print(f"[FAIL] Could not heal {repo}@{base}. Post-mortem under {settings.reports_dir}.")
        return 2

    # 4) Gather artefacts for the PR.
    try:
        raw = _git("log", f"{base}..{heal_branch}", "--format=%s", cwd=repo_dir)
        subjects = [s for s in raw.splitlines() if s.strip()]
    except RuntimeError:
        subjects = []
    aggregate = getattr(deps.telemetry, "aggregate", lambda: None)()

    if not open_pr:
        print(
            f"[OK] Healed locally on '{heal_branch}' "
            f"({len(subjects)} commit(s)). PR skipped (--no-pr)."
        )
        return 0

    # 5) Push + open the PR (no merge).
    from src.infrastructure.github import GitHubAdapter, build_pr_body

    gh = GitHubAdapter(token)
    await gh.push_branch(workspace_path=workspace, repo=repo, branch=heal_branch)
    title = subjects[0] if subjects else f"fix: auto-heal {base}"
    body = build_pr_body(
        reproduce_cmd=tuple(final.get("reproduce_cmd") or ("python", "main.py")),
        resolved=final.get("resolved_errors", []),
        telemetry=aggregate,
        commit_subjects=subjects,
    )
    pr_url = await gh.open_pull_request(
        repo=repo, base=base, head=heal_branch, title=title, body=body, draft=draft
    )
    print(f"[OK] Opened PR: {pr_url}")
    return 0


def main() -> int:
    """Parse the CLI and run the healing entry point."""
    ap = argparse.ArgumentParser(description="Heal a GitHub repo and open a PR (never merges).")
    ap.add_argument("--repo", required=True, help="owner/name of the GitHub repository")
    ap.add_argument("--base", required=True, help="base branch to heal and open the PR against")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--auto", action="store_true",
        help="auto-detect what to heal: failing tests first, then a crashing "
             "main.py; exits 0 when everything is green (the on-push trigger)",
    )
    mode.add_argument(
        "--test", metavar="NODEID", default=None,
        help="test-entry: heal a failing pytest node (e.g. tests/test_x.py::test_y) "
             "instead of a crashing main.py",
    )
    ap.add_argument("--no-pr", action="store_true", help="heal locally; do not push or open a PR")
    ap.add_argument("--draft", action="store_true", help="open the PR as a draft")
    args = ap.parse_args()
    return asyncio.run(
        heal_and_pr(
            args.repo, args.base,
            open_pr=not args.no_pr, draft=args.draft,
            test_node=args.test, auto=args.auto,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
