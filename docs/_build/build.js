/* Builds the Self-Healing System technical documentation (.docx).
 * Run: node docs/_build/build.js  ->  docs/SelfHealingSystem.docx
 */
const fs = require("fs");
const path = require("path");
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  AlignmentType, LevelFormat, HeadingLevel, BorderStyle, WidthType, ShadingType,
  Header, Footer, PageNumber, PageBreak, TableOfContents, VerticalAlign,
} = require("docx");

// ---- palette / metrics (A4, 1" margins) ------------------------------------
const MONO = "Consolas";
const ACCENT = "2E5AAC";
const CODE_BG = "F4F6F8";
const HEAD_BG = "D9E6F2";
const ZEBRA = "F7F9FB";
const CONTENT_W = 9026; // 11906 - 2*1440

// ---- inline run helpers ----------------------------------------------------
const t = (text, opts = {}) => new TextRun({ text, ...opts });
const b = (text) => new TextRun({ text, bold: true });
const code = (text) => new TextRun({ text, font: MONO, size: 19 });

const p = (children, opts = {}) => {
  if (typeof children === "string") children = [t(children)];
  return new Paragraph({ children, spacing: { after: 140, line: 276 }, ...opts });
};
const h1 = (text) => new Paragraph({ heading: HeadingLevel.HEADING_1, children: [t(text)], pageBreakBefore: true });
const h2 = (text) => new Paragraph({ heading: HeadingLevel.HEADING_2, children: [t(text)] });
const h3 = (text) => new Paragraph({ heading: HeadingLevel.HEADING_3, children: [t(text)] });

const bullet = (children) => {
  if (typeof children === "string") children = [t(children)];
  return new Paragraph({ numbering: { reference: "bul", level: 0 }, children, spacing: { after: 60, line: 276 } });
};
const numbered = (children) => {
  if (typeof children === "string") children = [t(children)];
  return new Paragraph({ numbering: { reference: "num", level: 0 }, children, spacing: { after: 60, line: 276 } });
};

const codeBlock = (lines) => lines.map((ln, i) => new Paragraph({
  children: [new TextRun({ text: ln === "" ? " " : ln, font: MONO, size: 18 })],
  shading: { type: ShadingType.CLEAR, fill: CODE_BG },
  border: { left: { style: BorderStyle.SINGLE, size: 14, color: ACCENT, space: 8 } },
  spacing: { before: i === 0 ? 80 : 0, after: i === lines.length - 1 ? 160 : 0, line: 240 },
}));

// ---- tables ----------------------------------------------------------------
const BORD = { style: BorderStyle.SINGLE, size: 1, color: "BBBBBB" };
const ALLBORD = { top: BORD, bottom: BORD, left: BORD, right: BORD };
const mkCell = (content, w, opts = {}) => {
  let kids;
  if (Array.isArray(content)) kids = content;            // array of Paragraph
  else kids = [p(typeof content === "string" ? [t(content)] : content, { spacing: { after: 0 } })];
  return new TableCell({
    width: { size: w, type: WidthType.DXA },
    columnSpan: opts.span,
    margins: { top: 60, bottom: 60, left: 110, right: 110 },
    verticalAlign: VerticalAlign.CENTER,
    shading: opts.fill ? { type: ShadingType.CLEAR, fill: opts.fill } : undefined,
    borders: ALLBORD,
    children: kids,
  });
};
const table = (headers, rows, widths) => {
  const head = new TableRow({
    tableHeader: true,
    children: headers.map((h, i) => mkCell([p([b(h)], { spacing: { after: 0 } })], widths[i], { fill: HEAD_BG })),
  });
  const body = rows.map((r, ri) => new TableRow({
    children: r.map((c, i) => mkCell(c, widths[i], { fill: ri % 2 ? ZEBRA : undefined })),
  }));
  return new Table({ width: { size: CONTENT_W, type: WidthType.DXA }, columnWidths: widths, rows: [head, ...body] });
};

const note = (label, children) => {
  if (typeof children === "string") children = [t(children)];
  return new Paragraph({
    children: [b(label + "  "), ...children],
    shading: { type: ShadingType.CLEAR, fill: "FFF8E1" },
    border: { left: { style: BorderStyle.SINGLE, size: 18, color: "E0A800", space: 10 } },
    spacing: { before: 80, after: 160, line: 276 },
  });
};
const spacer = () => new Paragraph({ children: [t("")], spacing: { after: 60 } });

// ============================================================================
//  CONTENT
// ============================================================================
const content = [];
const C = (...xs) => xs.forEach((x) => content.push(x));

// ---- Title page ------------------------------------------------------------
C(
  new Paragraph({ spacing: { before: 2600, after: 0 }, alignment: AlignmentType.CENTER,
    children: [new TextRun({ text: "Self-Healing System", bold: true, size: 64, font: "Arial", color: ACCENT })] }),
  new Paragraph({ alignment: AlignmentType.CENTER, spacing: { before: 160, after: 0 },
    children: [new TextRun({ text: "Autonomous Multi-Agent Code Repair", size: 30, font: "Arial" })] }),
  new Paragraph({ alignment: AlignmentType.CENTER, spacing: { before: 60, after: 0 },
    children: [new TextRun({ text: "Technical Documentation", italics: true, size: 26, color: "555555" })] }),
  new Paragraph({ alignment: AlignmentType.CENTER, spacing: { before: 1400, after: 0 },
    children: [new TextRun({ text: "Bachelor's Thesis (TFG) — Technical Annex", size: 24 })] }),
  new Paragraph({ alignment: AlignmentType.CENTER, spacing: { before: 80, after: 0 },
    children: [new TextRun({ text: "Author: Arnau Fabregas Figueras", size: 22 })] }),
  new Paragraph({ alignment: AlignmentType.CENTER, spacing: { before: 40, after: 0 },
    children: [new TextRun({ text: "2026", size: 22, color: "555555" })] }),
  new Paragraph({ children: [new PageBreak()] }),
);

// ---- TOC -------------------------------------------------------------------
C(
  new Paragraph({ heading: HeadingLevel.HEADING_1, children: [t("Contents")] }),
  new TableOfContents("Contents", { hyperlink: true, headingStyleRange: "1-3" }),
  new Paragraph({ children: [new PageBreak()] }),
);

// ===== 1. Introduction ======================================================
C(h1("1. Introduction"));
C(p([
  b("What it is.  "),
  t("The Self-Healing System is an autonomous, multi-agent service that sits inside a deployment "
    + "pipeline (production / QA) and repairs software errors "), b("without human intervention"),
  t(", driven only by the signal a real pipeline produces: a crash on the terminal, or a failing test."),
]));
C(p([
  b("The problem.  "),
  t("When a deploy crashes or a test goes red, a human has to read the traceback, locate the bug, edit the "
    + "code, write a regression test so it never comes back, and commit with a meaningful message. This is "
    + "repetitive, interrupt-driven toil. The goal of this project is to automate that whole loop."),
]));
C(p([
  b("The contribution.  "),
  t("The novelty is not a new coding model — it is the "), b("orchestration"),
  t(" around it: a fix-first pipeline that (1) repairs the error, (2) "), b("immunises"),
  t(" it with a targeted regression test, (3) "), b("documents"),
  t(" the fix with a Conventional-Commits message and a post-mortem, and (4) loops over "),
  b("chained errors"), t(" — when fixing one error surfaces a different one."),
]));
C(p("The system is model-agnostic (any provider supported by litellm) and is built on a hexagonal "
  + "architecture so each agent and each piece of infrastructure is a replaceable adapter."));
C(h2("1.1 Headline results"));
C(bullet([b("8 / 8"), t(" reproducible seeded crashes resolved autonomously (fix + targeted test + commit).")]));
C(bullet([t("~"), b("5 cents"), t(" (USD) and ~4 minutes for a full 9-branch benchmark run with "), code("openai/gpt-4.1-mini"), t(".")]));
C(bullet([b("110"), t(" unit/integration tests green; full per-agent latency, token and cost telemetry.")]));
C(bullet([t("Deployed for real: the system has opened "), b("live GitHub Pull Requests"),
  t(" healing both a latent bug and a seeded bug from a failing CI test (Chapter 9).")]));
C(note("Scope.", "This document describes the system as validated: the in-house benchmark (Chapter 7) and the "
  + "real GitHub PR-on-CI deployment with live pull requests (Chapter 9)."));

// ===== 2. Architecture ======================================================
C(h1("2. System Architecture"));
C(p([t("The system follows a "), b("hexagonal architecture"), t(" (ports & adapters). The application core "
  + "expresses what it needs as "), code("typing.Protocol"), t(" ports; concrete adapters (LLM agents, Docker, "
  + "git, filesystem) implement those ports and are wired together once at the composition root.")]));
C(h2("2.1 Why hexagonal"));
C(bullet([b("Model/tool independence: "), t("swap mini-swe-agent, the LLM provider, or the sandbox without touching the core.")]));
C(bullet([b("Testability: "), t("the whole pipeline runs against in-memory fakes — 89 tests, fully hermetic, in ~1 s.")]));
C(bullet([b("Clear contribution boundary: "), t("the third-party coding agent is a single adapter; the orchestration (our work) is the core.")]));
C(h2("2.2 Layers"));
C(table(
  ["Layer", "Package", "Responsibility"],
  [
    ["Domain", "src/core/domain", "Pure value objects: ErrorSignature, FixContext, Patch, RegressionTest, HealingState, Span. No I/O."],
    ["Ports", "src/core/ports", "Protocols: FixerPort, TesterPort, ReporterAgentPort, SandboxPort, GitPort, ReporterPort, TelemetryPort."],
    ["Adapters", "src/agents, src/infrastructure", "Concrete implementations: MiniSWEFixer, LLMTester, LLMReporter, DockerSandbox, GitAdapter, FilesystemReporter."],
    ["Orchestration", "src/orchestrator", "LangGraph state machine (nodes + routers) + the Dependencies container. Depends only on ports."],
    ["Observability", "src/observability", "Cross-cutting telemetry: decorators that wrap ports/nodes + a litellm callback."],
  ],
  [1500, 2700, 4826],
));
C(note("Invariant.", [t("The orchestrator "), b("never imports a concrete adapter"),
  t(". It depends on ports; the composition roots ("), code("src/main.py"), t(", "), code("scripts/run_benchmark.py"),
  t(") build the real adapters and inject them. The smoke-test fallback builder lives in the orchestrator "
    + "precisely to preserve this invariant.")]));
C(h2("2.3 The Dependencies container"));
C(p([t("A frozen dataclass ("), code("src/orchestrator/dependencies.py"),
  t(") bundles the collaborators each node needs: "), code("fixer"), t(", "), code("tester"), t(", "),
  code("reporter_agent"), t(", "), code("sandbox"), t(", "), code("git"), t(", "), code("reporter"),
  t(", and "), code("telemetry"), t(" (defaults to a no-op). Nodes close over this container via "),
  code("functools.partial"), t(", so unit tests can assemble ad-hoc bundles with fakes.")]));

// ===== 3. Pipeline ==========================================================
C(h1("3. The Healing Pipeline"));
C(p([t("The pipeline is a "), b("LangGraph"), t(" state machine (nodes + conditional routers, a "),
  code("MemorySaver"), t(" checkpointer). It is "), b("fix-first"),
  t(": correct the error, then validate, then immunise, then commit.")]));
C(h2("3.1 Topology"));
C(...codeBlock([
  "START → bootstrap → fix ─patch→ validate ─green/changed→ immunize → report_commit",
  "                      │ no patch       │ same error             │",
  "                      ▼                ▼                        ├─ residual error → fix",
  "                   rollback ◀──────────┘                        └─ done → END",
  "                      │ retries left → fix",
  "                      │ exhausted    → post_mortem → END",
]));
C(h2("3.2 Nodes"));
C(table(
  ["Node", "Does"],
  [
    ["bootstrap", "Creates the session regression file once, derives the reproduce command and entry kind, sets budgets."],
    ["fix", "Runs the Corrector (FixerPort.fix) — edits the working tree in place; returns a Patch or None."],
    ["validate", "Runs the reproduce command in the sandbox; extracts the post-fix ErrorSignature (None when green)."],
    ["immunize", "Crash-entry only: Tester writes a targeted test; gated on a green tree; smoke-test fallback."],
    ["report_commit", "Reporter writes the commit message; git commits fix+test; advances the chained-error loop."],
    ["rollback", "git reset --hard; records a FailedAttempt; increments the attempt counter."],
    ["post_mortem", "Reporter writes a Markdown narrative; persisted by the filesystem reporter."],
  ],
  [2100, 6926],
));
C(h2("3.3 Two entry modes"));
C(bullet([b("Crash entry: "), t("a production crash; the reproduce command is the crashing command (e.g. "),
  code("python main.py"), t("). The Tester writes a regression test (immunization).")]));
C(bullet([b("Test-failure entry: "), t("a test already failed; the reproduce command is that test node. "
  + "Immunization is skipped — a capturing test already exists.")]));
C(h2("3.4 Chained errors and the two loops"));
C(p([t("Errors are fingerprinted by "), code("ErrorSignature"), t(" keyed on "),
  code("(kind, exc_type, location)"), t(" — the message is excluded on purpose, so trivial wording "
  + "differences do not look like a new error. Two loops use it:")]));
C(bullet([b("Inner (retry): "), t("same signature still failing → rollback → fix, up to "), code("max_retries"), t(".")]));
C(bullet([b("Outer (chained): "), t("a "), b("different"), t(" signature surfaced → commit the progress, reset the "
  + "per-error scratch, and re-enter fix on the new error, up to "), code("error_cycle_budget"), t(".")]));
C(note("Observed in the wild.", "On the triple-syntax-error branch the Corrector once fixed the syntax but dropped an "
  + "unrelated method, surfacing a new AttributeError; the outer loop committed the syntax fix and healed the "
  + "AttributeError on a second cycle. (That code-dropping was later prevented — see §4.1.)"));
C(h2("3.5 The immunization gate"));
C(p("Immunization is targeted-first with a guaranteed floor:"));
C(numbered([t("The Tester writes a "), b("targeted"), t(" test and it is appended to the session file.")]));
C(numbered([t("On a green tree the test is "), b("gated"), t(": it is executed in the sandbox; it must pass.")]));
C(numbered([t("If it does not pass it is "), b("rolled back"), t(" and a deterministic "), b("smoke test"),
  t(" (re-run the reproduce command, assert exit 0) takes its place — so immunization never commits a test that "
  + "proves nothing, and never silently disappears.")]));

// ===== 4. Agents ============================================================
C(h1("4. The Agents"));
C(table(
  ["Agent", "Implementation", "Port", "Output"],
  [
    ["Corrector", "MiniSWEFixer (mini-swe-agent 2.2.8)", "FixerPort", "In-place edits + unified diff (Patch)"],
    ["Tester", "LLMTester (litellm)", "TesterPort", "A targeted pytest regression test"],
    ["Reporter", "LLMReporter (litellm)", "ReporterAgentPort", "Commit message + post-mortem prose"],
  ],
  [1400, 3300, 1800, 2526],
));
C(h2("4.1 Corrector — mini-swe-agent, original but tuned"));
C(note("Key clarification.", [t("The Corrector is "), b("mini-swe-agent 2.2.8 UNMODIFIED"),
  t(" (installed via pip; no fork, no patch). Its control loop, tool-call parsing and Docker execution are stock. "
  + "We only "), b("tune its behaviour through configuration and prompting"), t(", inside our "), code("MiniSWEFixer"),
  t(" adapter — not with its default config.")]));
C(p("What we tune (all in src/agents/swe_agent_dev.py):"));
C(bullet([b("Tool-calling-native prompt. "), t("mini-swe-agent's "), code("LitellmModel"),
  t(" always runs with a bash tool and requires a tool call back, but its stock prompt asks for a text block. "
  + "gpt-4.1-mini obeyed the prompt and thrashed on “No tool calls found”. We replace both templates with "
  + "our own tool-calling-native prompt.")]));
C(bullet([b("tool_choice=\"required\". "), t("Forces a bash tool call every turn, eliminating wasted “thinking” turns.")]));
C(bullet([b("Edit rules. "), t("“cat the whole file first; rewrite with a here-document, not "), code("sed"),
  t(", for indentation; never drop code you did not display.” This prevents the file-rewrite-drops-code failure.")]));
C(bullet([b("Context injection. "), t("We build the task with the failure output + the "), b("real source"),
  t(" of the files in the trace (focus-files, shared via "), code("src/agents/_focus.py"), t(") + previous attempts.")]));
C(bullet([b("Limits & capture. "), t("step/cost limits, trajectory JSON, diff capture, and cross-platform Docker cleanup.")]));
C(h2("4.2 Tester — targeted regression tests"));
C(p([t("The Tester ("), code("LLMTester"), t(") receives the failure, the "), b("diff of the fix"),
  t(", and the "), b("current (fixed) source"), t(" of the relevant files, and writes a test that calls the "
  + "specific function that was broken with inputs that exercise the bug, asserting the now-correct behaviour. "
  + "Because it sees the real source it uses the real API (no hallucinated class names).")]));
C(p("Example produced for the UnboundLocalError branch:"));
C(...codeBlock([
  "def test_cargar_datos_no_file_unboundlocal_error():",
  "    import os, tempfile",
  "    from almacenamiento import GestorBaseDatos",
  "    tmp = tempfile.TemporaryDirectory()",
  "    gestor = GestorBaseDatos(ruta_archivo=os.path.join(tmp.name, 'nope.json'))",
  "    datos = gestor.cargar_datos()        # used to raise UnboundLocalError",
  "    assert isinstance(datos, dict) and datos == {}",
]));
C(p("Anti-patterns are forbidden by the prompt: one test function, imports inside the function (it is appended to a "
  + "shared file), and never pytest.raises on the fixed behaviour. The gate + smoke fallback (§3.5) guarantee the floor."));
C(h2("4.3 Reporter — commit messages and post-mortems"));
C(p([t("The Reporter ("), code("LLMReporter"), t(") is the only narrative agent — no workspace, no sandbox. From "
  + "the diff and the error signature it writes a "), b("Conventional-Commits"), t(" message per fix; when the retry "
  + "budget is exhausted it writes a Markdown post-mortem. Example subjects produced in the benchmark:")]));
C(bullet([code("fix(almacenamiento): initialize datos_cargados to avoid UnboundLocalError")]));
C(bullet([code("fix(gestor_cuentas): fix NameError by correcting variable name in report generation")]));

C(h2("4.5 Prompt methodology: PICCO"));
C(p([t("All three agents' prompts follow the "), b("PICCO"), t(" structure — explicit "),
  b("Persona, Intention, Context, Conditions, Output"), t(" sections. The Corrector splits it across the "
  + "conversation the way chat prompts are layered: the system template carries Persona + Intention (and the "
  + "tool-call output channel), the instance template carries Context (the rendered task), Conditions and Output. "
  + "The Tester and the Reporter hold all five sections in their system prompts.")]));
C(p([t("Every battle-tested rule survives inside its PICCO section verbatim (tool-calling-native output, "
  + "heredoc-not-sed for indentation, never omitting unseen code on a rewrite, targeted tests against the real "
  + "API, Conventional-Commits-only output), and unit tests pin both the keywords and the five-section structure. "
  + "The restructure was validated with a full benchmark run: same 8/8 result at an equal-or-lower cost.")]));

// ===== 5. Observability =====================================================
C(h1("5. Observability & Telemetry"));
C(p([t("Telemetry is a cross-cutting concern implemented with the "), b("Decorator pattern"),
  t(" (the GoF object decorator — not a Python @method decorator, which would touch the business code). "
  + "Each port is wrapped by an "), code("Instrumented*"), t(" adapter that records a "), code("Span"),
  t(" around the call and delegates verbatim; each LangGraph node is wrapped the same way in "), code("build_graph"), t(".")]));
C(h2("5.1 What is captured"));
C(bullet([b("Per node: "), code("node.<name>"), t(" spans (bootstrap, fix, validate, immunize, report_commit, …).")]));
C(bullet([b("Per port: "), code("fixer.fix"), t(", "), code("tester.write_regression_test"), t(", "),
  code("sandbox.run_tests"), t(", "), code("git.commit"), t(", … — with outcome attributes (e.g. the sandbox verdict).")]));
C(bullet([b("Per LLM call: "), code("llm.completion"), t(" — token counts and cost, captured via a litellm callback.")]));
C(h2("5.2 Token & cost — total and per agent"));
C(p([t("The decorators cannot see inside an LLM call, so token/cost is hooked at litellm itself: a single "),
  code("CustomLogger"), t(" fires on every completion (including mini-swe-agent's internal calls). Two context "
  + "variables make it attributable without touching business code: "), code("use_agent"),
  t(" (set by the agent decorators → corrector / tester / reporter) and "), code("using_llm_sink"),
  t(" (set around the run). "), code("InMemoryTelemetry.aggregate()"), t(" then returns an "), code("llm"),
  t(" section with the "), b("total"), t(" and the "), b("per-agent"), t(" breakdown.")]));
C(p("Real aggregate from one benchmark branch (import-error):"));
C(...codeBlock([
  '"llm": {',
  '  "total":   { "calls": 21, "prompt_tokens": 79195, "completion_tokens": 773, "cost_usd": 0.013254 },',
  '  "by_agent": {',
  '    "corrector": { "calls": 19, "prompt_tokens": 77060, "cost_usd": 0.012016 },',
  '    "tester":    { "calls": 1,  "prompt_tokens": 1722,  "cost_usd": 0.001001 },',
  '    "reporter":  { "calls": 1,  "prompt_tokens": 413,   "cost_usd": 0.000237 }',
  '  }',
  '}',
]));
C(h2("5.3 Sinks"));
C(bullet([code("InMemoryTelemetry"), t(" — collects + aggregates (used by the benchmark and tests).")]));
C(bullet([code("JsonlTelemetry"), t(" — one JSON object per span to a file.")]));
C(bullet([code("MultiTelemetry"), t(" — fan-out; "), code("NullTelemetry"), t(" — no-op default (tests run un-instrumented).")]));

// ===== 6. Configuration =====================================================
C(h1("6. Configuration Reference"));
C(p([t("Configuration is a frozen "), code("Settings"), t(" object ("), code("src/config/settings.py"),
  t(") built from environment variables by "), code("load_settings()"), t(".")]));
C(h2("6.1 Environment variables"));
C(table(
  ["Variable", "Default", "Purpose"],
  [
    [[p([code("CDD_AGENT_MODE")])], "mock", "mock (in-memory fakes) | real (LLM agents + Docker)."],
    [[p([code("CDD_LLM_MODEL")])], "claude-sonnet-4-…", "litellm model string. Benchmark uses openai/gpt-4.1-mini."],
    [[p([code("CDD_MAX_RETRIES")])], "3", "Fix→Validate cycles per error before post-mortem."],
    [[p([code("CDD_ERROR_CYCLE_BUDGET")])], "5", "Max distinct chained errors healed per session."],
    [[p([code("CDD_SANDBOX_IMAGE")])], "self-healing-sandbox:latest", "Sandbox image (ships pytest)."],
    [[p([code("CDD_SANDBOX_TIMEOUT")])], "60", "Wall-clock timeout (s) for the sandboxed command."],
    [[p([code("CDD_SWEAGENT_USE_DOCKER")])], "true", "Run the Corrector in Docker (required on Windows)."],
    [[p([code("CDD_SWEAGENT_DOCKER_IMAGE")])], "python:3.12-slim", "Corrector container image."],
    [[p([code("CDD_SWEAGENT_STEP_LIMIT")])], "0 (∞)", "Max agent steps (≈20 bounds weak models)."],
    [[p([code("CDD_SWEAGENT_COST_LIMIT")])], "3.0", "Max USD per Corrector run."],
    [[p([code("CDD_SWEAGENT_TRAJECTORY_DIR")])], "(off)", "If set, saves each run's full trajectory JSON."],
    [[p([code("CDD_LOG_JSON")])], "true", "One-JSON-per-line logs (production) vs human logs."],
  ],
  [3050, 2100, 3876],
));
C(h2("6.2 Provider & external credentials (not CDD_)"));
C(bullet([code("OPENAI_API_KEY"), t(" / "), code("OPENAI_API_BASE"), t(" — for the Azure AI Foundry OpenAI-compatible "
  + "endpoint, used via litellm's "), code("openai/"), t(" provider (model "), code("openai/gpt-4.1-mini"), t(").")]));
C(bullet([code("GITHUB_TOKEN"), t(" — clones the target repo and, in the real-environment deployment, pushes the heal branch and opens the PR.")]));
C(bullet([code("MSWEA_COST_TRACKING=ignore_errors"), t(" — lets mini-swe-agent run when litellm lacks model pricing.")]));

// ===== 7. Benchmark & Evaluation ===========================================
C(h1("7. Benchmark & Evaluation"));
C(p([t("The benchmark ("), code("scripts/run_benchmark.py"), t(") drives the system against "),
  code("Arnaufafi/Benchmark_for_agents"), t(": it clones the repo, and for each "), code("bug*/*"),
  t(" branch it checks out, detects the crash, heals it, and commits the fix+test to a "), code("fix/<branch>"),
  t(" ref, then writes a JSON+Markdown report including the per-branch telemetry.")]));
C(h2("7.1 Results"));
C(p([t("Figures from the validation run of 2026-06-12 ("), code("benchmark_20260612_090229"),
  t("), executed with the final PICCO prompts. Zero retries and zero rollbacks across the run; all eight "
  + "regression tests written by the Tester were targeted (none fell back to the smoke test).")]));
C(table(
  ["Branch", "Resolved", "Duration", "LLM cost"],
  [
    ["bugA/identation-error", "✅", "32.6 s", "$0.0036"],
    ["bugA/import-error", "✅", "28.3 s", "$0.0060"],
    ["bugA/module-not-found", "✅", "26.3 s", "$0.0054"],
    ["bugA/syntax-error", "✅", "47.0 s", "$0.0088"],
    ["bugB/attribute-error", "✅", "32.3 s", "$0.0045"],
    ["bugB/name-error", "✅", "32.4 s", "$0.0043"],
    ["bugB/type-error", "✅", "34.1 s", "$0.0072"],
    ["bugB/unboundlocal-error", "✅", "24.9 s", "$0.0051"],
    ["bugC/index-error", "❌ (no repro)", "0.3 s", "—"],
  ],
  [3400, 2200, 1700, 1726],
));
C(p([b("8 / 8"), t(" reproducible crashes resolved; total "), b("$0.0449"),
  t(" in ~4.3 minutes. The single failure is a benchmark-scenario issue — that branch's "), code("python main.py"),
  t(" exits cleanly (rc=0), so there is no crash to reproduce; it is not a failure of the system.")]));
C(h2("7.2 Cost breakdown by agent"));
C(table(
  ["Agent", "Share of cost", "Why"],
  [
    ["Corrector", "~77% ($0.0344)", "Multi-step agentic loop; resends the growing trajectory each step (prompt tokens dominate)."],
    ["Tester", "~18% ($0.0079)", "One call, but with the source + diff in context."],
    ["Reporter", "~6% ($0.0026)", "One short call from the diff."],
  ],
  [1700, 2400, 4926],
));
C(note("Insight.", "Cost is driven by the Corrector's prompt tokens, not completions — mini-swe-agent re-sends the "
  + "whole trajectory on every step (prompt caching mitigates it). This motivates step limits and context management, "
  + "and is exactly the kind of finding the telemetry was built to surface."));
C(h2("7.3 Why not the full SWE-bench"));
C(p([t("SWE-bench is the industry standard, but it measures the "), b("underlying coding agent"),
  t(" — a known quantity (mini-swe-agent). It does not isolate this project's contribution (immunization, "
  + "chained-error handling, automatic documentation); in fact the test-failure entry mode "), b("skips the Tester"),
  t(" entirely. A custom benchmark that exercises all three agents is therefore a better instrument; a small SWE-bench "
  + "slice is reserved for stressing scale (large files/repos), not for a headline score.")]));

// ===== 8. Design Decisions ==================================================
C(h1("8. Design Decisions (ADRs)"));
const adr = (title, decision, rationale) => {
  C(h2(title));
  C(p([b("Decision.  "), t(decision)]));
  C(p([b("Rationale.  "), t(rationale)]));
};
adr("8.1 Fix-first (not test-first / CDD)",
  "Repair the error first, then immunise with a regression test, then commit.",
  "A real pipeline gives you a failure, not a spec. Writing the test first (and gating the fixer on a possibly-bad "
  + "reproducer) blocked progress; fix-first matches how an engineer actually triages a crash.");
adr("8.2 Hexagonal architecture",
  "Express needs as ports; implement them as swappable adapters; wire at the composition root.",
  "Gives model/tool independence, fully hermetic tests, and a clean boundary between the reused coding agent and "
  + "the orchestration that is the actual contribution.");
adr("8.3 Three agents, model-agnostic",
  "Corrector + Tester + Reporter, all behind ports and driven via litellm.",
  "Each maps to one fix-first responsibility (repair / immunise / document). Keeping them model-agnostic let us move "
  + "from a local 14B model to gpt-4.1-mini by changing one setting.");
adr("8.4 Reuse mini-swe-agent, tuned via the adapter",
  "Use mini-swe-agent unmodified; tune behaviour with our own prompt/config inside MiniSWEFixer.",
  "Reusing a validated agent avoids reinventing a control loop; keeping the tuning in the adapter means upstream "
  + "upgrades are a pip bump. The vanilla config thrashed with gpt-4.1-mini — the tuning is what made it work.");
adr("8.5 Targeted tests with a gate and a smoke fallback",
  "Prefer a targeted test (real API, from the diff+source); gate it on a green tree; fall back to a deterministic smoke test.",
  "A generic smoke test catches a re-crash but not a subtle reintroduction. Giving the Tester the real source removes "
  + "API hallucination; the gate guarantees the committed test actually passes; the fallback guarantees a floor.");
adr("8.6 Decorator-based telemetry + a litellm callback",
  "Wrap ports/nodes with object decorators; capture token/cost with a litellm callback tagged by contextvars.",
  "Observability is cross-cutting; the GoF object decorator keeps the business adapters free of telemetry code, and "
  + "the litellm hook captures even the Corrector's internal calls, attributed per agent.");

// ===== 9. Real-environment deployment ======================================
C(h1("9. The Real-Environment Deployment (GitHub PR-on-CI)"));
C(p([t("The real environment is "), b("GitHub PR-on-CI"), t(", and it is "), b("implemented and validated with "
  + "live pull requests"), t(": a failing CI test or a crashing entry point triggers a heal in the sandbox, and the "
  + "system pushes a "), code("selfheal/<id>"), t(" branch and "), b("opens a Pull Request"), t(" with the fix — "),
  b("never auto-merging"), t(". Human review is the safety gate.")]));
C(h2("9.1 Entry point and trigger modes"));
C(p([code("scripts/heal_and_pr.py"), t(" clones the target repo at a base branch, reproduces the failure, runs the "
  + "pipeline, and on success pushes + opens the PR via a dedicated "), code("GitHubPort"), t(" adapter "
  + "(GitHub REST API; the token is never logged and is scrubbed from push errors). Three trigger modes:")]));
C(bullet([b("crash-entry"), t(" (default): reproduce a crashing "), code("python main.py"), t(".")]));
C(bullet([b("test-entry"), t(" ("), code("--test <nodeid>"), t("): reproduce a failing pytest node — the CI signal. "
  + "The failing test IS the regression test, so immunization is skipped.")]));
C(bullet([b("auto"), t(" ("), code("--auto"), t("): probe tests first, then the crash; exit 0 when everything is "
  + "green. This is what the on-push workflow uses.")]));
C(h2("9.2 The GitHub Actions loop"));
C(bullet([b("On push (full loop): "), code("deploy/selfheal-on-push.yml"), t(" is a template installed in the "
  + "monitored repo. Every push — including a merge to main — probes the repo and, on a failure, heals and opens "
  + "the PR. Anti-loop guards: "), code("selfheal/**"), t(" pushes are ignored, and PRs created with the default "
  + "token do not re-trigger workflows.")]));
C(bullet([b("On demand: "), code(".github/workflows/self-heal.yml"), t(" ("), code("workflow_dispatch"),
  t(") heals any repository by name; an empty test input auto-detects.")]));
C(h2("9.3 Live results"));
C(p([t("Both modes were exercised against real GitHub pull requests on the benchmark repository:")]));
C(table(
  ["Live PR", "Scenario", "Outcome"],
  [
    ["PR #1 — latent bug", "main.py runs clean; a CI test exposes an unguarded FileNotFoundError that crash-entry cannot see",
     "Healed end-to-end in 27 s for ~$0.002; minimal fix (guard + return {}); immunization correctly skipped"],
    ["PR #2 — seeded bug", "A realistic CI test fails on the branch's seeded NameError (main.py also crashes; the probe chose tests-first)",
     "One-line fix for $0.0027; accurate Conventional-Commits message; the production crash healed as a side effect"],
  ],
  [1900, 3600, 3526],
));
C(note("Reading the PR.", "Each PR body states what was repaired (the reproduction command), the per-error "
  + "fingerprints, the regression tests, and the heal's total and per-agent cost — plus an explicit "
  + "'human review required, do not auto-merge' notice."));
C(h2("9.4 Guardrails"));
C(bullet([b("Isolation: "), t("edits and validation run in an ephemeral Docker sandbox.")]));
C(bullet([b("PR, not merge: "), t("the system proposes; a human approves. No write to protected branches.")]));
C(bullet([b("Bounded spend: "), t("cost/step limits per heal; telemetry surfaces the spend per PR.")]));
C(bullet([b("Do no harm: "), t("smallest change; inputs flow through env (no shell injection); the token never "
  + "reaches logs or error messages.")]));
C(h2("9.5 Remaining work"));
C(bullet([t("Premiere the on-push Action end-to-end (requires hosting this repo on GitHub and configuring secrets).")]));
C(bullet([t("Sandbox images with real-repo dependencies (today the sandbox ships only pytest; for test-entry the "
  + "Corrector's container must carry pytest too — solved by pointing "), code("CDD_SWEAGENT_DOCKER_IMAGE"),
  t(" at the sandbox image).")]));
C(bullet([t("Hygiene & robustness backlog (defect register in the working log): commit-content guard for "
  + "agent-created files, anti-spiral guard for chained errors.")]));

C(new Paragraph({ spacing: { before: 400 }, alignment: AlignmentType.CENTER,
  children: [new TextRun({ text: "— End of technical documentation —", italics: true, color: "777777" })] }));

// ============================================================================
//  DOCUMENT
// ============================================================================
const doc = new Document({
  creator: "Self-Healing System",
  title: "Self-Healing System — Technical Documentation",
  styles: {
    default: { document: { run: { font: "Arial", size: 21 } } },
    paragraphStyles: [
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 34, bold: true, font: "Arial", color: ACCENT },
        paragraph: { spacing: { before: 360, after: 200 }, outlineLevel: 0,
          border: { bottom: { style: BorderStyle.SINGLE, size: 6, color: ACCENT, space: 6 } } } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 27, bold: true, font: "Arial", color: "1F3864" },
        paragraph: { spacing: { before: 260, after: 120 }, outlineLevel: 1 } },
      { id: "Heading3", name: "Heading 3", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 23, bold: true, font: "Arial", color: "333333" },
        paragraph: { spacing: { before: 180, after: 80 }, outlineLevel: 2 } },
    ],
  },
  numbering: {
    config: [
      { reference: "bul", levels: [{ level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT,
        style: { paragraph: { indent: { left: 540, hanging: 280 } } } }] },
      { reference: "num", levels: [{ level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT,
        style: { paragraph: { indent: { left: 540, hanging: 280 } } } }] },
    ],
  },
  sections: [{
    properties: {
      page: {
        size: { width: 11906, height: 16838 },
        margin: { top: 1440, right: 1440, bottom: 1440, left: 1440 },
      },
    },
    footers: {
      default: new Footer({ children: [new Paragraph({
        alignment: AlignmentType.CENTER,
        border: { top: { style: BorderStyle.SINGLE, size: 4, color: "CCCCCC", space: 6 } },
        children: [
          new TextRun({ text: "Self-Healing System — Technical Documentation     ", size: 16, color: "888888" }),
          new TextRun({ children: ["Page ", PageNumber.CURRENT, " / ", PageNumber.TOTAL_PAGES], size: 16, color: "888888" }),
        ],
      })] }),
    },
    children: content,
  }],
});

Packer.toBuffer(doc).then((buf) => {
  const out = path.join(__dirname, "..", "SelfHealingSystem.docx");
  fs.writeFileSync(out, buf);
  console.log("Wrote", out, "(" + buf.length + " bytes)");
});
