# Self-Healing System

> A **fix-first, multi-agent self-healing pipeline**: when a deployment crashes or a CI
> test fails, three LLM agents repair the code inside a Docker sandbox, immunize the fix
> with a targeted regression test, and open a GitHub Pull Request — **never merging**.
> Human review is the only merge gate.

This repository is the code annex of a Bachelor's Thesis (TFG) in Computer Engineering.
The pipeline is **model-agnostic** (any [litellm](https://github.com/BerriAI/litellm)
provider) and ships in-memory mocks so the whole graph runs offline in milliseconds.

**Validated in real conditions** — gpt-4.1-mini on Azure AI Foundry:

| Evidence | Result |
|---|---|
| Own benchmark (8 seeded crash scenarios) | **8/8 healed**, ~$0.045/run, ~4 min, 0 retries |
| Live PR on a latent bug (CI test, clean `main.py`) | PR opened in 27 s, **$0.002** |
| Live PR on a seeded bug (failing CI test) | One-line fix, **$0.0027**, accurate commit message |

---

## 1. How it heals

```mermaid
flowchart LR
    T[Trigger\ncrash / failing test] --> B[bootstrap]
    B --> F[fix\nCorrector]
    F --> V[validate\nsandbox]
    V -->|green or new error| I[immunize\nTester + gate]
    I --> C[report_commit\nReporter]
    C -->|residual chained error| F
    C -->|all green| E([END])
    V -->|same error| R[rollback]
    R -->|budget left| F
    R -->|exhausted| P[post_mortem] --> E
```

- **Fix-first**: the Corrector edits the working tree immediately; no test is written
  before the fix.
- **Chained errors**: every failure is fingerprinted (`ErrorSignature` =
  kind + exception type + location). If fixing error A surfaces a *different* error B,
  A is committed and the loop re-enters for B — each healed error gets its own commit.
- **Immunization gate**: after a green fix (crash-entry only), the Tester writes a
  *targeted* regression test — it sees the failure, the fix diff and the real source,
  so it attacks the specific bug with the real API. The test is executed in the sandbox
  before being kept; if it fails, a deterministic smoke test takes its place.
  On test-entry the failing CI test *is* the regression test, so immunization is skipped.
- **Budgets everywhere**: per-error retry budget, chained-error cycle budget, agent cost
  and step limits, sandbox timeouts.

## 2. The three agents

| Agent | Implementation | Role |
|---|---|---|
| **Corrector** | [mini-swe-agent](https://github.com/SWE-agent/mini-SWE-agent) 2.2.8 (unmodified, driven by our own prompts/config) | Edits production code in a Docker container until the reproduction command exits 0. Never touches tests. |
| **Tester** | `LLMTester` (single litellm call) | Writes one targeted regression test per healed crash; gated in the sandbox before committing. |
| **Reporter** | `LLMReporter` (single litellm call, never blocks) | Conventional-Commits message per healed error; post-mortem narrative when the budget is exhausted. |

All three prompts follow the **PICCO** structure (Persona, Intention, Context,
Conditions, Output) — see `src/agents/`.

## 3. Architecture

Hexagonal (ports & adapters): the orchestrator depends only on `typing.Protocol` ports
and never imports a concrete adapter. Telemetry is layered on with GoF object
decorators, so business code contains zero instrumentation.

```
src/
├── core/                  # Pure application core (no I/O)
│   ├── domain/            # Models, HealingState, ErrorSignature, Span
│   └── ports/             # Protocols: Fixer/Tester/ReporterAgent, Sandbox, Git, GitHub, Telemetry
├── agents/                # LLM adapters (mini-swe-agent, litellm) + mocks
├── infrastructure/        # Docker sandbox, git ops, GitHub REST, filesystem reports
├── observability/         # Decorator instrumentation, spans, sinks, litellm cost callback
├── orchestrator/          # LangGraph StateGraph: nodes, routers, Dependencies
└── config/                # Env-driven settings + structured logging
scripts/
├── run_benchmark.py       # Benchmark harness (clones the bug-scenario repo)
└── heal_and_pr.py         # Real-environment entry point (heal → push → PR)
deploy/
└── selfheal-on-push.yml   # Workflow TEMPLATE for the monitored repository
```

## 4. Quick start

```bash
pip install -r requirements.txt
docker build -t self-healing-sandbox:latest -f docker/sandbox.Dockerfile .

# Offline demo (mock agents, no LLM, milliseconds)
python -m src.main

# Test suite
python -m pytest tests/ -q
```

### Run the benchmark

```bash
export GITHUB_TOKEN=ghp_...                     # repo scope on the benchmark repo
export OPENAI_API_KEY=... OPENAI_API_BASE=...   # any litellm-supported provider works
export CDD_LLM_MODEL=openai/gpt-4.1-mini
export CDD_SWEAGENT_USE_DOCKER=1
python scripts/run_benchmark.py
```

Reports land in `reports/benchmarks/` (JSON + Markdown, with per-agent token/cost
telemetry); full agent trajectories in `reports/traces/`.

### Heal a real repository (PR-on-CI)

```bash
export CDD_SWEAGENT_DOCKER_IMAGE=self-healing-sandbox:latest  # pytest inside the Corrector's container

# auto: probe failing tests first, then a crashing main.py; exit 0 when green
python scripts/heal_and_pr.py --repo owner/name --base main --auto

# explicit modes
python scripts/heal_and_pr.py --repo owner/name --base main                  # crash-entry
python scripts/heal_and_pr.py --repo owner/name --base main --test tests/test_x.py::test_y
python scripts/heal_and_pr.py --repo owner/name --base main --auto --no-pr  # heal locally, no PR
```

### Close the loop with GitHub Actions

- **On push (the full loop)**: copy [`deploy/selfheal-on-push.yml`](deploy/selfheal-on-push.yml)
  into the *monitored* repo's `.github/workflows/`. Every push probes the repo and, on a
  failing test or crash, the multi-agent system opens a fix PR. Anti-loop guards included.
- **On demand**: [`.github/workflows/self-heal.yml`](.github/workflows/self-heal.yml)
  (`workflow_dispatch`) heals any repo by name.

## 5. Configuration

All settings are environment variables (see `src/config/settings.py`):
`CDD_LLM_MODEL`, `CDD_MAX_RETRIES`, `CDD_ERROR_CYCLE_BUDGET`, `CDD_SANDBOX_IMAGE`,
`CDD_SANDBOX_TIMEOUT`, `CDD_SWEAGENT_COST_LIMIT`, `CDD_SWEAGENT_STEP_LIMIT`,
`CDD_SWEAGENT_USE_DOCKER`, `CDD_SWEAGENT_DOCKER_IMAGE`, `CDD_SWEAGENT_TRAJECTORY_DIR`,
`CDD_SANDBOX_WORKDIR` (container mount point; `/workspace` by default, `/testbed` for SWE-bench),
`CDD_REPORTS_DIR`, `CDD_LOG_LEVEL`, `CDD_LOG_JSON` — plus the provider credentials
(`OPENAI_API_KEY`/`OPENAI_API_BASE`, `ANTHROPIC_API_KEY`, `OLLAMA_API_BASE`, …) and
`GITHUB_TOKEN` for clone/push/PR.

## 6. Observability

Every port and every graph node is wrapped by an `Instrumented*` decorator emitting
`Span`s; a litellm callback attributes **tokens and cost per agent** (contextvars tag
each completion as corrector/tester/reporter, including mini-swe-agent's internal
calls). Benchmark reports include the full aggregate; PR bodies include the heal's
total and per-agent cost.

Known profile: the Corrector dominates spend (~77%) through *prompt* tokens — it
resends its growing trajectory every step.

## 7. Safety

- All code execution (reproduction, fixing, test gating) happens inside Docker.
- The system **never merges** — it opens PRs; a human reviews.
- The GitHub token is never logged; push errors are scrubbed before raising.
- Workflow inputs flow through `env` (no shell injection); `selfheal/**` pushes are
  ignored to prevent heal-the-heal loops.

## 8. SWE-bench (Corrector scaling test)

The system also runs against a slice of [SWE-bench](https://www.swebench.com/) — pure
test-entry, so it exercises the Corrector at real-repo scale (not the Tester). Select a
cheap slice, sanity-check the env without an LLM, heal, and score with the official harness:

```bash
python scripts/swebench_select.py --limit 8 --out slice.json
python scripts/run_swebench.py --instances-file slice.json --sanity      # no LLM
python scripts/run_swebench.py --instances-file slice.json --predictions-out preds.jsonl
```

Full guide: [`docs/SWEBENCH.md`](docs/SWEBENCH.md).

## 9. Documentation

- [`docs/SelfHealingSystem.docx`](docs/SelfHealingSystem.docx) — extensive technical
  documentation (architecture, agents, pipeline, observability, evaluation).
- [`docs/MEMORIA_TFG.md`](docs/MEMORIA_TFG.md) + [`docs/ANEXO_CODIGO.md`](docs/ANEXO_CODIGO.md) —
  Spanish thesis memoir (concepts) and line-by-line code annex.
- [`docs/SWEBENCH.md`](docs/SWEBENCH.md) — running against a SWE-bench slice.
