"""mini-swe-agent adapter implementing :class:`FixerPort` (the Corrector).

`mini-swe-agent <https://github.com/SWE-agent/mini-SWE-agent>`_ is a
lightweight autonomous coding agent (~100 lines) that modifies files
**in-place** using shell commands.  The adapter follows an
*apply-in-place* contract: the agent's edits stay on the working tree
and the orchestrator runs validation directly against them.  A unified
diff is still captured and wrapped in a :class:`Patch` for audit /
post-mortem use, but it is never re-applied.  Rollback of a failed
attempt is the responsibility of the Rollback node (``git reset --hard``
+ ``git clean -fd``).

Fix-first: the Corrector is driven by a :class:`FixContext` that describes
either a production crash (raw traceback + reproduction command) or a
failing test (pytest output + node id).  It makes the SMALLEST change that
clears the error and never writes tests — immunization is the Tester's job.

The agent uses **litellm** under the hood, so any provider supported
by litellm (OpenAI, Anthropic, Azure, …) works out of the box.
Configure the API key via the standard environment variable
(``OPENAI_API_KEY``, ``ANTHROPIC_API_KEY``, etc.).

Install
-------
``pip install mini-swe-agent``
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import re
import subprocess
import time
from pathlib import Path
from typing import Final

# Suppress mini-swe-agent's Rich startup banner which crashes on
# Windows terminals that don't support Unicode emojis (cp1252).
os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")

from src.agents._focus import render_focus_block
from src.core.domain import FixContext, Patch
from src.core.exceptions import FixGenerationError
from src.core.ports import FixerPort

_LOGGER = logging.getLogger(__name__)

# Regex to match Windows drive letters like C:\ or D:\
_WIN_DRIVE_RE = re.compile(r"^([A-Za-z]):[/\\]")

# How many bytes of the agent's chat trajectory to keep when reporting
# "submitted with no changes".  Bounded so the post-mortem stays readable.
_TRAJECTORY_TAIL_BYTES: Final[int] = 2 * 1024

# ---------------------------------------------------------------------------
# Prompt templates (tool-calling native).
#
# mini-swe-agent's ``LitellmModel`` ALWAYS calls the provider with
# ``tools=[bash]`` and *requires* a tool call back — an empty ``tool_calls`` is
# a hard FormatError ("No tool calls found in the response").  Its stock
# ``default`` config, however, instructs the model to answer with a
# ``mswea_bash_command`` **text block**, so a model that actually obeys the
# prompt (e.g. gpt-4.1-mini) puts the command in ``content`` and is rejected
# *every* turn.  We therefore drive the agent with our own tool-calling-native
# prompt (the shape of mini-swe-agent's ``mini`` config) and additionally pass
# ``tool_choice="required"`` (see ``_MODEL_KWARGS``) so the model can never burn
# a turn without acting.
#
# We also steer edits away from ``sed`` whitespace-surgery — the single most
# common way a weak model "fixes" an ``IndentationError`` by breaking the file
# a different way — toward rewriting the whole file with a here-document.
# Finally, the shell runs in a Linux container, so we say so: the stock
# template leaks the *host* OS (e.g. "Windows 10"), which is wrong and invites
# Windows-only commands.
#
# Both templates follow the PICCO prompt structure (Persona, Intention,
# Context, Conditions, Output), split across the conversation the way chat
# prompts are: the system template carries Persona + Intention, the instance
# template carries Context (the rendered task) + Conditions + Output.
_SYSTEM_TEMPLATE: Final[str] = """\
## Persona
You are the Corrector: an autonomous software-fixing agent operating a Linux \
shell inside a container.

## Intention
Repair the failure described in the task with the SMALLEST change that makes \
its success command exit with code 0 — verified by actually running it.

## Output
You act ONLY by calling the `bash` tool: every response must contain at least \
one `bash` tool call. Plain prose with no tool call does nothing and is \
rejected.\
"""

_INSTANCE_TEMPLATE: Final[str] = """\
## Context
{{task}}

## Conditions
You are at the repository root in a Linux (python:3.12-slim) container. Work in
small steps; each step is exactly one `bash` tool call:
1. Print the offending file's ENTIRE contents (e.g. `cat -n path/to/file.py`),
   not just a window — you must see every line before you change it.
2. Apply the fix.
3. Run the success command and confirm it exits with code 0.

Editing rules (important): to change more than a single token — and ALWAYS for
indentation / `IndentationError` fixes — do NOT add or remove spaces with `sed`.
Counting whitespace with `sed` is error-prone and frequently breaks the file a
new way. Instead rewrite the WHOLE file in one shot with a quoted here-document,
reproducing its current contents with the fix applied:

    cat > path/to/file.py <<'EOF'
    <the full corrected file goes here>
    EOF

CRITICAL: before a whole-file rewrite you MUST have printed the file's ENTIRE
current contents in an earlier step. Reproduce EVERY existing class, method,
function and line — change only what the fix needs. Never omit code you did not
display: dropping an unrelated method turns one bug into another. If the file is
too large to reproduce in full, make a targeted edit instead of a rewrite.

Indent Python with 4 spaces per level and keep every block consistent. Reserve
`sed` for a single, unambiguous one-line replacement.

## Output
One `bash` tool call per step. When — and only when — the success command has
exited with code 0, finish by issuing exactly
`echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` on its own, with no other command.
"""

# Passed straight to ``litellm.completion`` via ``LitellmModel.model_kwargs``.
# ``tool_choice="required"`` forces a ``bash`` tool call on every turn, which
# eliminates the "No tool calls found" thrashing that weak models fall into
# when they try to "explain" instead of act.
_MODEL_KWARGS: Final[dict[str, object]] = {"tool_choice": "required"}


def _docker_run_args(docker_path: str, workdir: str) -> list[str]:
    """Build ``docker run`` args: ephemeral container, workspace bind-mounted at *workdir*.

    On Windows we also add ``--add-host`` for ``host.docker.internal`` so the
    container can reach a model server (e.g. Ollama) running on the host. Pure
    and side-effect-free so it can be unit-tested without launching Docker.
    """
    run_args = ["--rm", "-v", f"{docker_path}:{workdir}"]
    if platform.system() == "Windows":
        run_args += ["--add-host", "host.docker.internal:host-gateway"]
    return run_args


class MiniSWEFixer(FixerPort):
    """Production Corrector backed by mini-swe-agent.

    Parameters
    ----------
    model_name:
        LLM model identifier understood by litellm (e.g.
        ``"claude-sonnet-4-20250514"``, ``"gpt-4o"``).
    workspace_path:
        Absolute path to the git repository mini-swe-agent will work
        in.  Must be a real git repo.
    cost_limit:
        Maximum USD spend per invocation (litellm billing).
    timeout_seconds:
        Wall-clock limit for a single agent run.
    step_limit:
        Maximum number of agent steps before stopping (``0`` = unlimited).
        Bounds the loop for weak local models that fail to submit; on the
        limit the agent returns gracefully and partial edits are captured.
    use_docker:
        When ``True`` the agent runs inside a Docker container
        (workspace mounted as a volume). Safer for untrusted repos.
    docker_image:
        Container image used when *use_docker* is ``True``.
    """

    def __init__(
        self,
        *,
        model_name: str = "claude-sonnet-4-20250514",
        workspace_path: str = ".",
        cost_limit: float = 3.0,
        timeout_seconds: float = 300.0,
        step_limit: int = 0,
        trajectory_dir: str | None = None,
        use_docker: bool = False,
        docker_image: str = "python:3.12-slim",
        container_workdir: str = "/workspace",
        python_executable: str = "python",
    ) -> None:
        """Store the configuration and verify mini-swe-agent is installed."""
        self._model_name = model_name
        self._workspace = workspace_path
        # Mount point + working directory inside the container. ``/workspace`` by
        # default; ``/testbed`` for SWE-bench images. See Settings.sandbox_workdir.
        self._container_workdir = container_workdir
        # Interpreter the agent's success command (pytest) invokes. ``python`` on
        # PATH by default; set an absolute conda-env path for SWE-bench images,
        # whose bare ``python`` is the empty base env (no pytest / repo deps).
        self._python_executable = python_executable
        self._cost_limit = cost_limit
        self._timeout = timeout_seconds
        # 0 ⇒ unlimited (rely on the wall-clock timeout). A positive cap bounds
        # the agent loop so a weak local model that keeps "thinking" stops after
        # N steps and we capture its partial edits, instead of the wall-clock
        # timeout cancelling the thread and discarding all of its work.
        self._step_limit = max(0, step_limit)
        # When set, the full agent trajectory (system prompt, every reasoning
        # turn, the shell commands it ran, their output, and the final diff) is
        # saved as JSON per fix attempt — the richest artefact for debugging
        # *why* the agent produced the solution it did.
        self._trajectory_dir = trajectory_dir
        self._use_docker = use_docker
        self._docker_image = docker_image
        self._verify_install()

    # ------------------------------------------------------------------
    # FixerPort
    # ------------------------------------------------------------------
    async def fix(self, context: FixContext) -> Patch | None:
        """See :meth:`FixerPort.fix`."""
        task = self._build_task(context)

        _LOGGER.info(
            "mini_swe_agent.fix.start",
            extra={
                "incident_id": context.incident_id,
                "model": self._model_name,
                "workspace": self._workspace,
            },
        )

        # 1) Run agent → modifies files in-place.  We keep the tail of
        #    the agent's chat trajectory so that, if no diff appears, we
        #    can surface *why* the agent thought it was done — the bare
        #    "no code changes" error tells the retry loop and the
        #    post-mortem nothing actionable.
        trajectory_tail = await self._run_agent(task, context.incident_id)

        # 2) Capture git diff for audit (files stay modified — apply-in-place).
        diff_text = await self._capture_diff()

        if not diff_text.strip():
            _LOGGER.warning(
                "mini_swe_agent.no_changes",
                extra={"trajectory_bytes": len(trajectory_tail)},
            )
            raise FixGenerationError(
                "mini-swe-agent submitted without editing any file. "
                "Trajectory tail:\n" + (trajectory_tail or "(empty)")
            )

        _LOGGER.info("mini_swe_agent.fix.done", extra={"diff_bytes": len(diff_text)})
        return Patch(diff_text=diff_text, author_agent="MiniSWEFixer")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _verify_install() -> None:
        """Fail fast if mini-swe-agent is not installed."""
        try:
            import minisweagent  # noqa: F401
        except ImportError as exc:
            raise FixGenerationError(
                "mini-swe-agent is not installed. "
                "Install with: pip install mini-swe-agent"
            ) from exc

    @staticmethod
    def _to_docker_volume_path(host_path: str) -> str:
        r"""Convert an OS-native path to the format Docker expects for ``-v``.

        On **Linux / macOS** paths are already POSIX — returned unchanged.

        On **Windows** Docker Desktop requires either forward slashes
        (``C:/Users/…``) or the ``/c/Users/…`` POSIX-style mount.
        We emit the ``/c/…`` form because it works reliably across
        Docker Desktop backends (Hyper-V, WSL 2) and older versions.

        Examples::

            C:\\Users\\dev\\repo  →  /c/Users/dev/repo
            /home/dev/repo      →  /home/dev/repo   (unchanged)
        """
        if platform.system() != "Windows":
            return host_path

        # Normalise backslashes first
        posix = host_path.replace("\\", "/")

        # Convert drive letter:  C:/… → /c/…
        match = _WIN_DRIVE_RE.match(posix)
        if match:
            drive = match.group(1).lower()
            posix = f"/{drive}/{posix[3:]}"

        _LOGGER.debug(
            "mini_swe_agent.docker_path",
            extra={"original": host_path, "docker": posix},
        )
        return posix

    def _build_task(self, context: FixContext) -> str:
        """Compose the ``task`` string fed to ``agent.run(task)``.

        Describes the *problem*, not the *method*: mini-swe-agent's stock
        ``instance_template`` already prescribes the workflow.  Handles both
        entry modes — a failing test (run that node) and a raw crash (run the
        reproduction command).  The pieces we add are the facts the agent
        could not derive on its own:

        * What command defines success (the failing pytest node, or the crash
          reproduction command) — the agent must see it exit 0 before
          submitting.
        * The captured failure output, so diagnosis starts in context.
        * The pre-loaded contents of the workspace files named in the failure
          trace — without this weak local models waste turns on ``ls``/``cat``
          or mistake an import-path error for a missing file and rewrite it.
        * A hard rule: do not submit before the command exits 0 (qwen-14B
          class models bail early otherwise).
        * The output of any previous failed attempts so retries do not repeat
          the same edit.  The Corrector never writes tests.
        """
        node_id = context.reproducer_node_id
        reproduce = " ".join(context.reproduce_cmd) if context.reproduce_cmd else "python main.py"
        parts: list[str] = []
        if node_id:
            success_cmd = f"{self._python_executable} -m pytest {node_id} -x --tb=short"
            parts.append(f"A pytest test is currently failing in this repository:\n  {node_id}\n\n")
        else:
            success_cmd = reproduce
            parts.append(f"The command `{reproduce}` crashes in this repository.\n\n")
        parts.extend([
            "## Failure output\n",
            f"```\n{context.failure_output}\n```\n\n",
        ])
        focus_block = self._render_focus_files(context.failure_output, node_id)
        if focus_block:
            parts.append(focus_block)
        parts.extend([
            "## Success criterion\n",
            f"Make `{success_cmd}` exit with code 0 by editing production\n",
            "source files.  Make the SMALLEST change that clears the error.\n",
            "Do NOT write or modify any test files — immunization is handled\n",
            "separately by another agent.\n",
            "\n",
            "DO NOT submit the task (do not issue\n",
            "`echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`) until you have\n",
            f"actually run `{success_cmd}` in the shell and seen it exit with\n",
            "code 0.  Reading files is not a fix.\n",
        ])
        if context.previous_attempts:
            parts.append(
                "\n## Previous failed attempts\n"
                "Earlier iterations of this same fix already failed.  The raw\n"
                "output of each is below; use it to diagnose what went wrong\n"
                "instead of repeating the same edit.\n\n"
            )
            for i, summary in enumerate(context.previous_attempts):
                parts.append(f"### Attempt {i + 1}\n```\n{summary}\n```\n\n")
        return "".join(parts)

    # ------------------------------------------------------------------
    # Focus-file collection (shared with the Tester — see ``_focus``)
    # ------------------------------------------------------------------
    def _render_focus_files(self, failure_output: str, node_id: str | None) -> str:
        """Embed the workspace sources named in the failure trace (or ``""``)."""
        return render_focus_block(self._workspace, failure_output, node_id)

    async def _run_agent(self, task: str, incident_id: str = "incident") -> str:
        """Invoke mini-swe-agent via its Python API in a worker thread.

        Returns the tail of the agent's chat trajectory (last ~2 KB of
        the formatted message log) so the caller can surface *why* the
        agent thought it was done if no diff appears.  Without this
        signal the retry loop and the post-mortem see only the generic
        "no code changes" string and cannot diagnose the bail-early
        failure mode common to small local models.

        Container cleanup is handled in two layers:

        1. The ``finally`` block inside ``_blocking()`` calls the
           environment's own ``cleanup()`` (works on Linux).
        2. After the thread completes (or times out), we explicitly
           stop any containers whose names start with ``minisweagent-``
           via ``docker stop`` + ``docker rm``.  This is the reliable
           fallback on **Windows** where the shell-based cleanup inside
           ``DockerEnvironment.cleanup()`` uses ``timeout`` (a Linux
           command) and silently fails.
        """
        # Snapshot existing minisweagent containers BEFORE starting the
        # agent so we can compute which ones are new (ours).
        existing_ids = self._list_docker_containers() if self._use_docker else set()

        # Optional: where to save the full agent trajectory for debugging.
        traj_path: Path | None = None
        if self._trajectory_dir:
            safe = re.sub(r"[^\w.-]", "_", incident_id)[:80]
            stamp = time.strftime("%Y%m%d-%H%M%S")
            traj_path = Path(self._trajectory_dir) / f"{safe}-{stamp}.json"

        def _blocking() -> list[dict]:
            import litellm
            from minisweagent.agents.default import DefaultAgent
            from minisweagent.models.litellm_model import LitellmModel

            # Reasoning / *codex* models reject params like ``temperature`` != 1.
            # Dropping unsupported params (globally, so mini-swe-agent's own
            # litellm call is covered too) keeps the Corrector model-agnostic.
            litellm.drop_params = True

            # ``tool_choice="required"`` (in _MODEL_KWARGS) forces a bash tool
            # call every turn — without it weak models burn turns "explaining"
            # and hit the "No tool calls found" FormatError repeatedly.
            model = LitellmModel(model_name=self._model_name, model_kwargs=_MODEL_KWARGS)

            if self._use_docker:
                from minisweagent.environments.docker import DockerEnvironment

                docker_path = self._to_docker_volume_path(self._workspace)
                _LOGGER.info(
                    "mini_swe_agent.docker_env",
                    extra={
                        "image": self._docker_image,
                        "workspace": self._workspace,
                        "docker_volume": docker_path,
                        "platform": platform.system(),
                    },
                )

                run_args = _docker_run_args(docker_path, self._container_workdir)

                env = DockerEnvironment(
                    image=self._docker_image,
                    cwd=self._container_workdir,
                    run_args=run_args,
                    forward_env=["OLLAMA_API_BASE"],
                    timeout=120,
                )
            else:
                from minisweagent.environments.local import LocalEnvironment

                env = LocalEnvironment(cwd=self._workspace)

            try:
                agent = DefaultAgent(
                    model,
                    env,
                    system_template=_SYSTEM_TEMPLATE,
                    instance_template=_INSTANCE_TEMPLATE,
                    cost_limit=self._cost_limit,
                    step_limit=self._step_limit,
                    output_path=traj_path,
                )
                result = agent.run(task)
                if traj_path is not None:
                    _LOGGER.info("mini_swe_agent.trajectory_saved", extra={"path": str(traj_path)})
            finally:
                # Layer 1: library's own cleanup (works on Linux).
                _cleanup = getattr(env, "cleanup", None)
                if callable(_cleanup):
                    try:
                        _cleanup()
                    except Exception:
                        _LOGGER.debug("mini_swe_agent.library_cleanup_failed", exc_info=True)

            exit_status = result.get("exit_status", "unknown")
            _LOGGER.info(
                "mini_swe_agent.run.done",
                extra={"exit_status": exit_status, "n_messages": len(agent.messages)},
            )
            if exit_status == "error":
                raise FixGenerationError(
                    f"mini-swe-agent exited with error: {result}"
                )
            return list(agent.messages)

        try:
            messages = await asyncio.wait_for(
                asyncio.to_thread(_blocking),
                timeout=self._timeout,
            )
        except TimeoutError as exc:
            raise FixGenerationError(
                f"mini-swe-agent timed out after {self._timeout}s"
            ) from exc
        finally:
            # Layer 2: explicit Docker cleanup — catches containers
            # that the library's cleanup missed (Windows) or that
            # survived a timeout cancellation.
            if self._use_docker:
                await asyncio.to_thread(
                    self._kill_new_containers, existing_ids
                )
        return self._format_trajectory_tail(messages)

    @staticmethod
    def _format_trajectory_tail(messages: list[dict]) -> str:
        """Render the last few agent messages as a short diagnostic string.

        We render newest-first so the budget cap drops the OLDEST turns
        first — the final assistant turn (which contains the submission
        action and the reasoning behind it) is always preserved.  The
        format intentionally mirrors ``role: content`` rather than the
        full JSON so it stays readable when inlined into an error message
        or a post-mortem.
        """
        rendered: list[str] = []
        budget = _TRAJECTORY_TAIL_BYTES
        for msg in reversed(messages):
            role = msg.get("role", "?")
            content = msg.get("content", "")
            if not isinstance(content, str):
                content = str(content)
            chunk = f"[{role}]\n{content.strip()}\n"
            if len(chunk) > budget:
                # Truncate the OLDEST surviving turn rather than dropping it
                # entirely; gives the reader at least a tail of context.
                chunk = chunk[:budget] + "\n... (truncated)\n"
                rendered.append(chunk)
                break
            rendered.append(chunk)
            budget -= len(chunk)
        # Re-reverse so the output reads chronologically.
        return "\n".join(reversed(rendered))

    async def _capture_diff(self) -> str:
        """Capture **all** changes the agent made (tracked + new files).

        ``git diff`` alone only shows changes to tracked files.  If the
        agent created new files they are *untracked* and invisible to
        ``git diff``.  We use ``git add -N`` ("intent to add") first so
        that new files appear as diffs of empty → content.

        The diff is captured for audit / post-mortem only; the
        modifications stay on the working tree so the Validation node
        runs against the files the agent just edited.
        """
        # 1) Register any new (untracked) files so they appear in the diff.
        add_proc = await asyncio.create_subprocess_exec(
            "git", "add", "-N", ".",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._workspace,
        )
        await add_proc.communicate()  # best-effort, ignore errors

        # 2) Now ``git diff`` sees both modified tracked files AND new files.
        proc = await asyncio.create_subprocess_exec(
            "git", "diff",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._workspace,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = (stderr or b"").decode("utf-8", errors="replace")
            _LOGGER.error("mini_swe_agent.git_diff.failed", extra={"stderr": err})
            raise FixGenerationError(f"git diff failed: {err}")

        diff_text = (stdout or b"").decode("utf-8", errors="replace")
        _LOGGER.debug(
            "mini_swe_agent.capture_diff",
            extra={"diff_bytes": len(diff_text), "diff_preview": diff_text[:500]},
        )
        return diff_text

    # ------------------------------------------------------------------
    # Docker container management
    # ------------------------------------------------------------------
    @staticmethod
    def _list_docker_containers() -> set[str]:
        """Return IDs of running containers whose name starts with ``minisweagent-``."""
        try:
            result = subprocess.run(
                ["docker", "ps", "-q", "--filter", "name=minisweagent-"],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0 and result.stdout.strip():
                return set(result.stdout.strip().splitlines())
        except Exception:
            pass
        return set()

    @staticmethod
    def _kill_new_containers(existing_ids: set[str]) -> None:
        """Stop and remove any minisweagent containers not in *existing_ids*.

        This is the cross-platform safety net that catches containers
        which ``DockerEnvironment.cleanup()`` missed — notably on
        **Windows** where its shell-based ``timeout`` command fails.
        """
        current_ids = MiniSWEFixer._list_docker_containers()
        new_ids = current_ids - existing_ids
        if not new_ids:
            return
        _LOGGER.info(
            "mini_swe_agent.docker_cleanup",
            extra={"containers": list(new_ids)},
        )
        for cid in new_ids:
            try:
                subprocess.run(
                    ["docker", "stop", cid],
                    capture_output=True, timeout=30,
                )
                # --rm flag should auto-remove after stop, but force-rm
                # just in case.
                subprocess.run(
                    ["docker", "rm", "-f", cid],
                    capture_output=True, timeout=15,
                )
            except Exception:
                _LOGGER.debug(
                    "mini_swe_agent.docker_cleanup.failed",
                    extra={"container_id": cid},
                    exc_info=True,
                )

    @staticmethod
    def cleanup_all_containers() -> None:
        """Stop and remove ALL minisweagent containers.

        Intended as a safety-net called between benchmark branches or
        at the end of a benchmark run.  Not part of normal pipeline flow.
        """
        all_ids = MiniSWEFixer._list_docker_containers()
        if all_ids:
            MiniSWEFixer._kill_new_containers(existing_ids=set())
        # Also catch stopped-but-not-removed containers.
        try:
            result = subprocess.run(
                ["docker", "ps", "-aq", "--filter", "name=minisweagent-"],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0 and result.stdout.strip():
                for cid in result.stdout.strip().splitlines():
                    subprocess.run(
                        ["docker", "rm", "-f", cid],
                        capture_output=True, timeout=15,
                    )
        except Exception:
            pass
