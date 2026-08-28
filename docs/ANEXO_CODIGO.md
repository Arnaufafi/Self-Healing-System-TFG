# Anexo C — Recorrido del código, línea a línea

> Complemento de `MEMORIA_TFG.md`: aquella explica el *porqué* por conceptos; este anexo
> recorre **los 54 ficheros de `src/` y `scripts/` (6.258 LOC)** explicando *cómo* funciona
> cada línea con carga semántica. Convención: se citan los fragmentos y se explica línea a
> línea dentro de cada fragmento; las líneas mecánicas (imports estándar, logging
> boilerplate) se agrupan y se declaran como tales. Ningún fichero queda fuera: los
> `__init__.py` triviales se cubren en la tabla §0.2.

## 0. Mapa del repositorio

### 0.1 Ficheros con lógica (por capa, con LOC)

| Capa | Fichero | LOC | Papel |
|---|---|---|---|
| dominio | `core/domain/enums.py` | 48 | Vocabulario cerrado (StrEnum) |
| dominio | `core/domain/signature.py` | 278 | Huella del error + parsers |
| dominio | `core/domain/models.py` | 212 | Objetos de valor pydantic |
| dominio | `core/domain/state.py` | 94 | Estado LangGraph (TypedDict) |
| dominio | `core/domain/telemetry.py` | 50 | `Span` + `NullTelemetry` |
| puertos | `core/ports/agents.py` | 66 | `FixerPort`, `TesterPort` |
| puertos | `core/ports/infrastructure.py` | 80 | `SandboxPort`, `GitPort` |
| puertos | `core/ports/reporter_agent.py` | 64 | `ReporterAgentPort` |
| puertos | `core/ports/reporter.py` | 38 | `ReporterPort` (persistencia) |
| puertos | `core/ports/github.py` | 42 | `GitHubPort` |
| puertos | `core/ports/telemetry.py` | 27 | `TelemetryPort` |
| núcleo | `core/exceptions/__init__.py` | 62 | Jerarquía de excepciones |
| orquestación | `orchestrator/dependencies.py` | 56 | Contenedor DI |
| orquestación | `orchestrator/routers.py` | 103 | 4 aristas condicionales puras |
| orquestación | `orchestrator/nodes.py` | 631 | Los 7 nodos + 12 helpers |
| orquestación | `orchestrator/graph.py` | 168 | Ensamblaje del StateGraph |
| agentes | `agents/_llm.py` | 57 | Helper litellm compartido |
| agentes | `agents/_focus.py` | 121 | Selección de focus files |
| agentes | `agents/swe_agent_dev.py` | 634 | Corrector (mini-swe-agent) |
| agentes | `agents/llm_tester.py` | 168 | Tester dirigido |
| agentes | `agents/llm_reporter.py` | 162 | Reporter narrativo |
| agentes | `agents/mock_{fixer,tester,reporter_agent}.py` | 72/51/66 | Dobles deterministas |
| infraestructura | `infrastructure/docker_sandbox/docker_sandbox.py` | 170 | Sandbox endurecido |
| infraestructura | `infrastructure/docker_sandbox/in_memory_sandbox.py` | 75 | Sandbox guionizable |
| infraestructura | `infrastructure/git_ops/git_adapter.py` | 95 | Git real (GitPython) |
| infraestructura | `infrastructure/git_ops/in_memory_git.py` | 45 | Git contable |
| infraestructura | `infrastructure/github/github_adapter.py` | 160 | REST + push + PR |
| infraestructura | `infrastructure/persistence/filesystem_reporter.py` | 118 | Post-mortems Markdown |
| observabilidad | `observability/span.py` | 72 | Primitiva de medición |
| observabilidad | `observability/agents.py` | 106 | Decoradores de agentes |
| observabilidad | `observability/infrastructure.py` | 46 | Decoradores sandbox/git |
| observabilidad | `observability/sinks.py` | 139 | InMemory/Jsonl/Multi |
| observabilidad | `observability/llm.py` | 120 | Callback litellm + contextvars |
| observabilidad | `observability/wiring.py` | 42 | `instrument_dependencies` |
| config | `config/settings.py` | 183 | Settings + factoría env |
| config | `config/logging_config.py` | 81 | JSON logging |
| entrada | `src/main.py` | 244 | Composition root + demo |
| entrada | `scripts/run_benchmark.py` | 672 | Harness del benchmark |
| entrada | `scripts/heal_and_pr.py` | 342 | Despliegue PR-on-CI |

### 0.2 Los `__init__.py` (re-exports, sin lógica)

`core/domain/__init__` re-exporta el dominio completo (modelos + firma + Span/NullTelemetry)
para que los consumidores importen de un solo sitio; `core/ports/__init__` los 8 puertos;
`agents/__init__` los 3 agentes reales + 3 mocks; `infrastructure/*/__init__` el adapter
real + el in-memory de cada paquete; `observability/__init__` la superficie pública del
paquete (spans, sinks, instrument_node, decoradores) **sin** `wiring` — exportarlo crearía
el ciclo observability→orchestrator→observability, por eso el wiring se importa explícito
en los composition roots; `config/__init__` `Settings`/`load_settings`/`configure_logging`;
`orchestrator/__init__` `Dependencies`/`build_graph`. Los `__init__` de `src`, `core` e
`infrastructure` raíz son de 1 línea (marcadores de paquete).

---

## 1. `src/core/domain/` — el dominio puro

### 1.1 `enums.py` (48 LOC)

```python
from enum import StrEnum

class TriggerType(StrEnum):
    PRODUCTION_CRASH = "production_crash"
    TEST_FAILURE = "test_failure"
```

- `StrEnum` (py3.11): cada miembro **es** un `str` — serializa directo en JSON/pydantic.
  Se migró desde `(str, Enum)` tras verificar por grep que ningún consumidor usaba
  `str(enum)` (cuyo resultado cambia entre ambas formas); todos usan `.value`.
- `SandboxVerdict`: `PASSED / FAILED / TIMEOUT / INFRASTRUCTURE_ERROR` — los dos últimos
  separan "tu código falló" de "la infraestructura falló", distinción que el router usa
  para no quemar reintentos por culpa de Docker.
- `RoutingDecision`: `TO_FIX / TO_VALIDATION / TO_IMMUNIZE / TO_ROLLBACK / TO_POST_MORTEM /
  FINISH` — enum cerrado en vez de strings libres: las aristas muertas se detectan en
  revisión y los routers son exhaustivamente testeables.

### 1.2 `signature.py` (278 LOC) — la huella del error

**Cabecera (l. 1–32).** Docstring que formula la pregunta única del orquestador (¿mismo
error, error distinto, o verde?) y declara el módulo puro (sin I/O). `ErrorKind =
Literal["crash", "test"]` — el canal se propaga por tipo, no por bool.

**Regexes precompiladas (l. 34–62), una por arte de pesca:**

```python
_TRACE_FRAME_RE = re.compile(r'File "([^"]+)", line (\d+)(?:, in (\S+))?')
```
- Frame CPython. El grupo `, in func` es **opcional** a propósito: los frames de
  `SyntaxError`/`IndentationError` no lo llevan y aun así deben dar localización.

```python
_PYTEST_SUMMARY_RE = re.compile(
    r"^(?:FAILED|ERROR)\s+(?P<nodeid>\S+::\S+)"
    r"(?:\s+-\s+(?P<exc>[A-Za-z_][\w.]*)(?::\s*(?P<msg>.*))?)?", re.M)
```
- La línea-resumen de pytest. Tanto el `- Exc: msg` como sus partes son opcionales:
  pytest **trunca esta línea al ancho del terminal** (80 columnas sin TTY), y el sufijo
  puede llegar amputado (`- F...`).

```python
_PYTEST_ERROR_LINE_RE = re.compile(r"^E\s+(?P<exc>[A-Za-z_][\w.]*)(?::\s*(?P<msg>.*))?$", re.M)
```
- La línea `E   ExcType: msg` del cuerpo del fallo — **nunca trunca el nombre de la
  clase**; por eso el parser la prefiere (commit `65c2acd`).
- `_HEX_ADDR_RE`, `_LINE_NO_RE`, `_WS_RE`: los tres normalizadores de ruido volátil
  (direcciones `0x…`→`0xADDR`, `line 42`→`line N`, colapso de espacios).
- `_EXC_SUFFIXES = ("Error", "Exception", "Exit", "Interrupt", "Warning", "Iteration")`:
  compuerta heurística para que una palabra suelta al final de la salida no se confunda
  con una clase de excepción (acepta `StopIteration`, `SystemExit`, `KeyboardInterrupt`…).

**La clase (l. 65–105).**

```python
@property
def fingerprint(self) -> str:
    raw = f"{self.kind}|{self.exc_type}|{self.location}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
```
- Línea 1: concatena **solo** canal, tipo y localización — el mensaje queda fuera del hash
  adrede (su inclusión producía falsos "errores nuevos" entre host y sandbox; el docstring
  del property documenta el incidente). Línea 2: SHA-256 truncado a 16 hex — estable,
  corto para logs y cuerpos de PR.

```python
def matches(self, other: ErrorSignature | None) -> bool:
    return other is not None and self.fingerprint == other.fingerprint
```
- Acepta `None` y devuelve `False` — los routers comparan sin guardas previas.
- `__str__` → `"TypeError @ app.py:func [a1b2c3…]"`: el formato que viaja a commits,
  PRs y post-mortems.

**`from_crash_text` (l. 111–132).** Paso a paso: (1) texto vacío ⇒ `None` (nada que
caracterizar); (2) `_parse_exception_line` extrae tipo+mensaje de la última línea del
traceback; (3) `_innermost_workspace_frame` la localización; (4) **si no hay ni tipo ni
frame ⇒ `None`** — un mensaje de infraestructura del sandbox no debe fabricar una firma
espuria que el router confundiría con un error encadenado (el `None` colapsa en "no sé ⇒
reintenta"); (5) construye la firma con el mensaje normalizado.

**`from_pytest_output` (l. 135–182).** (1) Verde explícito: si el texto contiene `passed`
y no contiene `failed|error` ⇒ `None`. (2) Intenta la línea-resumen (nodeid + exc + msg).
(3) **Siempre** consulta después las líneas `E ` y, si existen, su tipo/mensaje **pisan**
al del resumen — es la defensa contra el truncado de 80 columnas. (4) Sin localización
aún: busca un `path::node` suelto y, en último recurso, un frame `--tb=short`.
(5) Sin tipo **y** sin localización ⇒ `None`.

**Internos (l. 194–276).**
- `_parse_exception_line`: recorre las líneas **de abajo arriba** saltando vacías, frames
  (`File "`), `Traceback` y líneas indentadas (código fuente citado); la primera que casa
  `Identificador[: mensaje]` y pasa la compuerta (`.` en el nombre o sufijo de
  `_EXC_SUFFIXES`) gana. Si ninguna pasa: tipo vacío + última línea como mensaje.
- `_innermost_workspace_frame`: `findall` de todos los frames; se queda con **el último
  frame que cae dentro del workspace** (el más profundo = donde se levantó la excepción);
  si ninguno es del workspace, degrada al basename del frame más profundo — mejor una
  localización gruesa-pero-estable que ninguna.
- `_normalize_message`: borra el root del workspace (en ambas formas de barra), sustituye
  hex y números de línea, colapsa espacios, `lower()`.
- `_resolved`/`_is_inside`/`_to_rel`: aritmética de rutas con `PurePath` sobre barras
  normalizadas — funciona igual en Windows y POSIX y no toca el disco.

### 1.3 `models.py` (212 LOC) — los objetos de valor

**Dos bases (l. 23–44), y la diferencia importa:**

```python
class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid",
                              str_strip_whitespace=True, validate_assignment=True)

class _CodeFrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
```
- `_FrozenModel`: inmutable (compartible entre tareas async sin carreras), campos extra
  prohibidos (typos de construcción = error inmediato), espacios recortados.
- `_CodeFrozenModel` **no recorta espacios**: la indentación inicial y los saltos finales
  son significativos en código fuente, hunks de diff y tracebacks — la usan
  `SourceExcerpt`, `FixContext` y `RegressionTest`. Un `str_strip_whitespace` aquí
  corrompería silenciosamente los payloads de los agentes.

**Modelos de entrada.** `CrashReport(incident_id, service_name, stack_trace, commit_sha,
captured_at=now(UTC))` y `FailingTest(node_id, source, last_failure_output)` — los dos
payloads posibles. `TriggerEvent` los envuelve como unión discriminada:

```python
def model_post_init(self, __context):
    if self.trigger_type is TriggerType.PRODUCTION_CRASH and self.crash_report is None:
        raise ValueError(...)
    if self.trigger_type is TriggerType.TEST_FAILURE and self.failing_test is None:
        raise ValueError(...)
```
- Hook de pydantic v2 post-construcción: imposible fabricar un trigger incoherente.

**Modelos de ciclo.** `Patch(diff_text, author_agent, created_at)` — el diff es auditoría
(contrato apply-in-place). `SandboxResult(verdict, exit_code|None, duration_seconds≥0,
logs_tail)` — `exit_code=None` reservado a errores de infraestructura.
`FailedAttempt(attempt_index, cycle_index, patch?, sandbox_result?, error_summary,
recorded_at)` — `cycle_index` ata cada intento a su error encadenado, lo que permite que
`_current_cycle_attempts` (§3.3) filtre el historial que ve el Corrector en el reintento.

**Modelos fix-first.** `SourceExcerpt(file_path, content)`. `FixContext` (§4.4 de la
memoria): nótese `reproduce_cmd: tuple[str, ...]` — tupla, no lista: hashable e inmutable,
coherente con el modelo frozen. `RegressionTest(path, node_id, source)`.
`ResolvedError(signature, commit_sha, test_path|None, resolved_at)` — el ledger de la PR.

### 1.4 `state.py` (94 LOC) — el estado del grafo

```python
class HealingState(TypedDict, total=False):
    trigger: Required[TriggerEvent]
    workspace_path: Required[str]
```
- `TypedDict` porque LangGraph introspecciona reducers vía `Annotated`; `total=False`
  porque cada nodo devuelve un **delta parcial** que LangGraph fusiona — de ahí la regla
  de lectura `state.get(...)` en todos los nodos. Solo `trigger` y `workspace_path` son
  `Required`: lo mínimo para construir el estado inicial.
- Tres capas comentadas en el propio fichero: sesión (set por bootstrap, estable),
  error-actual (se resetea al avanzar la cadena), artefactos por-iteración
  (`current_patch`, `current_sandbox_result`, `post_fix_signature`…).

```python
failed_attempts: Annotated[list[FailedAttempt], operator.add]
resolved_errors: Annotated[list[ResolvedError], operator.add]
logs: Annotated[list[str], operator.add]
```
- Los tres acumuladores: `Annotated[..., operator.add]` le dice a LangGraph que la
  actualización se **concatena** al valor previo en vez de sustituirlo. Un nodo devuelve
  `{"logs": ["una línea"]}` y el historial crece; nadie muta listas compartidas.

### 1.5 `telemetry.py` (50 LOC)

`Span`: dataclass-modelo con `name`, `duration_s`, `status` (`"ok"|"error"`),
`attributes: dict`, `error_type|None`, `timestamp`. Vive en el dominio (no en
observability) para que `NullTelemetry` pueda ser el default del contenedor sin invertir
la dirección de dependencia:

```python
class NullTelemetry:
    def record(self, span: Span) -> None:
        return None
```
- Null Object del catálogo GoF: encaja **estructuralmente** con `TelemetryPort` sin
  importarlo (el comentario del fichero lo explicita) — el dominio queda libre de
  dependencias y los tests corren sin instrumentar por defecto.

---

## 2. `src/core/ports/` y `src/core/exceptions/`

### 2.1 Por qué `typing.Protocol` + `@runtime_checkable`

Tipado **estructural**: un adapter conforma un puerto por firma, sin heredar ni
registrarse. `@runtime_checkable` permite `isinstance(x, FixerPort)` en tests. El coste de
los stubs `...` con docstring es deliberado: el puerto es el lugar donde se documenta el
**contrato** (qué lanza, qué garantiza), no la implementación.

### 2.2 Contratos esenciales, fichero a fichero

- **`agents.py`**: `FixerPort.fix(ctx) -> Patch | None` — documenta el apply-in-place
  ("by the time it returns, the edits are on disk") y que `None` y `FixGenerationError`
  colapsan ambos en rollback. `TesterPort.write_regression_test(ctx) -> RegressionTest` —
  "invoked *after* a green fix on the crash-entry path only".
- **`infrastructure.py`**: `SandboxPort.run_tests` enumera las obligaciones del
  implementador (red deshabilitada, límite de memoria, timeout que mata el contenedor) y
  la distinción crítica: *los fallos funcionales NO lanzan* — van codificados en el
  veredicto; solo la infraestructura lanza. `GitPort` expone exactamente dos operaciones
  (`commit`, `reset_hard`) — el docstring explica que no existe paso de aplicación.
- **`reporter_agent.py` vs `reporter.py`**: separación escritura/persistencia — el
  primero *redacta* (commit message, post-mortem), el segundo *almacena*
  (`write_post_mortem(...) -> path`). Dos responsabilidades, dos puertos.
- **`github.py`**: `push_branch(workspace_path, repo, branch, force=True)` y
  `open_pull_request(repo, base, head, title, body, draft=False) -> url`. El docstring
  del protocolo fija el invariante del sistema: **no hay operación de merge**.
- **`telemetry.py`**: `record(span)` con dos MUSTs — barato y que jamás lance.

### 2.3 `exceptions/__init__.py` (62 LOC)

Jerarquía plana bajo `SelfHealingError`: rama infraestructura
(`InfrastructureError` → `DockerSandboxError` → `SandboxTimeoutError`;
`GitOperationError`), rama agentes (`AgentError` → `FixGenerationError`,
`TestGenerationError`, `ReportGenerationError`) y rama orquestación
(`OrchestrationError`, `MaxRetriesExceededError`). La purga D17 eliminó tres huérfanas del
diseño test-first (`QAGenerationError`, `DevPatchGenerationError`, `MalformedDiffError`) —
la jerarquía actual solo contiene excepciones con consumidor o con papel de API declarada.

---

## 3. `src/orchestrator/` — la máquina de estados

### 3.1 `dependencies.py` (56 LOC)

```python
@dataclass(frozen=True, slots=True)
class Dependencies:
    settings: Settings
    fixer: FixerPort
    tester: TesterPort
    reporter_agent: ReporterAgentPort
    sandbox: SandboxPort
    git: GitPort
    reporter: ReporterPort
    telemetry: TelemetryPort = field(default_factory=NullTelemetry)
```
- `frozen=True`: mutar el wiring en caliente es error de runtime. `slots=True`: sin
  `__dict__` por instancia. Todos los campos tipados como **puertos** — el dataclass es la
  materialización del hexágono. `telemetry` con default `NullTelemetry`: instrumentar es
  *opt-in* del composition root (`instrument_dependencies` devuelve una **copia** con los
  puertos envueltos, vía `dataclasses.replace` — posible precisamente porque es frozen).

### 3.2 `routers.py` (103 LOC) — cuatro funciones puras

Cada router es `HealingState -> RoutingDecision`, sin efectos (solo logging), lo que los
hace testeables con un dict literal.

```python
def route_after_fix(state):
    patch = state.get("current_patch")
    return TO_VALIDATION if patch is not None else TO_ROLLBACK
```
- Bajo apply-in-place, "hay patch" significa "hay edición en disco que validar"; `None`
  significa que el Corrector no produjo nada → consumir reintento vía rollback.

```python
def route_after_validate(state):
    result = state.get("current_sandbox_result")
    if result is not None and result.verdict is SandboxVerdict.PASSED:
        return TO_IMMUNIZE
    post = state.get("post_fix_signature")
    current = state.get("current_error_signature")
    if post is not None and not post.matches(current):
        return TO_IMMUNIZE          # error distinto ⇒ progreso encadenado
    return TO_ROLLBACK              # mismo error / indeterminado ⇒ reintento
```
- Línea a línea: (1) verde explícito gana; (2) si hay firma post-fix y **no** casa con la
  actual, el error original murió y emergió otro — eso es progreso, no fracaso; (3) todo
  lo demás (misma firma, salida imparseable ⇒ `post=None`, error de infraestructura)
  colapsa conservadoramente en reintento. Obsérvese que `matches(None)` devuelve `False`:
  si la firma actual fuese `None` y la post-fix no, se interpretaría como cambio — un
  borde teórico cubierto porque `bootstrap` siempre intenta fijar firma inicial.

```python
def route_after_commit(state):
    return TO_FIX if state.get("should_continue") else FINISH
```
- Deliberadamente tonto: la decisión real (¿hay residual? ¿queda presupuesto?) la tomó
  `report_commit_node`, que es quien tiene el contexto; el router solo la sigue.

```python
def route_after_rollback(state):
    return TO_FIX if state.get("attempt_count", 0) < state.get("max_retries", 3) \
           else TO_POST_MORTEM
```
- El contador ya viene incrementado por `rollback_node`; la comparación estricta `<`
  garantiza exactamente `max_retries` ejecuciones del Corrector por error.

### 3.3 `nodes.py` (631 LOC) — los siete nodos

**Constantes de cabecera.** `_DEFAULT_TEST_COMMAND = ("python","-m","pytest","-x","--tb=short")`
(reproducción por defecto en test-entry); `_RETRY_TAIL_BUDGET = 1200` (bytes de cola de
fallo por intento previo que ve el Corrector — presupuesto anti-inflación de contexto).

**`bootstrap_node` — normalizar la entrada.** Paso a paso:
1. `entry_kind = "crash" if trigger.trigger_type is PRODUCTION_CRASH else "test"` — el
   canal queda en el estado como `Literal`.
2. Extrae `failure_output`/`incident_id` del payload presente (stack trace+incident en
   crash; last_failure_output+node_id en test).
3. `reproduce_cmd`: respeta el del llamador si vino en el estado (así `heal_and_pr` fija
   el nodo exacto de pytest); si no, deriva el default por canal.
4. `regression_test_path = f"tests/test_selfheal_{session_id}.py"` y — **solo en
   crash-entry** (guard D12b) — `_ensure_session_test_file` crea el esqueleto con su
   docstring. En test-entry el fichero no existe jamás (un esqueleto vacío llegó a PRs
   reales).
5. `initial_sig = parse_error(failure_output, entry_kind, workspace)` — la primera firma.
6. Devuelve el delta completo de sesión: presupuestos desde settings (con override por
   estado), contadores a cero, `is_resolved=False`, y copia el payload del trigger a
   campos de primer nivel (`crash_report`/`failing_test`) para acceso O(1) de los nodos.

**`fix_node`.** `_build_fix_context(state)` → `deps.fixer.fix(ctx)` con tres salidas
mapeadas a estado: excepción `FixGenerationError` ⇒ `{"current_patch": None, logs:[error]}`
(el log conserva el mensaje — llega al post-mortem); `None` ⇒ patch None; patch ⇒ se
guarda con su tamaño logueado. El router hace el resto.

**`validate_node`.** (1) Guard defensivo: sin `workspace_path` fabrica un
`INFRASTRUCTURE_ERROR` sintético en lugar de lanzar (el grafo sigue gobernado).
(2) `cmd = tuple(state.get("reproduce_cmd") or ()) or _DEFAULT_TEST_COMMAND` — el doble
`or` cubre tanto ausencia como tupla vacía. (3) Ejecuta el sandbox. (4) La línea clave:

```python
post_sig = None if result.verdict is PASSED \
           else parse_error(result.logs_tail, entry_kind, workspace)
```
- Verde ⇒ sin firma post-fix; rojo ⇒ se fingerprinta **la cola de logs del sandbox** con
  el parser del canal correspondiente. `post_fix_output` guarda esa cola: si hay
  rollback, el siguiente intento del Corrector verá el síntoma *más reciente*.

**`immunize_node` — dirigido, compuerta, respaldo.** Línea a línea:
1. `if state.get("entry_kind", "crash") != "crash": return {..., "immunize.skipped"}` —
   test-entry sale antes de tocar nada.
2. `tree_is_green = result is not None and result.verdict is PASSED` — decide si la
   compuerta es exigible (en un error encadenado el árbol sigue rojo y exigir verde sería
   imposible).
3. Intento dirigido: `deps.tester.write_regression_test(_build_fix_context(state))` —
   nótese que el contexto lleva `fix_diff` (el diff del Corrector) porque
   `_build_fix_context` lo extrae de `current_patch`. `_gate_and_keep(..., gate=tree_is_green)`.
4. `except TestGenerationError: warning` — el Tester puede fallar sin romper el nodo.
5. Respaldo: solo si `tree_is_green`, genera el smoke determinista
   (`_build_smoke_test_source`) y lo pasa por la misma compuerta con `gate=True` (sobre
   árbol verde pasa por construcción).
6. Si nada sobrevivió: `current_regression_test=None` + log `immunize.no_test` — la
   sesión sigue; inmunizar es deseable, no bloqueante.

**`_gate_and_keep` (el helper de la compuerta).** (1) `before = _read_session_text(...)`
— snapshot textual del fichero de sesión (o `None` si no existe). (2)
`node_id = _append_regression_test(...)` — añade la fuente y deriva el node id real
parseando con `ast` el nombre de la primera `def test_*` (`_first_test_func`; fallback
`test_regression` si el parse falla). (3) Si `gate`: ejecuta
`pytest <node_id> -x --tb=short` **en el sandbox** y, si no pasa,
`_restore_session_text(before)` — que reescribe el contenido previo o **borra** el fichero
si `before is None` (no deja ni el esqueleto de un intento fallido). (4) Devuelve el
`RegressionTest` consolidado o `None`.

**`_build_smoke_test_source` / `_smoke_invocation`.** Generación de código por plantilla
pura: el argv de reproducción se vuelca como literal Python con `repr(...)` por argumento
(inmune a inyección por comillas), y el intérprete (`python|python3|py`) se sustituye por
`sys.executable` — el test pasará en cualquier máquina con cualquier layout de Python.

**`report_commit_node`.** Secuencia: (1) reúne incidente, firma, diff, intentos previos
(`_describe_attempt_for_retry` sobre `_current_cycle_attempts` — **solo** los del ciclo
actual, filtrados por `cycle_index`) y la ruta del test; (2) Reporter con `except
Exception` amplio → `_fallback_commit_message` determinista (un LLM caído no bloquea el
commit); (3) el comentario NB del código avisa de un landmine real del stdlib: `extra=`
de logging no admite la clave `message` (atributo reservado de LogRecord) — por eso se
loguea como `commit_message`; (4) `deps.git.commit(...)` (que internamente hace
`git add -A`); (5) construye `ResolvedError` — si la firma fuese `None`, fabrica una
mínima con el `entry_kind` como `kind` (el ledger nunca queda hueco); (6) **el avance de
cadena**:

```python
residual = _residual_signature(state)
if residual is not None and (cycle_index + 1) < budget:
    update.update({"should_continue": True,
                   "current_error_signature": residual,
                   "current_failure_output": state.get("post_fix_output", ""),
                   "current_incident_id": f"{incident_id}-chain{cycle_index+1}",
                   "error_cycle_index": cycle_index + 1,
                   "attempt_count": 0,
                   "current_patch": None, "current_sandbox_result": None,
                   "current_regression_test": None,
                   "post_fix_signature": None, "post_fix_output": ""})
```
- `_residual_signature` devuelve la firma post-fix solo si existe y **no** casa con la
  recién resuelta (si casa, reclamar progreso sería mentirse — devuelve `None` y la sesión
  termina como éxito parcial). El bloque resetea **todo el scratch por-error** y renombra
  el incidente con sufijo `-chainN`; los acumuladores (con reducer) sobreviven intactos.

**`rollback_node`.** (1) `deps.git.reset_hard(workspace)` con `except Exception:
log.exception` — un fallo de git no debe impedir registrar el intento; (2) construye el
`FailedAttempt` con `_summarise_failure` (tres casos: "corrector produced no fix" /
"sandbox verdict=X (exit_code=Y)" / "unknown failure"); (3) la línea sutil:

```python
freshest = state.get("post_fix_output") or state.get("current_failure_output", "")
return {..., "current_failure_output": freshest, "attempt_count": attempt_index + 1, ...}
```
- El **síntoma más fresco** pasa a ser el fallo actual: si la edición fallida cambió el
  error (sin cambiar la firma), el Corrector del reintento diagnostica sobre lo último
  observado, no sobre el traceback original. El resto del delta limpia los artefactos de
  la iteración.

**`post_mortem_node`.** Reporter (best-effort, `""` si falla) → `FilesystemReporter`
persiste el Markdown completo → `is_resolved=False` + ruta del informe en el estado.

**Helpers de E/S del fichero de sesión** (`_ensure_session_test_file`,
`_read_session_text`, `_restore_session_text`, `_append_regression_test`): todos
best-effort con `except OSError: warning` — la inmunización degrada, nunca derriba la
sesión. `_append_regression_test` calcula el separador (`"\n\n"` salvo que ya exista o el
fichero esté vacío) y **crea el fichero si no existe** (cubre el caso de un `git clean
-fd` previo que lo barrió).

### 3.4 `graph.py` (168 LOC) — el ensamblaje

1. **Nombres canónicos** como constantes (`NODE_FIX = "fix"`, …) exportadas — los tests
   referencian nodos sin strings mágicos.
2. `checkpointer = checkpointer or MemorySaver()` — inyectable; el default en memoria
   sirve para demo/tests y el docstring avisa de que producción debe inyectar backend
   persistente.
3. **Binding por `functools.partial`**: cada nodo se liga a `deps` una sola vez
   (`partial(fix_node, deps=deps)`) — los nodos son funciones libres testeables y el grafo
   no conoce el contenedor.
4. **Instrumentación en el registro**: `builder.add_node(name, instrument_node(name, fn,
   deps.telemetry))` — cada invocación de nodo emite un span `node.<name>` sin que el nodo
   lo sepa.
5. **Topología**: `START→bootstrap→fix` incondicional (fix-first: ambos canales van
   directos al Corrector); cuatro `add_conditional_edges` mapeando los valores del enum a
   nombres de nodo (fix→{validate,rollback}, validate→{immunize,rollback},
   report_commit→{fix,END}, rollback→{fix,post_mortem}); `immunize→report_commit` y
   `post_mortem→END` incondicionales.
6. `builder.compile(checkpointer=checkpointer)` devuelve el grafo ejecutable
   (`ainvoke`/`astream` con `configurable.thread_id`).

---

## 4. `src/agents/` — los tres agentes y sus auxiliares

### 4.1 `_llm.py` (57 LOC) — una llamada, un contrato

```python
async def acomplete(*, model, messages, temperature, timeout_seconds) -> str:
    try:
        import litellm                       # diferido: solo en modo real
    except ImportError as exc:
        raise RuntimeError("litellm is not installed...") from exc
    try:
        response = await asyncio.wait_for(
            litellm.acompletion(model=model, messages=messages,
                                temperature=temperature, drop_params=True),
            timeout=timeout_seconds)
    except TimeoutError as exc:
        raise RuntimeError(f"LLM completion timed out after {timeout_seconds}s") from exc
    except Exception as exc:
        raise RuntimeError(f"LLM completion failed: {exc!s}") from exc
    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, KeyError) as exc:
        raise RuntimeError(f"LLM returned an unexpected response shape...") from exc
    return content or ""
```
Línea a línea: import diferido (el paquete importa sin litellm instalado, requisito del
modo mock); `drop_params=True` — litellm descarta silenciosamente parámetros que el
modelo destino no acepta (los modelos razonadores rechazan `temperature≠1`), así la misma
llamada sirve para OpenAI/Azure/local; `wait_for` impone el timeout de pared; **todos los
fallos se normalizan a `RuntimeError`** — un único tipo que cada consumidor decide cómo
tratar (el Tester lo eleva a error de dominio, el Reporter lo traga); extracción defensiva
de la forma de respuesta; `content or ""` cubre el `None` que algunos proveedores
devuelven con tool-calls.

### 4.2 `_focus.py` (121 LOC) — qué ficheros ve un agente

**`collect_focus_paths(workspace, failure_output, node_id, budget=3)`**, paso a paso:
1. Resuelve la raíz y la ruta absoluta del test reproductor (si `node_id`) — para
   **excluirlo**: ningún agente debe leer ni editar el test que define el éxito.
2. Pesca frames con dos regex (traceback CPython y `--tb=short` de pytest).
3. **Pista ModuleNotFound**: de `No module named 'X'` deriva tres candidatos
   (`x/y.py`, `x/y/__init__.py`, `x.py` del primer segmento) — el fallo más engañoso del
   repertorio: la traza no contiene ninguna ruta literal del fichero culpable.
4. Ordena: `module_hints + reversed(frames)` — **el frame más profundo primero** (donde se
   levantó la excepción), con las pistas de módulo por delante de todo.
5. Filtro por candidato: resolución absoluta (relativa a la raíz si hace falta),
   `relative_to(root)` (fuera del workspace ⇒ descartado), exclusión del test, existencia
   real (`is_file()`), de-duplicación, y corte en `budget` (3).

**`render_focus_block(...)`** convierte esas rutas en el bloque Markdown
`## Project context (read-only)` con cada fichero en un fence ` ```python `, truncado a
8 KiB con marcador `# ... (truncated)`. El parámetro `intro` permite que cada agente
enmarque el bloque a su manera: el Corrector lo presenta como "código a modificar", el
Tester como "fuente de verdad de la API real" — el mismo contenido, intención opuesta.

### 4.3 `swe_agent_dev.py` (634 LOC) — el Corrector

**Preámbulo.** `os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")` **antes** de importar
nada de mini-swe-agent: su banner Rich con emojis revienta en terminales cp1252.
`_TRAJECTORY_TAIL_BYTES = 2048` acota el diagnóstico "submitted sin cambios".

**Las plantillas (§7 de la memoria).** `_SYSTEM_TEMPLATE` (PICCO: Persona+Intención+canal
de salida) y `_INSTANCE_TEMPLATE` (Contexto `{{task}}` + Condiciones + Output). El
comentario de 14 líneas que las precede documenta el porqué completo del desbloqueo
(`LitellmModel` exige tool_calls; el config stock pide bloque de texto; gpt-4.1-mini
obedecía y era rechazado cada turno). `_MODEL_KWARGS = {"tool_choice": "required"}` viaja
directo a `litellm.completion` — la imposibilidad estructural de un turno sin acción.

**`fix(context)` — el método del puerto.** (1) `_build_task(context)`; (2)
`_run_agent(task, incident)` → cola de trayectoria; (3) `_capture_diff()`; (4) **si el
diff está vacío ⇒ `FixGenerationError` con la cola de trayectoria embebida** — "el agente
submitió sin editar nada" deja de ser un misterio: el post-mortem muestra qué creyó estar
haciendo; (5) `Patch(diff_text, author_agent="MiniSWEFixer")`.

**`_build_task(context)` — el Contexto PICCO.** Compone, en orden: la frase del problema
(nodo pytest que falla, o comando que crashea); el bloque `## Failure output` con la
salida literal; el bloque de focus files; `## Success criterion` (exit 0 del comando de
éxito + "smallest change" + prohibición de tocar tests); la orden anti-submit-prematuro
("Reading files is not a fix"); y `## Previous failed attempts` con las colas
presupuestadas de cada intento anterior — la memoria del bucle de reintentos.

**`_run_agent` — la ejecución.** Dentro de un closure `_blocking` que corre en
`asyncio.to_thread` (mini-swe-agent es síncrono):
- `litellm.drop_params = True` global (cubre las llamadas internas del agente).
- `LitellmModel(model_name, model_kwargs=_MODEL_KWARGS)`.
- **Entorno Docker** (default): `_to_docker_volume_path` convierte la ruta Windows
  (`G:\x\y` → `/g/x/y` — la forma que Docker Desktop acepta en ambos backends);
  `run_args = ["--rm", "-v", f"{path}:/workspace"]` (+ `--add-host
  host.docker.internal:host-gateway` solo-Windows, residuo Ollama, registrado D19);
  `DockerEnvironment(image, cwd="/workspace", timeout=120)` — 120 s por **comando**, capa
  distinta del timeout de pared del run completo.
- `DefaultAgent(model, env, system_template=…, instance_template=…, cost_limit,
  step_limit, output_path=traj_path)` y `agent.run(task)` — `output_path` activa la
  persistencia de la trayectoria JSON.
- `finally` interno: `env.cleanup()` de la librería (capa 1, falla silenciosa en Windows
  — D14) con `except Exception: debug`.
- `exit_status == "error"` ⇒ `FixGenerationError` con el resultado completo.
- Fuera del closure: `asyncio.wait_for(to_thread(_blocking), timeout)` ⇒ el timeout de
  pared se convierte en `FixGenerationError`; y el `finally` externo ejecuta la **capa 2**
  de limpieza: `_kill_new_containers(existing_ids)` — diferencia entre los contenedores
  `minisweagent-*` listados antes y después del run y mata los nuevos (`docker stop` +
  `docker rm -f`, ambos con timeout y `except Exception: debug`). Esta doble capa es la
  razón de que D14 sea cosmético.
- `_format_trajectory_tail(messages)`: renderiza los mensajes **de más nuevo a más
  viejo** descontando un presupuesto de 2 KiB — así el recorte sacrifica los turnos
  antiguos y el turno final (la decisión de submit) sobrevive siempre; luego re-invierte
  para lectura cronológica.

**`_capture_diff`.** `git add -N .` (intent-to-add: los ficheros nuevos del agente
aparecen en `git diff` como vacío→contenido) seguido de `git diff` — ambos con
`cwd=self._workspace` explícito y vía `asyncio.create_subprocess_exec` (sin shell). El
`add -N` es best-effort (`communicate()` sin chequear rc); el `diff` sí valida rc y eleva
`FixGenerationError` con el stderr.

### 4.4 `llm_tester.py` (168 LOC) — el Tester

- `_TESTER_INTRO`: el intro alternativo del focus block — "the CURRENT (already-fixed)
  contents… source of truth for the real API" (la inversión de intención respecto al
  Corrector).
- `_SYSTEM_PROMPT` (PICCO completo): la Intención define el contrato FAIL_TO_PASS
  ("passes on the fixed code and would have FAILED before"); las Condiciones codifican las
  cuatro reglas duras (imports dentro de la función — el fichero de sesión es compartido y
  un import roto a nivel de módulo mataría a *todos* los tests; nombres EXACTOS del
  fuente; `pytest.raises` prohibido sobre el comportamiento reparado; setup mínimo); el
  Output exige exactamente una `def test_<name>():` sin prosa ni fences.
- `write_regression_test`: system + `_render_user(context)` → `acomplete` →
  `_extract_test_source(raw)`; `RuntimeError` se eleva a `TestGenerationError` (dominio) y
  una respuesta sin `def test_*` también — con los primeros 512 bytes crudos en el mensaje
  para el diagnóstico.
- `_render_user`: incidente + encargo + `## Failure that was fixed` (fence) +
  `## Diff of the fix` (fence diff, **solo si** `fix_diff` no está en blanco) + focus
  block con el intro del Tester + el comando de reproducción.
- `_extract_test_source`: dos regex — si hay fence ` ```python ` toma su cuerpo; localiza
  la primera `def test_*(` multilínea y corta desde ahí; normaliza el final a un único
  `\n`. Devuelve `None` si no hay test (el llamador decide).
- Devuelve `RegressionTest(path="", node_id="", source=…)` — el comentario lo declara:
  ambos campos son consultivos; `immunize_node` los pisa con la ruta de sesión y el nombre
  real parseado por `ast`.

### 4.5 `llm_reporter.py` (162 LOC) — el Reporter

- Dos system prompts PICCO (`_COMMIT_SYSTEM`, `_POSTMORTEM_SYSTEM`).
- `compose_commit_message`: mensaje de usuario con incidente, firma, nº de intentos
  previos, test añadido (condicional) y el diff **truncado a 6.000 caracteres** (un diff
  monstruoso no debe inflar el coste de una llamada narrativa); `except RuntimeError` ⇒
  `_fallback_commit(...)` determinista; y el matiz final — `message.strip() or
  _fallback_commit(...)`: una respuesta vacía también cae al fallback. **Nunca lanza.**
- `compose_post_mortem`: resume cada `FailedAttempt` en una línea
  (`- attempt N: summary`), añade trigger y contadores; `except RuntimeError` ⇒ `""` —
  el post-mortem estructurado se persiste igualmente sin narrativa.
- `_fallback_commit`: `fix(self-healing): resolve <firma>` + cuerpo de una línea — válido
  como Conventional Commit, reconocible como fallback.

### 4.6 Los mocks (`mock_fixer.py` 72, `mock_tester.py` 51, `mock_reporter_agent.py` 66)

- `MockFixer`: devuelve siempre el mismo diff unificado sintácticamente válido;
  `fail_on_attempts=(0,)` permite guionizar "falla el intento 0" — la demo lo usa para
  ejercitar el camino de rollback. El índice de intento se deriva de
  `len(context.previous_attempts)`: el mock *lee el contrato*, no un contador propio.
- `MockTester`: nombra la función con un hash del fallo (`test_selfheal_<sha8>`) — dos
  errores distintos en una misma sesión generan tests con nombres distintos, como el real.
- `MockReporterAgent`: mensajes plantilla deterministas (mismo formato Conventional
  Commits), latencia sintética opcional — gemelo de los otros dos.

---

## 5. `src/infrastructure/` — los adapters reales

### 5.1 `docker_sandbox/docker_sandbox.py` (170 LOC)

**Constantes de seguridad inmutables** (el llamador no puede relajarlas):
`_SECURITY_OPTS = ("no-new-privileges:true",)`, `_CAP_DROP = ("ALL",)`,
`_LOG_TAIL_BYTES = 8*1024`.

**`run_tests` (la cara async).** Envuelve `_run_blocking` en
`asyncio.wait_for(to_thread(...), timeout=settings.sandbox_timeout_seconds)` y mapea cada
desenlace a un `SandboxResult` — **nunca a una excepción funcional**:
- `TimeoutError` ⇒ veredicto `TIMEOUT` con la duración medida (el comentario lo subraya:
  "timeout is a recoverable verdict" — el router lo tratará como reintento, no como
  catástrofe).
- `DockerSandboxError` ⇒ veredicto `INFRASTRUCTURE_ERROR` con el mensaje como logs_tail.
- Éxito ⇒ veredicto por exit code.

**`_run_blocking` (la cara síncrona), línea a línea:**

```python
container = client.containers.run(
    image=image, command=list(command), detach=True,
    network_disabled=True,
    mem_limit=self._settings.sandbox_mem_limit,
    nano_cpus=self._settings.sandbox_cpu_quota * 10_000,
    read_only=False,
    cap_drop=list(_CAP_DROP), security_opt=list(_SECURITY_OPTS),
    pids_limit=256,
    volumes={workspace_path: {"bind": "/workspace", "mode": "rw"}},
    working_dir="/workspace", auto_remove=False)
```
- `detach=True`: el contenedor arranca y el control vuelve — el timeout se gestiona fuera.
- `network_disabled=True`: el código bajo prueba no tiene red. Punto.
- `nano_cpus = quota * 10_000`: la cuota viene en µs por periodo de 100 ms (semántica
  `--cpu-quota`), es decir `quota/100_000` CPUs; nano_cpus pide CPUs×1e9 ⇒ ×10.000. El
  comentario del código preserva la historia del defecto D15 (×1.000 daba 0,05 CPU).
- `read_only=False` con el porqué anotado: pytest escribe `.pyc`; el endurecimiento futuro
  es tmpfs, no romper pytest.
- `pids_limit=256`: una fork bomb muere en la cuna. `auto_remove=False`: los logs deben
  poder leerse **después** de `wait()` (con auto_remove el contenedor puede evaporarse
  antes de `logs()`).
- Después: `wait(timeout=...)` → exit code (default defensivo 1), `logs(tail=200)`
  recortados a 8 KiB y decodificados con `errors="replace"`, veredicto =
  `PASSED if exit_code == 0 else FAILED`.
- `finally: container.remove(force=True)` con `except` de limpieza best-effort — pase lo
  que pase, el contenedor no sobrevive.

### 5.2 `docker_sandbox/in_memory_sandbox.py` (75 LOC)

Doble guionizable con **dos colas**: `scripted_results` (objetos `SandboxResult`
completos — para tests que necesitan controlar `logs_tail` y forzar una firma encadenada
concreta) tiene precedencia sobre `scripted_verdicts` (solo veredictos); agotadas ambas,
`default_verdict` para siempre. El `logs_tail` sintético
`"[in-memory sandbox] verdict=…"` es deliberadamente **no parseable** como traceback —
`from_crash_text` devuelve `None` con él, ejercitando el camino "no sé caracterizarlo".

### 5.3 `git_ops/git_adapter.py` (95 LOC)

- GitPython importado en diferido (`_ensure_git`) con error claro si falta.
- `commit`: `repo.git.add(A=True)` (todo: tracked + untracked no ignorados — la razón de
  que existan los excludes del clone y el escudo D16) → `index.commit(message,
  author=actor, committer=actor)` con la identidad de settings → SHA. Todo dentro de
  `asyncio.to_thread` y con cualquier excepción envuelta en `GitOperationError`.
- `reset_hard`: `repo.git.reset("--hard")` + `repo.git.clean("-fd")` — la pareja completa:
  reset restaura los tracked, **clean elimina los untracked** que el intento fallido creó
  (un módulo nuevo erróneo del Corrector no contamina el siguiente intento). `-fd` sin
  `-x`: los *ignorados* (la BD de runtime excluida) sobreviven — borrarlos cambiaría el
  comportamiento de la app bajo prueba entre intentos.

### 5.4 `git_ops/in_memory_git.py` (45 LOC)

Contable puro: `commits: list[str]` (guarda los **mensajes** — los tests de integración
asertan sobre su contenido), `reset_count`, SHAs sintéticos secuenciales
(`sha_00000000`). `await asyncio.sleep(0)` mantiene la semántica async real (cede el
control una vez, como haría I/O).

### 5.5 `github/github_adapter.py` (160 LOC)

- `_default_http_post(url, payload, headers) -> (status, dict)`: transporte por defecto
  con `urllib.request` (cero dependencias); captura `HTTPError` y devuelve también su
  cuerpo parseado — un 422 trae el mensaje de GitHub, no una excepción opaca. El
  constructor acepta `http_post=` inyectable: los tests capturan la petición exacta sin
  red.
- `push_branch`: construye
  `https://x-access-token:<token>@github.com/<repo>.git` y ejecuta
  `git -C <ws> push <remote> HEAD:refs/heads/<branch> [--force]` vía
  `create_subprocess_exec`. La línea de seguridad:

```python
msg = (err or b"").decode("utf-8", errors="replace").replace(self._token, "***")
raise GitHubError(f"git push failed (rc={proc.returncode}): {msg[:500]}")
```
  — git imprime la URL del remote (con token) en sus errores; **el token se enmascara
  antes de que el mensaje exista**. `HEAD:refs/heads/<branch>` publica el commit actual
  con el nombre dado sin depender del estado de ramas local; `--force` es seguro porque
  las ramas `selfheal/<uuid>` son efímeras y propias.
- `open_pull_request`: POST a `/repos/{repo}/pulls` con los cuatro headers canónicos
  (Bearer, `application/vnd.github+json`, versión de API pinneada `2022-11-28`,
  User-Agent) y payload `{title, head, base, body, draft}`; el transporte corre en
  `asyncio.to_thread`; 201 ⇒ `html_url`; otro código ⇒ `GitHubError` con el `message` de
  GitHub. **No existe método de merge.**
- `build_pr_body(...)`: compone el Markdown del cuerpo — título "🤖 Automated fix", la
  frase de qué se reparó (el reproduce_cmd), el aviso en negrita de revisión humana, la
  lista de commits (subjects), los errores sanados con `ErrorSignature.__str__` (tipo @
  localización [fingerprint]) y sus tests, y el bloque de coste: total + por agente,
  formateado desde el agregado de telemetría (robusto a `telemetry=None`: el bloque se
  omite).

### 5.6 `persistence/filesystem_reporter.py` (118 LOC)

- Constructor: `reports_dir.mkdir(parents=True, exist_ok=True)` — el directorio existe
  desde el wiring.
- `write_post_mortem`: sanea el incident_id a fragmento de fichero seguro
  (`_sanitise_filename`: alfanumérico/`-`/`_`, cap 120) y escribe con
  `asyncio.to_thread(path.write_text, content, "utf-8")`.
- `_render_markdown`: narrativa primero (si existe), metadatos del trigger, y por intento:
  índice, timestamp, resumen, veredicto/exit del sandbox y dos `<details>` plegables (cola
  de logs y diff del parche). Los dos comentarios sobre `rstrip("\n")` son oro forense:
  un `rstrip()` a pelo se comería el espacio inicial de las líneas de contexto en blanco
  de un diff unificado (` \n`), haciendo que hunks válidos de 7 líneas parezcan corruptos
  de 6 — el tipo de detalle que solo se aprende depurando un post-mortem real.

---

## 6. `src/observability/` — medir sin tocar

### 6.1 `span.py` (72 LOC) — la primitiva

```python
@asynccontextmanager
async def span(sink, name, **attributes):
    attrs = dict(attributes); start = time.perf_counter()
    status, error_type = "ok", None
    try:
        yield attrs
    except BaseException as exc:
        status, error_type = "error", type(exc).__name__
        raise
    finally:
        try:
            sink.record(Span(name=name, duration_s=time.perf_counter()-start,
                             status=status, attributes=attrs,
                             error_type=error_type, timestamp=time.time()))
        except Exception:
            pass
```
- Cede el dict **mutable** — el llamador enriquece el span con el resultado
  (`attrs["verdict"] = ...`) antes de que el `finally` lo registre.
- `except BaseException` (no `Exception`): una `CancelledError` también debe quedar
  registrada como error antes de re-lanzarse.
- `perf_counter` para duración (monotónico), `time.time()` para timestamp (época).
- El `try/except` del `finally`: **un sink roto jamás rompe el negocio** — el contrato
  del puerto, aplicado.
- `instrument_node(name, node, sink)`: el wrapper de nodos — `async with span(sink,
  f"node.{name}")` alrededor del nodo ya ligado a deps; reenvía `*args/**kwargs` tal cual
  (LangGraph pasa estado y, a veces, config).

### 6.2 `agents.py` (106) e `infrastructure.py` (46) — los decoradores

Cinco clases con el mismo esqueleto de tres líneas por método: guardar `inner`+`sink`;
en cada método del puerto, `with use_agent("<rol>")` (solo agentes) + `async with
span(sink, "<puerto>.<método>", ...)` + delegación textual. Enriquecimientos puntuales:
`InstrumentedFixer` anota `produced_patch` (bool); `InstrumentedSandbox` anota el
`verdict` del resultado. `InstrumentedGit` cubre `commit` y `reset_hard`. Cada wrapper
**es** el puerto (hereda del Protocol): el grafo no distingue instrumentado de desnudo.

### 6.3 `sinks.py` (139 LOC)

- `InMemoryTelemetry`: lista protegida por `threading.Lock` (el callback de litellm puede
  registrar desde otro hilo); `spans` (property) devuelve una **copia** bajo lock.
  `aggregate()` produce el resumen en un solo paseo: por nombre de span
  `{count, errors, total_s, max_s, avg_s}` y, para los spans `llm.completion`, el doble
  bucle `for tgt in (llm_total, per_agent)` acumula llamadas/tokens/coste en el total y en
  el bucket del agente simultáneamente; redondeos al final (coste a 6 decimales). La forma
  del resultado (`by_name` + `llm.total` + `llm.by_agent`) es exactamente lo que consumen
  el informe del benchmark y `build_pr_body`.
- `JsonlTelemetry`: una línea JSON por span, append, `default=str` para atributos no
  serializables, y **todo** envuelto en try/except a debug — best-effort radical.
- `MultiTelemetry`: fan-out con `except Exception: pass` por hijo — un sink caído no
  arrastra a los demás.

### 6.4 `llm.py` (120 LOC) — el callback (§8.2 de la memoria)

- Dos `ContextVar` módulo-globales: `_current_agent` (default `"unknown"`) y
  `_current_sink` (default `None`). `use_agent`/`using_llm_sink`: contextmanagers de
  set/reset por token — anidables y seguros en concurrencia.
- `_record(kwargs, response_obj, start, end)`: sale gratis si no hay sink activo; extrae
  `usage.prompt_tokens/completion_tokens` con `getattr` defensivo; coste:
  `kwargs["response_cost"]` (litellm lo precalcula) con fallback a
  `litellm.completion_cost(...)` y a `0.0` — **tokens siempre, coste best-effort**;
  duración de los datetimes del callback; emite el span con
  `agent=_current_agent.get()`. Todo el cuerpo en try/except a debug.
- `ensure_registered()`: define la subclase `CustomLogger` una sola vez (singleton de
  módulo), implementa los hooks síncrono y asíncrono delegando en `_record`, y la añade a
  `litellm.callbacks` **solo si no está** — idempotente: `instrument_dependencies` puede
  llamarse N veces sin duplicar spans.

### 6.5 `wiring.py` (42 LOC)

`instrument_dependencies(deps, sink)`: `ensure_registered()` +
`dataclasses.replace(deps, fixer=InstrumentedFixer(deps.fixer, sink), …,
telemetry=sink)` — una llamada en el composition root instrumenta los cinco puertos; los
nodos los instrumenta `build_graph` leyendo `deps.telemetry`. El docstring del módulo
explica por qué vive fuera del `__init__`: importa `Dependencies` del orquestador y
re-exportarlo cerraría un ciclo de imports.

---

## 7. `src/config/`

### 7.1 `settings.py` (183 LOC)

`Settings(BaseModel)` con `frozen=True, extra="forbid"`: la configuración es un valor,
no un saco mutable. Cada campo lleva `Field(default, restricciones, description)` — las
restricciones validan en frontera (`max_retries: ge=1 le=20`, `sandbox_cpu_quota: gt=0`,
timeouts `gt=0`) y las descripciones son la referencia de configuración. Grupos: modo,
política de reintentos/cadena, sandbox (imagen, memoria, CPU, timeout), identidad git,
informes, logging, y el bloque del Corrector (`sweagent_*`) + `tester_timeout_seconds`.
`load_settings()`: factoría explícita — un walrus-if por variable de entorno
(`CDD_*`) con coerción manual (`int()`, `float()`, set de strings truthy para bools) y
`Settings(**overrides)`. Sin `pydantic-settings` a propósito: la lectura del entorno es
visible, testeable y no-mágica.

### 7.2 `logging_config.py` (81 LOC)

`JsonFormatter.format`: payload base (`ts` ISO-UTC, `level`, `logger`, `message`),
`exc_info` formateado si existe, y el barrido de extras: todo atributo del record que no
esté en `_RESERVED_RECORD_KEYS` (la lista completa de slots del stdlib) ni empiece por
`_` se intenta serializar a JSON y, si no puede, se guarda como `repr` — los `extra={...}`
de todo el proyecto aparecen como claves de primer nivel sin colisionar con el stdlib.
`configure_logging(level, json_mode)`: **purga los handlers previos** del root antes de
añadir el suyo (idempotencia — tests y entry points la llaman repetidas veces sin duplicar
líneas), y elige JsonFormatter o el formato humano `%(asctime)s [%(levelname)s] %(name)s
:: %(message)s`.

---

## 8. Los puntos de entrada

### 8.1 `src/main.py` (244 LOC) — composition root + demo

Cuatro constructores simétricos (`_build_mock_agents` / `_build_real_agents` /
`_build_mock_infra` / `_build_real_infra`) seleccionados por `agent_mode`:
- Los **mock** vienen guionizados para que la demo ejercite los caminos interesantes:
  `MockFixer(fail_on_attempts=(0,))` fuerza un rollback en el primer intento;
  `InMemorySandbox(scripted_verdicts=(FAILED, PASSED))` hace fallar la primera validación
  y pasar la segunda — la demo offline recorre fix→rollback→fix→validate→immunize→commit
  en milisegundos.
- Los **real** se importan en diferido dentro de la función (el proyecto importa sin
  mini-swe-agent/litellm instalados) y mapean settings→constructores; el `LLMTester`
  recibe `tester_timeout_seconds`.
- `_build_dependencies` ensambla el contenedor, lo loguea (tipos concretos elegidos —
  trazabilidad del wiring) y lo pasa por `instrument_dependencies(deps,
  InMemoryTelemetry())`.
- `run_demo`: settings→logging→deps→grafo→estado inicial (un `CrashReport` sintético con
  un TypeError de juguete)→`ainvoke` dentro de `using_llm_sink`. `main()` ejecuta la demo
  en un `tempfile.TemporaryDirectory` — la inmunización escribe su fichero de sesión en un
  workspace desechable, jamás en el repo del sistema.

### 8.2 `scripts/run_benchmark.py` (672 LOC) — el harness

**Helpers git (§1–2 del fichero).**
- `_git(*args, cwd)`: subprocess con timeout 120 s, rc≠0 ⇒ `RuntimeError` con stderr.
  Matiz documentado en D16: hace `stdout.strip()` — correcto para SHAs y nombres, pero un
  consumidor de salidas posicionales (porcelain) perdería el primer carácter; por eso el
  escudo del deploy usa `diff --name-only`.
- `_git_skip_worktree(repo_dir, rel_path, skip)`: `update-index --[no-]skip-worktree` con
  `except RuntimeError: pass` — en ramas donde el fichero no está trackeado es un no-op.
- `_purge_readonly_tree(path)`: `shutil.rmtree` con hook `onerror` que limpia el bit
  read-only y reintenta — git marca `objects/pack` de solo-lectura en Windows y un rmtree
  ingenuo deja árboles a medio borrar que rompen el siguiente clone.
- `clone_repo(token, repo, dest)`: clone `--no-single-branch --depth=1` (todas las ramas,
  historia mínima) con el token embebido en la URL, y **escribe
  `.git/info/exclude`** con los artefactos de runtime (`datos_bancarios.json`,
  `__pycache__/`, `*.pyc`) — el gitignore local-al-clone que mantiene los heal commits
  limpios sin tocar el repo remoto.
- `checkout_branch(repo_dir, branch)`: la coreografía de aislamiento en 4 pasos —
  (0) quitar el skip-worktree de la rama anterior (un fichero skip-worktree es invisible
  para reset y quedaría con el contenido de la rama previa); (1) `reset --hard` (limpia
  también el index manchado por el `add -N` del fixer y el `add -A` del commit);
  (2) `clean -fdx` (TODO lo untracked, ignorados incluidos — borrón total entre ramas);
  (3) checkout; (4) re-marcar skip-worktree sobre la BD si la rama la trackea.
- `detect_crash(workspace)`: ejecuta `python main.py` (timeout 60 s); **rc=0 ⇒
  RuntimeError "expected a crash"** (un escenario sano no es un escenario); stderr con
  fallback a stdout y a un mensaje sintético si ambos vacíos.
- `build_crash_report`/`build_trigger`: empaquetado en dominio — incident_id
  `bench-<rama-saneada>-<uuid6>`, stack trace recortado a los últimos 4 KB.

**`build_real_deps(settings, workspace)`.** El wiring real del benchmark: `MiniSWEFixer` +
`LLMTester` + `LLMReporter` + `DockerSandbox` + `GitAdapter` + `FilesystemReporter`, con
overrides de entorno `BENCHMARK_USE_DOCKER`/`BENCHMARK_DOCKER_IMAGE` (compatibilidad) que
caen a los `CDD_SWEAGENT_*` canónicos; remata con `instrument_dependencies(deps,
InMemoryTelemetry())` — cada rama estrena sink.

**`run_branch(branch, ...)`.** El ciclo por rama: checkout prístino → detect_crash →
trigger → estado inicial → deps reales → grafo → `ainvoke` dentro de
`using_llm_sink(deps.telemetry)` → `await asyncio.sleep(0.2)` (el flush del callback
detached — D4) → vuelca `resolved`, `attempts` y `telemetry=aggregate()` al dict de
resultado → si resolvió, `push_and_pr` (best-effort; D18 documenta que su mitad
`gh pr create` exige el CLI y duplica al GitHubAdapter — candidata a consolidación).
Todo el cuerpo bajo un `except Exception` que convierte cualquier fallo en una fila de
informe con `error` en vez de abortar el run completo.

**`write_report(results, out_dir)`.** JSON íntegro (con la telemetría por rama,
`default=str` para los tipos no nativos) + Markdown con la tabla
rama/resuelto/intentos/duración/PR/error y el total.

**`async_main`.** Token obligatorio → wipe verificado del workspace (si tras el purge el
directorio sigue existiendo, aborta con mensaje accionable: "cierra el proceso que
retiene ficheros") → clone → `list_remote_branches` (filtra `SKIP_BRANCHES` y
deduplica) → `cleanup_all_containers()` preventivo → bucle por rama con doble protección:
`KeyboardInterrupt/CancelledError` marca la fila como interrumpida y **rompe el bucle
conservando los resultados parciales**, y entre ramas se ejecuta la limpieza de
contenedores de nuevo → informe SIEMPRE (parcial o completo) → `main()` traduce Ctrl+C a
exit 130 con limpieza final.

### 8.3 `scripts/heal_and_pr.py` (342 LOC) — el despliegue

- **Bootstrap de imports**: inserta la raíz del proyecto en `sys.path` y reutiliza por
  import directo los helpers del benchmark (`clone_repo`, `detect_crash`,
  `build_crash_report`, `build_trigger`, `build_real_deps`, `_git`,
  `_purge_readonly_tree`) — el despliegue no duplica una línea de wiring.
- `detect_failing_test(repo_dir, node_id) -> (source, output)`: comprueba que el fichero
  del nodo existe (`FileNotFoundError` si no), ejecuta **ese nodo** con pytest (timeout
  120 s) y — la inversión especular de `detect_crash` — **rc=0 ⇒ RuntimeError "expected a
  failing test"**; devuelve la fuente literal del fichero y la salida del fallo.
- `build_test_trigger`: `FailingTest(node_id, source, output[-4096:])` envuelto en
  `TEST_FAILURE`.
- `shield_runtime_artifacts(repo_dir)` (D16): `git diff --name-only` (un path limpio por
  línea: tracked modificados y borrados; los untracked no aparecen — de esos se ocupan
  los excludes del clone) y por cada path `git checkout -- <p>` (restaurar el seed) +
  `git update-index --skip-worktree <p>` (protegerlo de los `git add` futuros); imprime
  el resumen `[i] Shielded N...` si hubo algo.
- `detect_any_failure(repo_dir, base)` — la sonda del modo `--auto`, en orden:
  (1) `pytest --tb=short -q` sobre el repo; rc∉{0,5} (5 = sin tests) ⇒ busca el primer
  `FAILED|ERROR <nodo>` con `_FAILED_NODE_RE`, lo re-ejecuta aislado vía
  `detect_failing_test` — y si **pasa en aislamiento** (flaky/orden) cae al paso
  siguiente en vez de perseguir un fantasma; devuelve el trigger de test **y** el
  `reproduce_cmd` clavado a ese nodo. (2) Si existe `main.py`: `detect_crash`; limpio ⇒
  `None`. (3) `None` = nada que sanar.
- `heal_and_pr(repo, base, *, open_pr, draft, test_node, auto)` — la secuencia completa:
  token obligatorio (rc 1) → wipe + clone + checkout base → rama `selfheal/<uuid8>` →
  sonda según modo (en auto, `None` ⇒ imprime `[OK] Nothing to heal` y **rc 0**: verde es
  éxito de CI) → **escudo D16** → estado+deps+grafo → `ainvoke` bajo `using_llm_sink` +
  flush 0,2 s → si no resolvió: `[FAIL]` + rc 2 (el post-mortem ya quedó en reports/) →
  subjects de los commits (`git log base..rama --format=%s`) → con `--no-pr`: resumen
  local y rc 0 → `push_branch` + `build_pr_body` (telemetría agregada incluida) +
  `open_pull_request` → `[OK] Opened PR: <url>`. Prints en ASCII puro (D2: emojis
  reventaban en cp1252).
- `main()`: argparse con `--repo`/`--base` obligatorios y el **grupo mutuamente
  excluyente** `--auto | --test NODEID` — los tres modos sin ambigüedad posible.

### 8.4 Los workflows YAML

- **`deploy/selfheal-on-push.yml`** (plantilla para el repo vigilado): `on: push` +
  `branches-ignore: ["selfheal/**"]` (anti-bucle nivel 1; el nivel 2 es que las PRs del
  token por defecto no re-disparan workflows); `concurrency:
  selfheal-${{ github.ref }}` sin cancelación (un push nuevo espera, no mata el heal en
  curso); `permissions: contents: write, pull-requests: write` (el `GITHUB_TOKEN` efímero
  del job basta intra-repo — sin PATs); pasos: checkout del repo del sistema (con
  `HEALER_REPO_TOKEN || github.token` para repos privados/públicos), Python 3.12, deps,
  `docker build` de la imagen sandbox, e invocación con los valores de contexto pasados
  **por `env`** (`IN_REPO`, `IN_BASE`) y nunca interpolados en el shell — la defensa
  contra inyección por nombre de rama. La cabecera comenta el alta única (secretos LLM,
  ajuste de organización para crear PRs, limitación de dependencias).
- **`.github/workflows/self-heal.yml`** (dispatch): inputs `repo/base/test/draft`; la
  línea de despacho elige `--test "$IN_TEST"` si vino nodo y `--auto` si no.

---

## 9. Cierre: cómo se sostiene todo

La suite (115 tests) refleja este anexo sección a sección: el dominio con tests de
firma/modelos, los routers con dicts literales, el grafo con los in-memory guionizados
(incluida la cadena de dos errores y el camino de presupuesto agotado), los prompts con
tests de contrato (palabras clave + estructura PICCO), el sandbox con un cliente Docker
falso (baseline de seguridad + aritmética de CPU), el escudo D16 contra un repo git real
temporal, la sonda auto con pytest real sobre `tmp_path`, y el adapter de GitHub con
transporte HTTP capturado. La regla transversal que este recorrido ha hecho visible:
**cada `except` del sistema decide conscientemente entre degradar (telemetría, Reporter,
ficheros de sesión, limpiezas) y fallar alto (sonda, git del commit, transporte de la
PR)** — y esa decisión está escrita al lado del código que la toma.
