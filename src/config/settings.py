"""Application-wide configuration object.

Uses ``pydantic.BaseModel`` (kept dependency-light: no ``pydantic-settings``
requirement) and loads from environment variables via an explicit
factory. This keeps tests deterministic — they can instantiate the
settings object directly with overrides.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

_DEFAULT_REPORTS_DIR: Final[Path] = Path("./reports").resolve()


class Settings(BaseModel):
    """Frozen configuration shared by every layer of the pipeline."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # --- Pipeline mode ---------------------------------------------------
    agent_mode: Literal["mock", "real"] = Field(
        default="mock",
        description="'mock' uses deterministic in-memory agents; 'real' uses "
        "the mini-swe-agent Corrector and the litellm Tester/Reporter.",
    )

    # --- Retry policy ----------------------------------------------------
    max_retries: int = Field(
        default=3,
        ge=1,
        le=20,
        description="Maximum number of Fix → Validation cycles per error "
        "before escalating to post-mortem.",
    )

    # --- Chained-error policy --------------------------------------------
    error_cycle_budget: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Maximum number of distinct (chained) errors healed in a "
        "single session before stopping. Bounds the outer healing loop.",
    )

    # --- Sandbox ---------------------------------------------------------
    sandbox_image: str = Field(
        default="self-healing-sandbox:latest",
        description="Default container image used by the sandbox adapter. "
        "Built locally from docker/sandbox.Dockerfile — ships pytest "
        "preinstalled so validation and the immunization gate can run "
        "the regression suite without a bootstrap step.",
    )
    sandbox_workdir: str = Field(
        default="/workspace",
        description="Container path the host workspace is bind-mounted at, and the "
        "working directory of both the Corrector container and the validation "
        "sandbox. Defaults to ``/workspace``; set ``/testbed`` for SWE-bench "
        "images, whose editable install resolves the package to ``/testbed``.",
    )
    sandbox_python: str = Field(
        default="python",
        description="Python interpreter invoked inside the sandbox/Corrector "
        "containers (pytest + reproduce commands). Defaults to ``python`` (on "
        "PATH). SWE-bench images install the repo's deps in a conda env, so set "
        "``/opt/miniconda3/envs/testbed/bin/python`` — the bare ``python`` there "
        "is the empty base env and fails with ``No module named pytest``.",
    )
    sandbox_mem_limit: str = Field(
        default="512m",
        description="Cgroup memory limit. See docker-py ``mem_limit``.",
    )
    sandbox_cpu_quota: int = Field(
        default=50_000,
        gt=0,
        description="CPU quota in microseconds per 100ms (Docker semantics).",
    )
    sandbox_timeout_seconds: float = Field(
        default=60.0,
        gt=0,
        description="Hard wall-clock timeout for the sandboxed command.",
    )
    sandbox_network_disabled: bool = Field(
        default=True,
        description="Disable container networking in the sandbox (security "
        "baseline; keep ``True`` for untrusted code). Some SWE-bench instances "
        "(e.g. requests) have tests that hit a network, so set "
        "``CDD_SANDBOX_ALLOW_NETWORK=1`` to relax it for official benchmark images.",
    )

    # --- Git -------------------------------------------------------------
    git_author_name: str = Field(default="self-healing-bot")
    git_author_email: str = Field(default="self-healing-bot@urv.cat")

    # --- Reporting -------------------------------------------------------
    reports_dir: Path = Field(
        default=_DEFAULT_REPORTS_DIR,
        description="Output directory for post-mortems.",
    )

    # --- Logging ---------------------------------------------------------
    log_level: str = Field(default="INFO")
    log_json: bool = Field(
        default=True,
        description="Emit one-JSON-object-per-line records (production mode).",
    )

    # --- Real agent settings (only used when agent_mode == "real") --------
    llm_model: str = Field(
        default="claude-sonnet-4-20250514",
        description="LLM model identifier shared by all three agents "
        "(litellm format, e.g. 'claude-sonnet-4-20250514', 'gpt-4o').",
    )
    sweagent_cost_limit: float = Field(
        default=3.0,
        gt=0,
        description="Maximum USD spend per mini-swe-agent invocation.",
    )
    sweagent_timeout_seconds: float = Field(
        default=300.0,
        gt=0,
        description="Wall-clock timeout for a single mini-swe-agent run.",
    )
    sweagent_step_limit: int = Field(
        default=0,
        ge=0,
        description="Max agent steps before stopping (0 = unlimited). A positive "
        "cap stops a weak local model that loops without submitting; partial "
        "edits are captured instead of the wall-clock timeout discarding them. "
        "~20 is a good value for qwen-class models.",
    )
    sweagent_trajectory_dir: str = Field(
        default="",
        description="When set, the Corrector saves each agent run's full "
        "trajectory (reasoning + shell commands + final diff) as JSON in this "
        "directory — the richest artefact for debugging the agent's solutions. "
        "Empty disables it.",
    )
    sweagent_use_docker: bool = Field(
        default=True,
        description="Run mini-swe-agent inside a Docker container with the "
        "workspace bind-mounted.  Required on Windows because the local "
        "environment relies on POSIX shell semantics (bash, ``timeout``, "
        "``&&`` short-circuiting) that ``cmd.exe`` / PowerShell do not "
        "provide.  Set ``CDD_SWEAGENT_USE_DOCKER=0`` to force the local "
        "executor (Linux / macOS only).",
    )
    sweagent_docker_image: str = Field(
        default="python:3.12-slim",
        description="Container image used when ``sweagent_use_docker`` is "
        "true.  Must include a POSIX shell; ``git`` is not required because "
        "the diff capture runs on the host.",
    )
    tester_timeout_seconds: float = Field(
        default=180.0,
        gt=0,
        description="Wall-clock timeout for a single Tester (litellm) call.",
    )


def load_settings() -> Settings:
    """Build a :class:`Settings` instance from environment variables.

    Unknown variables are ignored. Type coercion is performed by
    Pydantic. This indirection allows DI containers to override values
    at compose time without monkey-patching.
    """
    overrides: dict[str, Any] = {}
    if (val := os.getenv("CDD_AGENT_MODE")) is not None:
        overrides["agent_mode"] = val
    if (val := os.getenv("CDD_MAX_RETRIES")) is not None:
        overrides["max_retries"] = int(val)
    if (val := os.getenv("CDD_ERROR_CYCLE_BUDGET")) is not None:
        overrides["error_cycle_budget"] = int(val)
    if (val := os.getenv("CDD_SANDBOX_IMAGE")) is not None:
        overrides["sandbox_image"] = val
    if (val := os.getenv("CDD_SANDBOX_TIMEOUT")) is not None:
        overrides["sandbox_timeout_seconds"] = float(val)
    if (val := os.getenv("CDD_SANDBOX_WORKDIR")) is not None:
        overrides["sandbox_workdir"] = val
    if (val := os.getenv("CDD_SANDBOX_PYTHON")) is not None:
        overrides["sandbox_python"] = val
    if (val := os.getenv("CDD_SANDBOX_ALLOW_NETWORK")) is not None:
        overrides["sandbox_network_disabled"] = val.lower() not in {"1", "true", "yes"}
    if (val := os.getenv("CDD_REPORTS_DIR")) is not None:
        overrides["reports_dir"] = Path(val).resolve()
    if (val := os.getenv("CDD_LOG_LEVEL")) is not None:
        overrides["log_level"] = val
    if (val := os.getenv("CDD_LOG_JSON")) is not None:
        overrides["log_json"] = val.lower() in {"1", "true", "yes"}
    if (val := os.getenv("CDD_LLM_MODEL")) is not None:
        # Strip surrounding whitespace: a stray trailing space in the deployment
        # name (easy to introduce when exporting the var) makes Azure return an
        # opaque 404 "Resource not found" — guard against it once and for all.
        overrides["llm_model"] = val.strip()
    if (val := os.getenv("CDD_SWEAGENT_COST_LIMIT")) is not None:
        overrides["sweagent_cost_limit"] = float(val)
    if (val := os.getenv("CDD_SWEAGENT_TIMEOUT")) is not None:
        overrides["sweagent_timeout_seconds"] = float(val)
    if (val := os.getenv("CDD_SWEAGENT_STEP_LIMIT")) is not None:
        overrides["sweagent_step_limit"] = int(val)
    if (val := os.getenv("CDD_SWEAGENT_TRAJECTORY_DIR")) is not None:
        overrides["sweagent_trajectory_dir"] = val
    if (val := os.getenv("CDD_SWEAGENT_USE_DOCKER")) is not None:
        overrides["sweagent_use_docker"] = val.lower() in {"1", "true", "yes"}
    if (val := os.getenv("CDD_SWEAGENT_DOCKER_IMAGE")) is not None:
        overrides["sweagent_docker_image"] = val
    if (val := os.getenv("CDD_TESTER_TIMEOUT")) is not None:
        overrides["tester_timeout_seconds"] = float(val)
    return Settings(**overrides)
