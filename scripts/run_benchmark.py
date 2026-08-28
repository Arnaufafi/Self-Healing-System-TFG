"""Benchmark runner for the Self-Healing CDD pipeline.

Clones every branch of a private GitHub repository (each containing a
crashing ``main.py``), runs the full multi-agent pipeline on each
branch, and optionally opens a Pull Request with the fix.

Usage
-----
::

    set GITHUB_TOKEN=ghp_...
    set CDD_LLM_MODEL=ollama/qwen2.5-coder:14b
    set OLLAMA_API_BASE=http://localhost:11434
    set MSWEA_COST_TRACKING=ignore_errors
    python scripts/run_benchmark.py

Environment variables
~~~~~~~~~~~~~~~~~~~~~
``GITHUB_TOKEN``
    Personal access token with ``repo`` scope (required).
``BENCHMARK_REPO``
    GitHub slug, defaults to ``Arnaufafi/Benchmark_for_agents``.
``CDD_LLM_MODEL``
    Model identifier in litellm format (e.g. ``ollama/qwen2.5-coder:14b``).
``OLLAMA_API_BASE``
    Base URL for local Ollama (e.g. ``http://localhost:11434``).
``MSWEA_COST_TRACKING``
    Set to ``ignore_errors`` when using local/free models.
``BENCHMARK_USE_DOCKER``
    When ``1`` / ``true``, mini-swe-agent runs inside Docker.
``BENCHMARK_DOCKER_IMAGE``
    Docker image for mini-swe-agent (default ``python:3.12-slim``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

# ---------------------------------------------------------------------------
# Ensure the project root is importable
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.agents import MiniSWEFixer
from src.config import Settings, configure_logging, load_settings
from src.core.domain import (
    CrashReport,
    HealingState,
    TriggerEvent,
    TriggerType,
)
from src.infrastructure.persistence import FilesystemReporter
from src.orchestrator import Dependencies, build_graph

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_REPO = "Arnaufafi/Benchmark_for_agents"
SKIP_BRANCHES = {"main", "master", "HEAD", "origin"}

# The benchmark only measures *crash-to-pass* scenarios: branches whose seeded
# bug makes ``python main.py`` exit non-zero (crash-entry).  Those live under
# these prefixes (``bugA/*`` = import/parse-time, ``bugB/*`` = runtime).  Every
# other branch on the remote is intentionally excluded:
#   - ``bugC/*``      documented non-reproducing scenarios (main.py exits clean)
#   - ``ci/*``        test-entry deployment demos (healed via heal_and_pr, not here)
#   - ``selfheal/*``  the system's own PR output branches (already-fixed code)
SCENARIO_PREFIXES: Final[tuple[str, ...]] = ("bugA/", "bugB/")


# ===================================================================
# 1.  Clone / branch helpers
# ===================================================================

def _git(*args: str, cwd: str | Path | None = None) -> str:
    """Run a git command and return stripped stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (rc={result.returncode}):\n{result.stderr}"
        )
    return result.stdout.strip()


# The app writes this "database" when it runs. Some bug branches TRACK it in
# their seed, so the untracked-only ``.git/info/exclude`` cannot keep it out of
# heal commits — we mark it skip-worktree per branch instead (see below).
_RUNTIME_DB: Final[str] = "datos_bancarios.json"


def _git_skip_worktree(repo_dir: Path, rel_path: str, *, skip: bool) -> None:
    """Best-effort toggle of the skip-worktree bit on a *tracked* file.

    With the bit set, git ignores the app's writes to *rel_path* — ``git add``
    will not stage them, so the runtime DB stays out of the Reporter's commit.
    Silently a no-op when the file is not tracked on the current branch.
    """
    flag = "--skip-worktree" if skip else "--no-skip-worktree"
    try:
        _git("update-index", flag, rel_path, cwd=repo_dir)
    except RuntimeError:
        pass  # not tracked on this branch — nothing to mark


def _purge_readonly_tree(path: Path) -> None:
    """Delete *path* recursively, clearing read-only attributes as needed.

    Git stores objects under ``.git/objects/pack/`` with the read-only
    bit set on Windows. ``shutil.rmtree`` then fails with
    ``PermissionError`` and, when called with ``ignore_errors=True``,
    swallows the failure and leaves a half-deleted tree — which makes
    the subsequent ``git clone`` complain that the destination is not
    empty. This helper resets the write bit and retries via the
    ``onerror`` hook so the tree is genuinely gone afterwards.
    """
    def _clear_readonly_and_retry(func, target, exc_info):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except FileNotFoundError:
            pass
        except OSError:
            # Log-and-continue: a single locked file should not abort
            # the wipe of the whole tree.
            _LOGGER.warning(
                "workspace.purge.skipped",
                extra={"target": str(target), "error": str(exc_info[1])},
            )

    shutil.rmtree(str(path), onerror=_clear_readonly_and_retry)


# Runtime artifacts the seeded app produces when it runs (e.g. the JSON "DB"
# main.py writes, bytecode caches). Excluding them in the clone keeps the
# Reporter's heal commits to just the fix + regression test — without them
# ``git add`` sweeps the generated DB into every commit as noise.
_CLONE_EXCLUDES: Final[tuple[str, ...]] = (
    "datos_bancarios.json",
    "__pycache__/",
    "*.pyc",
)


def clone_repo(token: str, repo: str, dest: Path) -> None:
    """Shallow-clone the benchmark repo into *dest*."""
    url = f"https://x-access-token:{token}@github.com/{repo}.git"
    _git("clone", "--no-single-branch", "--depth=1", url, str(dest))
    # Keep heal commits clean: ignore runtime artifacts locally (does not touch
    # the remote repo). ``.git/info/exclude`` is the per-clone, un-tracked gitignore.
    try:
        exclude = dest / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a", encoding="utf-8") as fh:
            fh.write("\n# self-healing benchmark: runtime artifacts\n")
            fh.write("\n".join(_CLONE_EXCLUDES) + "\n")
    except OSError as exc:
        _LOGGER.warning("clone.exclude_failed", extra={"error": str(exc)})
    _LOGGER.info("clone.done", extra={"dest": str(dest)})


def list_remote_branches(repo_dir: Path) -> list[str]:
    """Return remote branch names (without ``origin/``)."""
    raw = _git("branch", "-r", "--format=%(refname:short)", cwd=repo_dir)
    branches: list[str] = []
    for line in raw.splitlines():
        name = line.strip()
        # Remote branches are like "origin/branch-name"
        if "/" in name:
            name = name.split("/", 1)[1]
        if name in SKIP_BRANCHES:
            continue
        # Crash-to-pass only: skip deployment demos, bot output and
        # documented non-reproducing scenarios (see SCENARIO_PREFIXES).
        if not name.startswith(SCENARIO_PREFIXES):
            continue
        branches.append(name)
    return sorted(set(branches))


def checkout_branch(repo_dir: Path, branch: str) -> None:
    """Switch the working tree to *branch* with a pristine state.

    Steps executed **before** the checkout:

    1. ``git reset --hard``  — revert all tracked-file modifications
       (e.g. source files the Corrector edited on the previous branch).
    2. ``git clean -fdx``  — remove **all** untracked files including
       ignored ones (runtime artifacts, session test files, ``__pycache__``).

    This guarantees every branch starts from a clean slate.
    """
    # 0) Clear any skip-worktree mark left by the previous branch so the next
    #    reset/checkout can restore the tracked runtime DB to its pristine state
    #    (skip-worktree files are otherwise left untouched by reset --hard).
    _git_skip_worktree(repo_dir, _RUNTIME_DB, skip=False)
    # 1) Hard-reset the index + working tree to HEAD.  ``git checkout .`` is not
    #    enough: the pipeline stages files (``git add -N`` in the fixer's diff
    #    capture, ``git add -A`` in the commit) and the app under test writes
    #    runtime artefacts (e.g. ``datos_bancarios.json``).  Any of those left
    #    staged/modified makes the next ``git checkout <branch>`` abort with
    #    "local changes would be overwritten".  ``reset --hard`` clears them all.
    _git("reset", "--hard", cwd=repo_dir)
    # 2) Remove ALL untracked files (including gitignored ones like runtime data)
    _git("clean", "-fdx", cwd=repo_dir)
    # 3) Now switch branch safely
    _git("checkout", branch, cwd=repo_dir)
    # 4) On branches that TRACK the runtime DB, mark it skip-worktree so the
    #    app's writes to it are not swept into the heal commit by ``git add -A``
    #    (the untracked case is already handled by .git/info/exclude at clone).
    _git_skip_worktree(repo_dir, _RUNTIME_DB, skip=True)
    _LOGGER.info("checkout.done", extra={"branch": branch})


# ===================================================================
# 2.  Crash detection
# ===================================================================

def detect_crash(workspace: Path) -> str:
    """Run ``python main.py`` and capture the crash traceback.

    Returns the raw stderr/traceback text.  Raises ``RuntimeError`` if
    the process exits cleanly (nothing to fix).
    """
    main_py = workspace / "main.py"
    if not main_py.exists():
        raise FileNotFoundError(f"No main.py found in {workspace}")

    result = subprocess.run(
        [sys.executable, "main.py"],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode == 0:
        raise RuntimeError(
            f"main.py exited cleanly (rc=0).  Expected a crash.\n"
            f"stdout: {result.stdout[:500]}"
        )

    # Prefer stderr (where tracebacks go), fall back to stdout
    crash_output = result.stderr or result.stdout
    if not crash_output.strip():
        crash_output = f"Process exited with code {result.returncode} but no output."

    _LOGGER.info(
        "crash.detected",
        extra={"returncode": result.returncode, "output_len": len(crash_output)},
    )
    return crash_output


# ===================================================================
# 3.  CrashReport / TriggerEvent construction
# ===================================================================

def build_crash_report(branch: str, crash_output: str, workspace: Path) -> CrashReport:
    """Build a :class:`CrashReport` from the captured crash."""
    commit_sha = "0000000"
    try:
        commit_sha = _git("rev-parse", "--short", "HEAD", cwd=workspace)
    except RuntimeError:
        pass

    # Sanitise branch name: replace / and other unsafe chars so the
    # incident_id is safe for use as a filename component.
    safe_branch = branch.replace("/", "_").replace("\\", "_")
    return CrashReport(
        incident_id=f"bench-{safe_branch}-{uuid.uuid4().hex[:6]}",
        service_name=f"benchmark/{branch}",
        stack_trace=crash_output[-4096:],  # Keep last 4 KB
        commit_sha=commit_sha,
    )


def build_trigger(crash: CrashReport) -> TriggerEvent:
    """Wrap in a PRODUCTION_CRASH trigger."""
    return TriggerEvent(trigger_type=TriggerType.PRODUCTION_CRASH, crash_report=crash)


# ===================================================================
# 4.  Dependency wiring (mirrors main.py "real" mode)
# ===================================================================

def build_real_deps(
    settings: Settings, workspace: str, *, image_override: str | None = None
) -> Dependencies:
    """Wire real agents and infrastructure — same as ``main.py --real``.

    *image_override*, when given, is the container image for the Corrector
    (the validation sandbox takes its image from the graph state). SWE-bench
    passes the per-instance image here; the benchmark leaves it ``None``.
    """
    from src.agents import LLMReporter, LLMTester, MiniSWEFixer
    from src.infrastructure.docker_sandbox import DockerSandbox
    from src.infrastructure.git_ops import GitAdapter

    # ``BENCHMARK_*`` overrides exist for backwards compatibility; when
    # unset we fall back to the canonical ``CDD_SWEAGENT_*`` settings so
    # the benchmark and ``main.py`` stay aligned.
    use_docker_env = os.getenv("BENCHMARK_USE_DOCKER")
    use_docker = (
        use_docker_env.lower() in {"1", "true", "yes"}
        if use_docker_env is not None
        else settings.sweagent_use_docker
    )
    docker_image = image_override or os.getenv(
        "BENCHMARK_DOCKER_IMAGE", settings.sweagent_docker_image
    )

    fixer = MiniSWEFixer(
        model_name=settings.llm_model,
        workspace_path=workspace,
        cost_limit=settings.sweagent_cost_limit,
        timeout_seconds=settings.sweagent_timeout_seconds,
        step_limit=settings.sweagent_step_limit,
        trajectory_dir=settings.sweagent_trajectory_dir or None,
        use_docker=use_docker,
        docker_image=docker_image,
        container_workdir=settings.sandbox_workdir,
        python_executable=settings.sandbox_python,
    )
    tester = LLMTester(
        model_name=settings.llm_model,
        workspace_path=workspace,
        timeout_seconds=settings.tester_timeout_seconds,
    )
    reporter_agent = LLMReporter(model_name=settings.llm_model)
    sandbox = DockerSandbox(settings)
    git = GitAdapter()
    reporter = FilesystemReporter(settings)

    deps = Dependencies(
        settings=settings,
        fixer=fixer,
        tester=tester,
        reporter_agent=reporter_agent,
        sandbox=sandbox,
        git=git,
        reporter=reporter,
    )
    # Wrap every port with telemetry decorators (build_graph adds the node-level
    # spans too) so each run yields per-agent / per-node timing metrics.
    from src.observability import InMemoryTelemetry
    from src.observability.wiring import instrument_dependencies

    return instrument_dependencies(deps, InMemoryTelemetry())


# ===================================================================
# 5.  Push + PR helpers
# ===================================================================

def push_and_pr(
    workspace: Path,
    branch: str,
    token: str,
    repo: str,
) -> str | None:
    """Push the fix branch and open a PR.  Returns the PR URL or None."""
    fix_branch = f"fix/{branch}"
    try:
        _git("checkout", "-b", fix_branch, cwd=workspace)
    except RuntimeError:
        # Already on a branch — force-create
        _git("checkout", "-B", fix_branch, cwd=workspace)

    # Stage and commit any changes the pipeline made
    _git("add", "-A", cwd=workspace)
    try:
        _git(
            "commit", "-m",
            f"fix({branch}): auto-heal via Self-Healing CDD pipeline\n\n"
            f"Co-Authored-By: Self-Healing-Bot <self-healing-bot@urv.cat>",
            cwd=workspace,
        )
    except RuntimeError as exc:
        _LOGGER.warning("push.no_changes", extra={"error": str(exc)})
        return None

    # Push
    push_url = f"https://x-access-token:{token}@github.com/{repo}.git"
    _git("push", push_url, fix_branch, "--force", cwd=workspace)
    _LOGGER.info("push.done", extra={"branch": fix_branch})

    # Open PR via gh CLI
    try:
        pr_result = subprocess.run(
            [
                "gh", "pr", "create",
                "--repo", repo,
                "--base", branch,
                "--head", fix_branch,
                "--title", f"fix({branch}): auto-heal crashing code",
                "--body",
                f"## Auto-generated fix\n\n"
                f"Branch `{branch}` had a crashing `main.py`.\n"
                f"This PR was generated by the **Self-Healing CDD pipeline**.\n\n"
                f"🤖 Generated by Self-Healing CDD benchmark runner",
            ],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "GH_TOKEN": token},
        )
        if pr_result.returncode == 0:
            pr_url = pr_result.stdout.strip()
            _LOGGER.info("pr.created", extra={"url": pr_url})
            return pr_url
        else:
            _LOGGER.warning(
                "pr.failed",
                extra={"stderr": pr_result.stderr[:500]},
            )
    except FileNotFoundError:
        _LOGGER.warning("pr.gh_cli_missing")
    return None


# ===================================================================
# 6.  Per-branch orchestration
# ===================================================================

async def run_branch(
    branch: str,
    repo_dir: Path,
    settings: Settings,
    token: str,
    repo: str,
) -> dict[str, Any]:
    """Run the full pipeline on a single branch.  Returns a result dict."""
    result: dict[str, Any] = {
        "branch": branch,
        "resolved": False,
        "attempts": 0,
        "duration_s": 0.0,
        "error": None,
        "pr_url": None,
    }
    t0 = time.monotonic()

    try:
        # Switch to the branch
        checkout_branch(repo_dir, branch)
        # Sequential CLI: a blocking path call in this async fn is harmless.
        workspace = str(repo_dir.resolve())  # noqa: ASYNC240

        # Detect crash
        crash_output = detect_crash(repo_dir)
        crash_report = build_crash_report(branch, crash_output, repo_dir)
        trigger = build_trigger(crash_report)

        # Build initial state
        initial_state = HealingState(
            trigger=trigger,
            workspace_path=workspace,
            attempt_count=0,
            failed_attempts=[],
            logs=[],
            is_resolved=False,
        )

        # Wire dependencies
        deps = build_real_deps(settings, workspace)
        graph = build_graph(deps)

        # Run pipeline
        thread_id = f"bench-{branch}-{uuid.uuid4().hex[:6]}"
        config = {"configurable": {"thread_id": thread_id}}
        _LOGGER.info(
            "pipeline.start",
            extra={"branch": branch, "thread_id": thread_id},
        )

        # Route LLM token/cost spans (litellm callback) into this run's sink.
        from src.observability.llm import using_llm_sink

        with using_llm_sink(deps.telemetry):
            final_state = cast(
                HealingState,
                await graph.ainvoke(initial_state, config=config),
            )
            # litellm flushes its async success callback as a detached task;
            # yield briefly so the tail LLM call (Reporter) lands in telemetry.
            await asyncio.sleep(0.2)

        result["resolved"] = final_state.get("is_resolved", False)
        result["attempts"] = final_state.get("attempt_count", 0)

        # Roll up the telemetry collected during the run (per-agent / per-node).
        aggregate = getattr(deps.telemetry, "aggregate", None)
        if callable(aggregate):
            result["telemetry"] = aggregate()

        # If resolved, push fix and open PR
        if result["resolved"]:
            pr_url = push_and_pr(repo_dir, branch, token, repo)
            result["pr_url"] = pr_url

    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        _LOGGER.error(
            "branch.failed",
            extra={"branch": branch, "error": str(exc)},
        )

    result["duration_s"] = round(time.monotonic() - t0, 2)
    return result


# ===================================================================
# 7.  Report writer
# ===================================================================

def write_report(results: list[dict[str, Any]], out_dir: Path) -> Path:
    """Write JSON + Markdown summary.  Returns path to the .md file."""
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")

    # JSON
    json_path = out_dir / f"benchmark_{ts}.json"
    json_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")

    # Markdown
    md_lines = [
        f"# Benchmark Report — {ts}\n",
        "",
        "| Branch | Resolved | Attempts | Duration (s) | PR | Error |",
        "|--------|----------|----------|-------------|----|-------|",
    ]
    resolved_count = 0
    for r in results:
        resolved_flag = "✅" if r["resolved"] else "❌"
        if r["resolved"]:
            resolved_count += 1
        pr_link = f"[PR]({r['pr_url']})" if r.get("pr_url") else "—"
        error = (r.get("error") or "—")[:80]
        md_lines.append(
            f"| {r['branch']} | {resolved_flag} | {r['attempts']} "
            f"| {r['duration_s']} | {pr_link} | {error} |"
        )
    md_lines.append("")
    md_lines.append(f"**Total: {len(results)} branches, {resolved_count} resolved.**\n")

    md_path = out_dir / f"benchmark_{ts}.md"
    md_path.write_text("\n".join(md_lines), encoding="utf-8")

    _LOGGER.info(
        "report.written",
        extra={"json": str(json_path), "md": str(md_path)},
    )
    return md_path


# ===================================================================
# 8.  Main entry point
# ===================================================================

async def async_main() -> int:
    """Run the full benchmark."""
    settings = load_settings()
    configure_logging(level=settings.log_level, json_mode=settings.log_json)

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        _LOGGER.error("GITHUB_TOKEN environment variable is required.")
        return 1

    repo = os.environ.get("BENCHMARK_REPO", DEFAULT_REPO)

    # Workspace inside the project tree (gitignored via .workspaces/)
    workspace_root = _PROJECT_ROOT / ".workspaces" / "benchmark"
    repo_dir = workspace_root / "repo"

    # Wipe the entire benchmark workspace so every run starts from
    # bytes-identical state.  Leftover __pycache__, session test files,
    # half-applied edits or sibling directories from a previous run
    # otherwise leak into the new clone and confuse downstream diffs.
    if workspace_root.exists():
        _purge_readonly_tree(workspace_root)
        if workspace_root.exists():
            raise RuntimeError(
                f"Could not wipe {workspace_root!s}. Close any process "
                "holding files in it (editor, file explorer, prior "
                "python.exe) and retry."
            )
        _LOGGER.info("workspace.cleaned", extra={"path": str(workspace_root)})
    workspace_root.mkdir(parents=True, exist_ok=True)

    _LOGGER.info(
        "benchmark.start",
        extra={"repo": repo, "workspace": str(workspace_root)},
    )

    clone_repo(token, repo, repo_dir)
    branches = list_remote_branches(repo_dir)

    if not branches:
        _LOGGER.error("No branches found (excluding main/master).")
        return 1

    _LOGGER.info(
        "branches.found",
        extra={"count": len(branches), "branches": branches},
    )

    # Clean any leftover minisweagent containers from previous runs
    MiniSWEFixer.cleanup_all_containers()

    results: list[dict[str, Any]] = []
    interrupted = False
    for i, branch in enumerate(branches, 1):
        _LOGGER.info(
            "branch.start",
            extra={"index": i, "total": len(branches), "branch": branch},
        )
        try:
            r = await run_branch(branch, repo_dir, settings, token, repo)
        except (KeyboardInterrupt, asyncio.CancelledError):
            _LOGGER.warning("branch.interrupted", extra={"branch": branch})
            results.append({
                "branch": branch, "resolved": False, "attempts": 0,
                "duration_s": 0.0, "error": "KeyboardInterrupt", "pr_url": None,
            })
            interrupted = True
            break

        results.append(r)

        status = "RESOLVED" if r["resolved"] else "FAILED"
        _LOGGER.info(
            "branch.done",
            extra={
                "branch": branch,
                "status": status,
                "attempts": r["attempts"],
                "duration_s": r["duration_s"],
            },
        )

        # Safety net: clean up any Docker containers between branches
        # so they don't accumulate if the library cleanup failed.
        MiniSWEFixer.cleanup_all_containers()

    # Final cleanup of any remaining Docker containers
    MiniSWEFixer.cleanup_all_containers()

    # Write report (even if interrupted — partial results are valuable)
    report_dir = _PROJECT_ROOT / "reports" / "benchmarks"
    md_path = write_report(results, report_dir)
    print(f"\n{'='*60}")
    if interrupted:
        print(f"  Benchmark INTERRUPTED — partial report at {md_path}")
    else:
        print(f"  Benchmark complete — report at {md_path}")
    print(f"{'='*60}\n")

    return 0


def main() -> int:
    """Run the async benchmark, translating interrupts into clean exits."""
    try:
        return asyncio.run(async_main())
    except KeyboardInterrupt:
        # Last-resort cleanup on Ctrl+C
        print("\nInterrupted — cleaning up Docker containers...")
        MiniSWEFixer.cleanup_all_containers()
        print("Done.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
