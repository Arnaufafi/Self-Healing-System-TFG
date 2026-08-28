# Sistema Multiagente de Auto-Sanación de Código (Self-Healing System)

> **Memoria técnica del Trabajo de Fin de Grado** — borrador completo para volcar en la
> plantilla institucional. Autor: Arnau Fabregas Figueras. Anexo técnico en inglés:
> `docs/SelfHealingSystem.docx`. Código: este repositorio (rama `master`, 115 tests).

---

## Índice

1. [Introducción: el problema y la tesis](#1-introducción-el-problema-y-la-tesis)
2. [Contexto tecnológico y decisión de alcance](#2-contexto-tecnológico-y-decisión-de-alcance)
3. [Arquitectura: hexagonal estricta](#3-arquitectura-hexagonal-estricta)
4. [El dominio: modelar el error](#4-el-dominio-modelar-el-error)
5. [El pipeline fix-first, paso a paso](#5-el-pipeline-fix-first-paso-a-paso)
6. [Los tres agentes](#6-los-tres-agentes)
7. [Ingeniería de prompts: metodología PICCO](#7-ingeniería-de-prompts-metodología-picco)
8. [Observabilidad: el patrón decorator y el coste por agente](#8-observabilidad-el-patrón-decorator-y-el-coste-por-agente)
9. [Seguridad y guardarraíles](#9-seguridad-y-guardarraíles)
10. [El benchmark propio](#10-el-benchmark-propio)
11. [El despliegue real: GitHub PR-on-CI](#11-el-despliegue-real-github-pr-on-ci)
12. [Verificación y calidad](#12-verificación-y-calidad)
13. [El registro de defectos como metodología](#13-el-registro-de-defectos-como-metodología)
14. [Resultados, limitaciones y conclusiones](#14-resultados-limitaciones-y-conclusiones)
- [Apéndice A: referencia de configuración](#apéndice-a-referencia-de-configuración)
- [Apéndice B: cronología de commits clave](#apéndice-b-cronología-de-commits-clave)

---

## 1. Introducción: el problema y la tesis

### 1.1 El problema

En una pipeline de despliegue moderna, un error llega por dos canales: un **crash en
ejecución** (el proceso revienta con un traceback) o un **test de CI que falla**. En ambos
casos el flujo se detiene hasta que un ingeniero (1) reproduce el fallo, (2) diagnostica la
causa, (3) edita el código, (4) verifica que el síntoma desaparece, (5) escribe un test de
regresión para que el bug no vuelva, y (6) documenta el cambio en un commit revisable. Los
pasos 1–6 son mecánicos en una fracción significativa de los incidentes reales (errores de
nombre, de tipo, de indentación, imports rotos, variables no inicializadas), pero consumen
tiempo de ingeniería y, sobre todo, **latencia de recuperación**.

### 1.2 La tesis

Este TFG construye y valida un sistema que ejecuta los seis pasos de forma autónoma con un
equipo de **tres agentes LLM especializados** orquestados por una máquina de estados,
bajo dos invariantes de seguridad innegociables:

1. **Todo código no confiable se ejecuta en un sandbox Docker** (la reproducción, la
   reparación y la validación del test).
2. **El sistema jamás fusiona sus cambios**: el resultado es una *pull request* en GitHub;
   la revisión humana es la única puerta de merge.

La validación es doble: un **benchmark propio** de 9 escenarios de error sembrados
(8/8 reproducibles resueltos, ~0,045 USD por ejecución completa) y un **despliegue real**
que abrió dos pull requests verificables en GitHub, una de ellas reparando un bug latente
que el canal de crash era estructuralmente incapaz de ver (§11.5).

### 1.3 Contribuciones

La contribución **no** es el agente de codificación (se reutiliza `mini-swe-agent`, §6.1),
sino el sistema alrededor:

- Una **arquitectura hexagonal** que hace al sistema agnóstico de modelo y de herramienta
  (§3), demostrada cambiando de un modelo local de 14B parámetros a `gpt-4.1-mini` de Azure
  modificando una variable de entorno.
- El **bucle de errores encadenados** sobre una huella estable del error (`ErrorSignature`,
  §4.3): si reparar el error A destapa un error B distinto, A se consolida en su propio
  commit y el bucle reentra para B.
- La **inmunización con compuerta**: un test de regresión *dirigido* generado por un agente
  que ve el fallo, el diff de la reparación y el código fuente real — y que solo se
  consolida si pasa en el sandbox (§5.6).
- **Telemetría transversal sin tocar el código de negocio** mediante decoradores objeto
  (GoF) y un callback de litellm que atribuye tokens y coste por agente (§8).
- El **cierre del bucle en GitHub**: sonda de detección automática, rama `selfheal/*`,
  PR con coste desglosado y aviso de no-merge, y plantillas de GitHub Actions con
  protección anti-bucle (§11).

---

## 2. Contexto tecnológico y decisión de alcance

### 2.1 Piezas reutilizadas

| Pieza | Versión | Papel | Por qué |
|---|---|---|---|
| [LangGraph](https://langchain-ai.github.io/langgraph/) | 0.x | Máquina de estados del pipeline | Grafo declarativo con checkpointing y aristas condicionales puras |
| [mini-swe-agent](https://github.com/SWE-agent/mini-SWE-agent) | 2.2.8, **sin modificar** | Motor del agente Corrector | Bucle agente↔shell validado por la comunidad (~100 líneas de núcleo); reinventarlo no aporta a la tesis |
| [litellm](https://github.com/BerriAI/litellm) | 1.x | Capa de acceso a LLMs | API única sobre OpenAI/Azure/Anthropic/Ollama: el sistema es agnóstico de proveedor |
| [pydantic](https://docs.pydantic.dev/) v2 | 2.x | Modelos de dominio inmutables | Validación en frontera, `frozen=True` |
| docker-py / GitPython | — | Adapters de infraestructura | Wrappers asíncronos vía `asyncio.to_thread` |

### 2.2 Por qué NO se evalúa con SWE-bench completo

SWE-bench es el estándar de la industria para agentes de codificación, pero **mide el
componente equivocado para esta tesis**: puntúa al agente de parcheo (mini-swe-agent, una
cantidad conocida con resultados publicados) y no a la contribución de este trabajo
(inmunización, encadenamiento, reporting, despliegue). Peor aún: SWE-bench es
*test-to-pass* — en nuestro pipeline ese modo de entrada **salta al agente Tester por
diseño** (§5.6), así que un run de SWE-bench ejercitaría un tercio del sistema. La decisión
(registrada como ADR en el anexo) fue un **benchmark propio que ejercita los tres agentes y
el bucle de cadena** (§10), reservando una rebanada de SWE-bench como prueba de escala
futura, no como cifra de cabecera.

### 2.3 Modelos utilizados

El desarrollo arrancó con `ollama/qwen2.5-coder:14b` en local (gratis, lento, frágil) y la
validación final se hizo con `openai/gpt-4.1-mini` servido por Azure AI Foundry a través de
su endpoint compatible OpenAI (`OPENAI_API_BASE` + `OPENAI_API_KEY`, proveedor `openai/` de
litellm). El cambio de modelo no tocó una sola línea de código: `CDD_LLM_MODEL`.

---

## 3. Arquitectura: hexagonal estricta

### 3.1 Capas y regla de dependencia

```
src/
├── core/                  # NÚCLEO PURO — sin I/O, sin frameworks
│   ├── domain/            #   modelos, HealingState, ErrorSignature, Span, enums
│   ├── ports/             #   contratos typing.Protocol (8 puertos)
│   └── exceptions/        #   jerarquía SelfHealingError
├── agents/                # ADAPTERS de agentes (mini-swe-agent, litellm) + mocks
├── infrastructure/        # ADAPTERS de infraestructura (Docker, Git, GitHub REST, FS)
├── observability/         # decoradores de instrumentación, sinks, callback litellm
├── orchestrator/          # LangGraph: nodos, routers, contenedor Dependencies
└── config/                # Settings por variables de entorno + logging estructurado
```

La regla de dependencia tiene una formulación operativa única: **el orquestador no importa
jamás un adapter concreto**. Los nodos del grafo reciben un contenedor `Dependencies`
(dataclass con `fixer`, `tester`, `reporter_agent`, `sandbox`, `git`, `reporter`,
`telemetry`) cuyos campos están tipados como `Protocol`. La composición ocurre
exclusivamente en los *composition roots*: `src/main.py` (demo), `scripts/run_benchmark.py`
(benchmark) y `scripts/heal_and_pr.py` (despliegue).

### 3.2 Los puertos

| Puerto | Contrato esencial | Adapter real | Adapter de test |
|---|---|---|---|
| `FixerPort` | `fix(FixContext) -> Patch \| None` — edita el árbol **in place**; el Patch es el diff de auditoría, nunca se re-aplica | `MiniSWEFixer` | `MockFixer` |
| `TesterPort` | `write_regression_test(FixContext) -> RegressionTest` — devuelve fuente, no toca disco | `LLMTester` | `MockTester` |
| `ReporterAgentPort` | `compose_commit_message(...)`, `compose_post_mortem(...)` | `LLMReporter` | `MockReporterAgent` |
| `SandboxPort` | `run_tests(workspace, image, cmd) -> SandboxResult` | `DockerSandbox` | `InMemorySandbox` (veredictos guionizados) |
| `GitPort` | `commit(...) -> sha`, `reset_hard(...)` | `GitAdapter` (GitPython) | `InMemoryGit` |
| `GitHubPort` | `push_branch(...)`, `open_pull_request(...) -> url` — **nunca fusiona** | `GitHubAdapter` (REST) | transporte HTTP inyectable |
| `ReporterPort` | persistencia de post-mortems | `FilesystemReporter` | (el real es inocuo) |
| `TelemetryPort` | `record(Span)` — barato, jamás lanza | sinks de §8 | `NullTelemetry` |

Tres consecuencias medibles de esta disciplina: (1) la **suite completa de integración es
hermética** — el grafo entero corre con mocks en milisegundos, sin Docker ni red ni LLM;
(2) el **cambio de modelo/proveedor** es una variable de entorno; (3) la telemetría se
añadió *después* sin modificar ningún adapter de negocio (§8).

### 3.3 El contrato apply-in-place

Decisión estructural temprana: el Corrector **no devuelve un parche para aplicar** —
edita los ficheros directamente y el `Patch` retornado es el `git diff` capturado *a
posteriori* para auditoría, mensaje de commit y post-mortem. Esto elimina toda una clase de
fallos (parches malformados, conflictos de aplicación — la excepción `MalformedDiffError`
del diseño original murió sin un solo uso) y alinea el sistema con cómo trabaja realmente
mini-swe-agent. La contrapartida es que el **rollback se vuelve responsabilidad del
orquestador**: `git reset --hard` + `git clean -fd` (§5.7).

---

## 4. El dominio: modelar el error

### 4.1 El disparador (`TriggerEvent`)

Unión discriminada validada por pydantic: `trigger_type ∈ {PRODUCTION_CRASH,
TEST_FAILURE}` con exactamente un payload coherente — `CrashReport` (incident_id, servicio,
stack trace crudo, SHA desplegado) o `FailingTest` (node id de pytest, fuente del test,
salida del último fallo). `model_post_init` rechaza construcciones incoherentes; los enums
son `StrEnum` (migrados de `(str, Enum)` tras verificar que ningún consumidor dependía de
`str(enum)` — todos usan `.value`).

### 4.2 El estado del grafo (`HealingState`)

`TypedDict` de LangGraph con dos clases de campos:

- **Acumuladores con reducer** (`Annotated[list[X], operator.add]`): `failed_attempts`,
  `resolved_errors`, `logs` — los nodos devuelven deltas y LangGraph concatena.
- **Campos de scratch por error**: `current_error_signature`, `current_failure_output`,
  `current_patch`, `current_sandbox_result`, `post_fix_signature`, `post_fix_output`,
  `attempt_count` — se **resetean** al avanzar a un error encadenado (§5.5), mientras que
  `error_cycle_index` y los acumuladores sobreviven a toda la sesión.

Esta separación scratch/acumulador es lo que permite que un solo `ainvoke` del grafo sane
N errores distintos produciendo N commits con N tests.

### 4.3 La huella del error (`ErrorSignature`) — la pieza que gobierna el bucle

```python
fingerprint = sha256(f"{kind}|{exc_type}|{location}")[:16]
```

- `kind ∈ {crash, test}` — el canal.
- `exc_type` — la clase de excepción (`NameError`, `FileNotFoundError`, …).
- `location` — el frame más interno **dentro del workspace** (crash) o el node id de
  pytest (test).
- `normalized_msg` — el mensaje con rutas absolutas, direcciones hex, números de línea y
  temporales normalizados; se conserva para el humano pero **se excluye deliberadamente del
  fingerprint**.

La exclusión del mensaje es una cicatriz de guerra, no una elegancia: en los primeros runs
el mismo bug reportaba mensajes ligeramente distintos entre el host y el sandbox (rutas,
desplazamientos de línea, re-redacción del siguiente error de indentación por el parser de
CPython), el fingerprint cambiaba, el router lo interpretaba como "error distinto ⇒
progreso", y el sistema **consolidaba medio-arreglos y avanzaba** en lugar de reintentar.
`(kind, exc_type, location)` resultó ser la señal robusta de "el error anterior de verdad
ha desaparecido".

Los parsers (`from_crash_text`, `from_pytest_output`) son tolerantes por diseño y derivan
la firma de texto crudo de terminal. Un detalle encontrado en la validación real
(commit `65c2acd`): pytest sin TTY trunca su línea de resumen a 80 columnas, convirtiendo
`FileNotFoundError` en `F...`; el parser prefiere desde entonces la línea `E ExcType:` —
que nunca trunca el nombre de la clase — sobre la línea `FAILED ... - Exc`.

### 4.4 Objetos de valor del flujo

- `FixContext` — la entrada uniforme de Corrector y Tester: `incident_id`,
  `failure_output`, `reproduce_cmd`, `reproducer_node_id` (solo test-entry),
  `previous_attempts` (colas de los intentos fallidos del ciclo actual, presupuestadas a
  1.200 bytes por intento) y `fix_diff` (vacía para el Corrector; el diff de la reparación
  para el Tester).
- `Patch` — diff unificado + agente autor + timestamp. Auditoría, no aplicación.
- `RegressionTest` — ruta, node id y fuente del test de inmunización.
- `ResolvedError` — `(signature, commit_sha, test_path)` acumulado por error sanado; es la
  materia prima del cuerpo de la PR.
- `FailedAttempt` — índice de intento, índice de ciclo, patch, resultado del sandbox y
  resumen; alimenta los reintentos y el post-mortem.
- `Span` — primitiva de telemetría (§8): nombre, duración, estado, atributos, error_type.

---

## 5. El pipeline fix-first, paso a paso

### 5.1 Topología

```
START → bootstrap → fix ──patch──→ validate ──verde o error distinto──→ immunize → report_commit
                     │ sin patch        │ mismo error                        │
                     ▼                  ▼                                    ├─ error residual y
                  rollback ◀────────────┘                                    │  presupuesto → fix
                     │ quedan reintentos → fix                               └─ todo verde → END
                     │ agotados → post_mortem → END
```

Dos bucles anidados: el **interno** (fix→validate→rollback→fix) reintenta el *mismo* error
hasta `max_retries` (defecto 3); el **externo** (report_commit→fix) avanza por errores
*encadenados* distintos hasta `error_cycle_budget` (defecto 5). Los routers son funciones
puras estado→`RoutingDecision` (enum cerrado), trivialmente testeables.

El sistema fue originalmente **test-first** (estilo CDD: un agente QA escribía el test
reproductor *antes* de reparar). Se descartó por una observación empírica: una pipeline
real entrega un *fallo*, no una especificación, y bloquear al reparador detrás de un
reproductor potencialmente malo mataba el progreso. El refactor a fix-first
(commit `a2abc9e`, 39 ficheros) es el punto de partida del sistema actual.

### 5.2 `bootstrap` — normalizar la entrada

Deriva del `TriggerEvent`: `entry_kind` (crash/test), `incident_id`, el
`reproduce_cmd` (por defecto `python main.py` en crash y `python -m pytest -x --tb=short`
en test; el llamador puede fijar el nodo exacto), la **firma inicial** parseando la salida
del fallo, los presupuestos, y la ruta del fichero de sesión de tests
`tests/test_selfheal_<session>.py`. Matiz ganado en producción (D12b, commit `dbcf8f4`):
el fichero **solo se crea en crash-entry** — en test-entry no se usa (el test que falla
*es* la regresión) y su esqueleto vacío llegó a colarse en dos PRs reales e indujo al
Reporter a redactar "Added regression test" sin haber añadido ninguno.

### 5.3 `fix` — el Corrector edita

Construye el `FixContext` (incluyendo las colas de los intentos fallidos del ciclo actual,
para que el reintento no repita la misma edición) y llama a `FixerPort.fix`. Tres salidas:
patch (→ validate), `None` o `FixGenerationError` (→ rollback, consumiendo presupuesto).

### 5.4 `validate` — reproducir en el sandbox

Ejecuta `reproduce_cmd` en el contenedor de validación (§9.1) y extrae dos cosas: el
**veredicto** (`PASSED/FAILED/TIMEOUT/INFRASTRUCTURE_ERROR`) y la **firma post-fix**
(`None` si verde). El router compara:

- verdicto verde → `immunize` (el error actual murió).
- firma post-fix **distinta** de la actual → `immunize` igualmente: es *progreso
  encadenado* — el error A se consolidará y el B será el siguiente objetivo.
- misma firma o salida imparseable → `rollback` (reintento).

### 5.5 `report_commit` — consolidar y avanzar la cadena

El Reporter redacta el mensaje (Conventional Commits; plantilla determinista de respaldo
si el LLM falla — un proveedor caído jamás bloquea un commit), `GitPort.commit` hace
`git add -A` + commit, y se acumula el `ResolvedError`. Si hay **firma residual distinta**
y queda presupuesto de ciclo: se resetea el scratch por-error (`attempt_count=0`, patch,
resultados, firmas post-fix), la firma residual pasa a ser la actual, el incidente se
renombra `<id>-chainN` y el router devuelve el control a `fix`. El caso real más bonito lo
produjo el benchmark: en `bugA/syntax-error` el Corrector reescribió un fichero entero y
omitió un método que no había mostrado (`dividir`), generando un `AttributeError` encadenado;
el sistema consolidó la reparación de sintaxis en su commit, reentró, restauró el método, y
salió con **2 commits + 2 tests** — el bucle externo funcionando con un error que el propio
sistema se autoinfligió (y que motivó la regla de prompt §6.1.3-c).

### 5.6 `immunize` — el test con compuerta (solo crash-entry)

Estrategia *targeted-first, gated, smoke-fallback*:

1. El **Tester** genera un test dirigido viendo el fallo original, el **diff de la
   reparación** y el **código fuente real ya reparado** (§6.2). El nodo lo añade al fichero
   de sesión.
2. **Compuerta**: si el árbol está verde, el test recién añadido se ejecuta en el sandbox
   (`pytest <node> -x`). Si no pasa, se revierte el fichero de sesión al estado anterior
   (snapshot/restore textual) y se descarta.
3. **Respaldo determinista**: si el dirigido cayó en la compuerta, se genera un *smoke
   test* sintáctico (re-ejecuta `reproduce_cmd` vía `subprocess` + `sys.executable`,
   afirma exit 0) que en árbol verde pasa por construcción.

Invariante resultante: **la inmunización nunca consolida un test que no demuestra nada,
pero tampoco desaparece en silencio**. En un error encadenado (árbol aún rojo) el dirigido
se conserva sin compuerta — exigirle verde sería imposible hasta cerrar la cadena.
En test-entry el nodo retorna inmediatamente: el test fallido del CI es la regresión.

### 5.7 `rollback` y `post_mortem`

`rollback`: `git reset --hard` + `git clean -fd` (los ficheros nuevos del intento fallido
también mueren), registra el `FailedAttempt` con la cola de la salida más fresca (la
post-fix si existe — el Corrector del reintento ve el *último* síntoma, no el primero) e
incrementa `attempt_count`. `post_mortem`: el Reporter redacta la narrativa (qué falló, qué
se intentó, siguiente paso humano) y `FilesystemReporter` persiste el Markdown con todo el
historial de intentos. `is_resolved=False`.

---

## 6. Los tres agentes

### 6.1 Corrector — mini-swe-agent gobernado por el adapter

#### 6.1.1 Reutilizado, no modificado

`MiniSWEFixer` envuelve mini-swe-agent **2.2.8 de pip, sin fork**. Toda la conducta se
gobierna desde el adapter: plantillas de prompt propias, `model_kwargs`, límites
(`cost_limit` 3 USD, `step_limit` 20, timeout 300 s), entorno de ejecución
(`DockerEnvironment` con el workspace montado) y persistencia de **trayectorias** (JSON
con cada turno de razonamiento, cada comando de shell y su salida — el artefacto forense
clave del proyecto, `reports/traces/`).

#### 6.1.2 El desbloqueo de gpt-4.1-mini: tool-calling nativo

El hallazgo técnico más importante de la fase de agentes (commit `9dac58f`). El
`LitellmModel` de mini-swe-agent llama **siempre** al proveedor con `tools=[bash]` y trata
una respuesta sin `tool_calls` como `FormatError` duro. Pero su configuración `default`
instruye al modelo a responder con un *bloque de texto* ```mswea_bash_command```. Un modelo
obediente (gpt-4.1-mini) seguía el prompt al pie de la letra, ponía el comando en
`content`, y era rechazado **cada turno**: bucle infinito de "No tool calls found" hasta
agotar el presupuesto. El diagnóstico salió de leer las trayectorias JSON; la solución fue
doble: plantillas propias *tool-calling-native* ("you act ONLY by calling the `bash` tool")
y `tool_choice="required"` en los `model_kwargs`, que hace estructuralmente imposible
quemar un turno sin actuar. Resultado inmediato: de 0/8 a 8/8 en el benchmark.

#### 6.1.3 Las reglas de edición (cada una con su incidente de origen)

a) **Heredoc, no `sed`**: para cualquier cambio supra-token — y siempre en errores de
   indentación — el prompt prohíbe la cirugía de espacios con `sed` (la forma clásica en
   que un modelo débil "arregla" una `IndentationError` rompiendo el fichero de otra
   manera) y exige reescribir el fichero completo con un here-document entrecomillado.
b) **Ver antes de reescribir**: antes de un rewrite completo es obligatorio haber impreso
   el fichero **entero** (`cat -n`).
c) **Nunca omitas código que no mostraste** (commit `044d12d`): la regla nacida del
   incidente `dividir` de §5.5 — reproducir cada clase/método/línea existente, cambiar solo
   lo que el fix necesita.
d) **No submitas sin ver exit 0**: el agente debe ejecutar el comando de éxito y verlo
   salir 0 antes de emitir la señal de finalización; "leer ficheros no es un fix".
e) **Nunca toques tests**: la inmunización es de otro agente.

El contexto que el adapter inyecta en la tarea incluye los **focus files**: el contenido
completo de los ficheros del workspace nombrados en el traceback (módulo compartido
`_focus.py`, §6.2), lo que ahorra turnos de `ls`/`cat` y evita que un modelo débil
confunda un error de import con un fichero inexistente.

#### 6.1.4 Ejecución y limpieza

El agente corre en un hilo (`asyncio.to_thread`) con timeout de pared; el diff se captura
en el host con `git add -N .` (los ficheros nuevos aparecen como diffs de vacío→contenido)
+ `git diff`. La limpieza de contenedores es de **doble capa**: el `cleanup()` de la
librería (que en Windows falla silenciosamente — ejecuta shell POSIX
`(timeout 60 docker stop …) >/dev/null 2>&1 &` con `shell=True`, es decir, contra cmd.exe;
diagnóstico D14) y una red de seguridad propia multiplataforma que lista los contenedores
`minisweagent-*` nuevos y los mata explícitamente.

### 6.2 Tester — tres generaciones hasta el test dirigido

La evolución del Tester es el mejor ejemplo del método de trabajo del proyecto
(observar → diagnosticar con artefactos → corregir → fijar con tests):

1. **Generación 1 — test unitario libre**: el LLM escribía "un test para el bug".
   Resultado real: **alucinaba la API** (`from gestor_cuentas import Cuenta` cuando la
   clase real era `CuentaBancaria`) y los tests rotos se consolidaban igualmente.
2. **Generación 2 — contrato smoke** (commit `f431b08`): test de caja negra que re-ejecuta
   el comando de reproducción y afirma exit 0, con prohibición de importar módulos del
   proyecto (no puede alucinar lo que no puede nombrar). Robusto pero genérico: detecta un
   re-crash, no una reintroducción sutil. La crítica del director ("demasiado genérico, no
   ataca al problema") fue el detonante de la tercera generación.
3. **Generación 3 — test dirigido con compuerta** (commit `72958d5`, vigente): el prompt
   recibe (a) el fallo, (b) el **diff exacto de la reparación** (`FixContext.fix_diff`) y
   (c) el **fuente real ya reparado** de los ficheros implicados (vía `_focus.py`,
   extraído del Corrector para que ambos agentes compartan el mismo lector). Con la API
   real delante, el test llama a la función concreta que estaba rota con entradas que
   habrían disparado el bug y afirma el comportamiento ahora correcto (FAIL_TO_PASS). Las
   reglas duras: imports dentro de la función (el fichero de sesión es compartido), nombres
   exactos del fuente, prohibido `pytest.raises` sobre el comportamiento reparado, setup
   mínimo. La compuerta de §5.6 hace de verificador final.

En el run de validación, los **8/8 tests fueron dirigidos** (cero respaldos smoke). Ejemplo
real generado para `bugB/attribute-error`: inyecta un `DummyDB` que *solo* expone
`cargar_datos` y dispara la ruta completa `procesar_transaccion → _guardar_estado` — si el
código volviera a llamar `cargar_dato`, el test falla. Eso es atacar al problema.

### 6.3 Reporter — narrativa que nunca bloquea

Dos llamadas de una sola pasada: el **mensaje de commit** (Conventional Commits tipo
`fix`, resumen imperativo ≤72 caracteres, cuerpo de 1–3 frases con causa raíz, a partir del
diff + firma + nº de intentos + test añadido) y el **post-mortem** (3–5 frases para el
humano cuando se agota el presupuesto). Contrato de resiliencia: **best-effort, jamás
lanza** — fallo de proveedor ⇒ plantilla determinista (commit) o narrativa vacía
(post-mortem). Un LLM caído nunca pierde un commit ni un informe. Calidad observada en el
run de validación: 8/8 mensajes correctos con scope y causa precisos (p. ej.
`fix(almacenamiento): initialize datos_cargados before try block to fix UnboundLocalError`).

---

## 7. Ingeniería de prompts: metodología PICCO

Los tres agentes siguen la estructura **PICCO** — secciones explícitas **P**ersona,
**I**ntención, **C**ontexto, **C**ondiciones, **O**utput (commit `0260edb`):

| Agente | Dónde vive cada sección |
|---|---|
| Corrector | Repartida como se estratifica una conversación: el *system template* lleva Persona + Intención (+ el canal de salida tool-call); el *instance template* lleva Contexto (la tarea renderizada), Condiciones y Output |
| Tester / Reporter | Las cinco secciones en su system prompt |

Dos decisiones de implementación importan más que la estética:

1. **Las reglas ganadas a pulso se conservaron literalmente** dentro de su sección
   (tool-calling nativo, heredoc-no-sed, never-omit-unseen-code, test dirigido con API
   real, salida solo-commit). No se reescribió contenido: se re-estructuró.
2. **La estructura está fijada por tests**: además de los tests de palabra clave
   preexistentes (que pinchan las reglas individuales), tests estructurales afirman la
   presencia de las cinco secciones en cada prompt. Cualquier regresión futura de formato
   rompe la suite antes de gastar un céntimo de LLM.

La validación fue empírica: un run completo del benchmark con los prompts PICCO dio el
mismo 8/8 con coste igual o ligeramente inferior (0,0449 vs ~0,049 USD — dentro del ruido
entre ejecuciones), cero reintentos y trayectorias visiblemente disciplinadas (la de
`identation-error` son **cuatro llamadas**: `cat -n` → heredoc → `python main.py` →
submit). Conclusión honesta: PICCO es neutro en coste y rendimiento y gana en
mantenibilidad y trazabilidad del prompt — exactamente lo que se pide a una reestructura.

---

## 8. Observabilidad: el patrón decorator y el coste por agente

### 8.1 El problema y el patrón

Requisito: medir latencia, tokens y coste **por agente y por nodo** sin contaminar el
código de negocio. La solución es el **decorator de objeto** del catálogo GoF (no el
`@decorator` sintáctico de Python, que habría tocado los métodos): cada puerto se envuelve
en un `Instrumented*` que abre un `span()` alrededor de la llamada y delega textualmente.
El `span()` es un context manager asíncrono que (a) cede un dict mutable de atributos para
enriquecer con el resultado (p. ej. el veredicto del sandbox), (b) registra siempre —
éxito o excepción —, (c) re-lanza sin alterar el contrato y (d) **nunca deja que un fallo
del sink rompa el flujo**. Los siete nodos del grafo se envuelven igual en `build_graph`
(`node.<nombre>`). El cableado es una sola llamada en el composition root
(`instrument_dependencies(deps, sink)`); con `NullTelemetry` por defecto, los tests corren
sin instrumentar.

### 8.2 Tokens y coste: el callback de litellm + contextvars

Los decoradores no ven *dentro* de una llamada LLM — y las del Corrector ocurren en las
tripas de mini-swe-agent. El gancho correcto es **litellm mismo**: un `CustomLogger`
registrado una sola vez emite un span `llm.completion` por cada completion del proceso,
con tokens de prompt/completion y coste. La **atribución** se resuelve con dos
`ContextVar`: `use_agent("corrector"|"tester"|"reporter")` — que los decoradores de agente
activan alrededor de cada llamada — y `using_llm_sink(sink)` — que cada entry point activa
alrededor del run. Ambas **se propagan al hilo de trabajo** de mini-swe-agent
(`asyncio.to_thread` copia el contexto), así que hasta las llamadas internas del Corrector
quedan etiquetadas. Limitación conocida y documentada (D4): el callback asíncrono de
litellm es una tarea *detached*; un `await asyncio.sleep(0.2)` tras el `ainvoke` da el
margen para que la última llamada (el Reporter) aterrice en el agregado.

### 8.3 El insight de coste

El agregado (`total` + `by_agent`) reveló el perfil económico del sistema: el **Corrector
concentra ~77 % del gasto, y lo hace vía prompt tokens** — mini-swe-agent reenvía la
trayectoria completa creciente en cada paso (mitigado parcialmente por el caché de prompts
del proveedor, visible en `cached_tokens` de las trazas). Tester ~18 % (una llamada, pero
con fuente + diff en contexto) y Reporter ~6 %. Consecuencia de diseño directa: los límites
de pasos importan más que los de coste, y cualquier optimización futura debe atacar el
tamaño del contexto del Corrector, no el número de llamadas.

---

## 9. Seguridad y guardarraíles

### 9.1 El sandbox de validación (endurecido e inmutable)

Toda reproducción y toda compuerta de test corren en `DockerSandbox` con un baseline de
seguridad **codificado como constantes que el llamador no puede relajar**:
`network_disabled=True`, `cap_drop=["ALL"]`, `security_opt=["no-new-privileges:true"]`,
`pids_limit=256`, `mem_limit=512m`, CPU limitada a 0,5 núcleos (tras corregir el defecto
D15, §13) y timeout de pared que produce un **veredicto** `TIMEOUT` recuperable, no una
excepción. La imagen (`self-healing-sandbox:latest`, `python:3.12-slim` + pytest) se
construye localmente desde `docker/sandbox.Dockerfile`. Estas propiedades están fijadas por
tests offline con un cliente Docker falso inyectado.

### 9.2 Las demás líneas

- **Nunca merge**: `GitHubPort` no expone ninguna operación de fusión; el cuerpo de cada
  PR lleva el aviso explícito *"Human review required — do not auto-merge"*.
- **El token jamás se filtra**: la URL de push lo lleva embebido, pero todo error de push
  se reescribe sustituyéndolo por `***` antes de propagarse; ningún log lo contiene.
- **Sin inyección de shell en CI**: los inputs de los workflows fluyen por `env` y nunca se
  interpolan en el script (un nombre de rama malicioso no ejecuta nada).
- **Anti-bucle**: el workflow on-push ignora `selfheal/**` y, además, las PRs creadas con
  el token por defecto de Actions no re-disparan workflows (guardia propia + guardia de
  GitHub). Concurrencia por rama para no apilar sanaciones.
- **Presupuestos**: coste y pasos por invocación del Corrector, reintentos por error,
  ciclos por sesión, timeouts por contenedor.
- **Asimetría documentada (D19, abierto)**: el contenedor del *Corrector* (a diferencia
  del de validación) corre con red y un `--add-host` residual de la era Ollama; el
  endurecimiento (`--network none`) está en el backlog y la afirmación del cuerpo de la PR
  se matizará en consecuencia.

---

## 10. El benchmark propio

### 10.1 Diseño

Un repositorio de escenarios (`Arnaufafi/Benchmark_for_agents`) con una pequeña aplicación
bancaria (5 módulos: gestor de cuentas, calculadora, almacenamiento JSON, modelos, main) y
**una rama por bug sembrado**: 4 de la familia A (sintaxis/imports/indentación — fallan al
importar) y 4 de la familia B (atributo/nombre/tipo/variable-no-inicializada — fallan en
ejecución), más `bugC/index-error` (que documenta un defecto del escenario: su `main.py`
sale limpio, no hay crash que reproducir — no es un fallo del sistema).

El harness (`scripts/run_benchmark.py`) garantiza el aislamiento entre ramas con una
disciplina git estricta: workspace borrado a bytes idénticos por run (con desarme del bit
read-only que git pone en `objects/pack` en Windows), `reset --hard` + `clean -fdx` +
checkout por rama, exclusión de artefactos de runtime vía `.git/info/exclude` (caso
untracked) y **skip-worktree** por rama para la base de datos JSON cuando la rama la trae
trackeada (caso tracked) — sin esto, el `git add -A` del commit arrastraba la BD generada a
cada heal commit. Cada rama corre el pipeline completo con dependencias reales y vuelca su
agregado de telemetría al informe (JSON + Markdown).

### 10.2 Resultados (run de validación `benchmark_20260612_090229`, prompts PICCO)

| Rama | Resuelto | Duración | Coste LLM |
|---|---|---|---|
| bugA/identation-error | ✅ | 32,6 s | $0,0036 |
| bugA/import-error | ✅ | 28,3 s | $0,0060 |
| bugA/module-not-found | ✅ | 26,3 s | $0,0054 |
| bugA/syntax-error | ✅ | 47,0 s | $0,0088 |
| bugB/attribute-error | ✅ | 32,3 s | $0,0045 |
| bugB/name-error | ✅ | 32,4 s | $0,0043 |
| bugB/type-error | ✅ | 34,1 s | $0,0072 |
| bugB/unboundlocal-error | ✅ | 24,9 s | $0,0051 |
| bugC/index-error | ❌ (no reproduce) | 0,3 s | — |

**8/8 reproducibles resueltos · $0,0449 totales · 257,8 s · 76 llamadas LLM · 190.416
tokens de prompt / 6.048 de completion · 0 reintentos · 0 rollbacks · 8/8 tests dirigidos
(0 smoke) · 8/8 mensajes de commit correctos.** Desglose por agente: Corrector $0,0344
(77 %, 60 llamadas), Tester $0,0079 (18 %, 8), Reporter $0,0026 (6 %, 8). El run pre-PICCO
de referencia cerró en ~$0,049 con idéntico 8/8 — la reestructura de prompts quedó validada
como neutra-o-mejor.

### 10.3 Qué midió de verdad

Más allá de la cifra de cabecera, el benchmark ejercitó y demostró: el bucle de cadena con
un error real autoinfligido (§5.5), la compuerta de inmunización descartando tests
inservibles, la higiene de commits (cero artefactos de runtime), la atribución de coste por
agente, y —al comparar runs— la no-regresión de cada cambio de prompt. Es un instrumento de
regresión del *sistema*, que es lo que SWE-bench no podía ser (§2.2).

---

## 11. El despliegue real: GitHub PR-on-CI

### 11.1 El entry point (`scripts/heal_and_pr.py`), paso a paso

1. **Clone fresco** del repo objetivo (con purga previa del workspace) y checkout de la
   rama base; el clone hereda los excludes de runtime.
2. **Rama de sanación** `selfheal/<uuid>` — los commits del pipeline nunca tocan la base.
3. **Sonda de detección** según el modo:
   - `--auto` *(el del workflow on-push)*: ejecuta la suite (`pytest --tb=short -q`); si
     falla, identifica el primer nodo `FAILED/ERROR`, lo re-ejecuta aislado para capturar
     fuente y traceback, y entra por **test-entry** (descartando nodos flaky que pasan en
     aislamiento); si la suite está verde (o no hay tests, rc=5), prueba `python main.py`
     y entra por **crash-entry**; si todo está verde, **exit 0 sin PR** — "nada que sanar"
     es éxito de CI.
   - `--test <nodeid>`: test-entry explícito. — *(sin flag)*: crash-entry.
4. **Escudo de artefactos** (D16, commit `9b2e2f0`): todo fichero *trackeado* que la sonda
   modificó al ejecutar el código del objetivo se restaura a su seed y se marca
   skip-worktree — ni la sonda, ni el contenedor del Corrector, ni el sandbox de
   validación pueden ya colar artefactos de runtime en el commit de la PR.
5. **Pipeline completo** (el mismo grafo, las mismas dependencias reales que el benchmark)
   con el sink de telemetría activo.
6. **Push + PR** vía `GitHubAdapter`: REST puro con `urllib` (sin dependencia del CLI
   `gh`), transporte HTTP inyectable para test, token nunca logueado. El cuerpo de la PR
   (`build_pr_body`) declara qué se reparó (el comando de reproducción), los errores
   sanados con sus fingerprints, los tests, y el **coste total y por agente** del heal.
   `--no-pr` permite sanar y inspeccionar localmente; `--draft` abre la PR como borrador.

### 11.2 Los workflows

- **`deploy/selfheal-on-push.yml`** (plantilla para el repo *vigilado*): `on: push` a toda
  rama — un merge a main es un push a main — con `branches-ignore: selfheal/**`,
  concurrencia por rama, y `permissions: contents+pull-requests: write` (el `GITHUB_TOKEN`
  por defecto basta intra-repo). Pasos: checkout del repo del sistema → deps → build de la
  imagen sandbox → `heal_and_pr --auto`. La cabecera documenta el alta única: secretos del
  LLM, el ajuste "Allow GitHub Actions to create PRs" y la limitación de dependencias.
- **`.github/workflows/self-heal.yml`** (dispatch): sanación bajo demanda de cualquier
  repo por nombre; input de test vacío ⇒ `--auto`.

### 11.3 PR real #1 — el bug latente (la demostración estructural)

Escenario sembrado: rama `ci/latent-bug` cuyo `main.py` **sale limpio** pero cuyo
`GestorBaseDatos.cargar_datos` deja escapar `FileNotFoundError` (solo guarda
`JSONDecodeError`); `main.py` lo enmascara porque `inicializar_bd()` crea el fichero antes.
Un test de CI lo expone. Resultado medido: la sonda eligió test-entry (crash-entry habría
dicho "nada que sanar" — **la PR existe únicamente gracias al canal de test**), el
Corrector reparó con el mínimo exacto (`except FileNotFoundError: return {}`), la
inmunización se saltó correctamente (cero llamadas del Tester), y la PR quedó abierta en
**27 segundos y ~$0,002** (trayectoria del Corrector: cat → heredoc → pytest del nodo →
submit; 4 llamadas, $0,0016).

### 11.4 PR real #2 — el bug sembrado vía CI

Rama `ci/name-error` (base con el `NameError` de `lista_montos` + un test de CI realista
que falla por él, sin tocar disco). La sonda eligió el test **aunque `main.py` también
crasheaba** (prioridad tests-first), el fix fue la línea exacta (`lista_montos`→`montos`),
el mensaje del Reporter fue preciso, y el coste total **$0,0027** (8.686 prompt + 670
completion, 5 llamadas). Efecto colateral verificado: `main.py` quedó sanado de propina —
mismo bug, dos síntomas, una cura. La PR quedó abierta contra su base sin fusionar nada.

### 11.5 La lección de las dos PRs

El par demuestra empíricamente la complementariedad de los dos canales de entrada: el
crash cubre lo que revienta; el test cubre lo *latente* y lo *semántico*. Y deja una
lección operativa de revisión: el diff que GitHub propone al pushear una rama semilla
(`compare` contra la rama por defecto) no es el diff de la PR del bot
(`selfheal → base`) — confundirlos hizo parecer que el sistema había añadido artefactos
que en realidad pertenecían al seed del escenario.

---

## 12. Verificación y calidad

- **115 tests** (unit + integración), todos herméticos: la integración recorre el grafo
  completo con adapters in-memory (veredictos guionizados para forzar cada ruta: happy
  path, reintento+recuperación, presupuesto agotado, cadena de dos errores, test-entry).
- **Tests de contrato de prompts**: palabras clave (cada regla ganada) + estructura PICCO
  (las cinco secciones). Un cambio de prompt que rompa una invariante falla en CI, no en
  un run de pago.
- **Tests conductuales sobre infraestructura real barata**: el escudo D16 se prueba contra
  un repo git temporal de verdad (commit → ensuciar → escudo → re-ensuciar → `add -A` →
  el commit contiene el fix y no el artefacto); la sonda auto se prueba con pytest real
  sobre `tmp_path`; el sandbox, con un cliente Docker falso que pincha el baseline de
  seguridad y la aritmética de CPU.
- **Estático**: `ruff` con configuración estricta (E/F/I/B/UP/N/ASYNC/D/RUF,
  línea 100, docstrings pep257) en verde; `mypy` se evaluó y descartó con justificación
  (Protocols + pydantic + la suite dan la red de tipos proporcional al alcance).
- **Trazabilidad**: cada run real persiste trayectorias completas del Corrector
  (JSON por incidente) y agregados de telemetría por rama; los informes del benchmark
  llevan JSON + Markdown.

---

## 13. El registro de defectos como metodología

El proyecto mantiene un registro numerado (D1–D19) en el log de trabajo del autor, donde
cada defecto tiene síntoma, causa raíz, decisión y —si se arregló— commit y test.
Selección representativa:

| ID | Hallazgo | Causa raíz | Estado |
|---|---|---|---|
| D2 | `UnicodeEncodeError` en consolas Windows | prints con emoji bajo cp1252 | ✅ ASCII `[OK]/[FAIL]` |
| D12b | Esqueleto de test vacío en PRs reales (+ alucinación inducida del Reporter) | bootstrap creaba el fichero de sesión también en test-entry | ✅ `dbcf8f4` |
| D14 | 2× "El sistema no puede encontrar la ruta especificada" | upstream: cleanup de mini-swe-agent ejecuta shell POSIX con `shell=True` ⇒ cmd.exe | ✅ diagnosticado; inocuo (limpieza propia de doble capa) |
| D15 | Sandbox de validación al **5 %** de CPU | error de unidades: `nano_cpus = quota×1000` en vez de `×10_000` | ✅ `079e2b3` + test |
| D16 | Artefactos de runtime trackeados entrarían en PRs de crash-entry | la sonda ejecuta el código del objetivo y `git add -A` barre | ✅ `9b2e2f0` (escudo) + test conductual |
| D17 | Fantasmas del diseño test-first (puertos/excepciones muertos, nombres engañosos) | refactor incompleto | ✅ `c1ec986` (purga verificada por grep) |
| D5 | Sin guardia anti-espiral (ping-pong A→B→A solo lo frena el presupuesto) | — | ⏸️ backlog (con cautela: §5.5 muestra que "avanzar" puede ser correcto) |
| D19 | Red + puerta al host en el contenedor del Corrector | residuos era-Ollama | ⏸️ backlog |

Dos defectos merecen subrayado metodológico: **D15** estuvo dormido durante toda la vida
del proyecto (la carga de pytest era tan ligera que un sandbox al 5 % de CPU pasaba
desapercibido) y solo cayó en una auditoría sistemática post-freeze; **D16** se encontró
*por análisis* antes de manifestarse — la PR #2 se libró únicamente porque test-entry no
ejecuta `main.py`. La tesis implícita: en un sistema autónomo que comete y publica código,
**el registro de defectos es parte del producto**, no un anexo.

---

## 14. Resultados, limitaciones y conclusiones

### 14.1 Resultados consolidados

| Evidencia | Resultado |
|---|---|
| Benchmark propio (9 escenarios) | **8/8 reproducibles resueltos**, $0,0449, ~4,3 min, 0 reintentos |
| Calidad de inmunización | 8/8 tests dirigidos FAIL_TO_PASS, 0 respaldos smoke, 100 % pasan la compuerta |
| PR real #1 (bug latente) | abierta en 27 s, ~$0,002, fix mínimo exacto, inmunización correctamente omitida |
| PR real #2 (bug sembrado) | $0,0027, fix de una línea, crash de producción sanado como efecto colateral |
| Robustez de ingeniería | 115 tests herméticos, ruff estricto en verde, registro D1–D19 |
| Agnosticismo | qwen-14B local → gpt-4.1-mini Azure cambiando una variable de entorno |

### 14.2 Limitaciones (honestas)

1. **Validación con un solo modelo y repos de juguete**: la generalización a repos con
   dependencias pesadas exige imágenes de sandbox con las deps instaladas (hoy la imagen
   solo trae pytest; en test-entry el contenedor del Corrector debe llevarlo también —
   resuelto apuntándolo a la imagen del sandbox).
2. **La sonda de detección ejecuta el código del objetivo en el host** (no en el sandbox);
   aceptable en un runner efímero de CI, mejorable en local.
3. **Sin guardia anti-espiral** más allá del presupuesto de ciclos (D5).
4. **El flush de telemetría de 0,2 s** es una carrera teórica (D4).
5. El estreno del workflow on-push end-to-end en GitHub Actions queda pendiente de alojar
   este repositorio en GitHub y configurar secretos (el código está listo y testeado; la
   misma lógica quedó validada ejecutando el entry point localmente contra GitHub real).

### 14.3 Conclusiones

Se ha construido, instrumentado y validado en condiciones reales un sistema multiagente
que cierra el ciclo completo de la auto-sanación de código: detectar → reparar → validar →
inmunizar → documentar → proponer (nunca fusionar). Las decisiones que más rendimiento
dieron por unidad de complejidad fueron tres: la **arquitectura hexagonal** (que convirtió
tests, telemetría y cambios de modelo en operaciones locales), la **huella de error
estable** (que transformó "reintentar vs avanzar" de heurística frágil a comparación de
fingerprints) y la **reutilización gobernada** de mini-swe-agent (toda la conducta en el
adapter, el motor intacto). El coste operativo medido — del orden de **medio céntimo de
dólar por error reparado, con test de regresión y mensaje de commit incluidos** — sitúa la
auto-sanación de la clase de errores estudiada claramente por debajo del coste de
interrupción de un ingeniero, y el diseño PR-con-gate-humano mantiene la decisión final
donde debe estar.

---

## Apéndice A: referencia de configuración

| Variable | Defecto | Efecto |
|---|---|---|
| `CDD_AGENT_MODE` | `mock` | `mock` = agentes deterministas in-memory; `real` = mini-swe-agent + litellm |
| `CDD_LLM_MODEL` | `claude-sonnet-4-20250514` | Modelo litellm compartido por los tres agentes |
| `CDD_MAX_RETRIES` | 3 | Reintentos por error (bucle interno) |
| `CDD_ERROR_CYCLE_BUDGET` | 5 | Errores encadenados por sesión (bucle externo) |
| `CDD_SANDBOX_IMAGE` | `self-healing-sandbox:latest` | Imagen del sandbox de validación |
| `CDD_SANDBOX_TIMEOUT` | 60 s | Timeout de pared por comando sandboxeado |
| `CDD_SWEAGENT_COST_LIMIT` / `_STEP_LIMIT` / `_TIMEOUT` | 3 USD / 0 / 300 s | Presupuestos del Corrector |
| `CDD_SWEAGENT_USE_DOCKER` / `_DOCKER_IMAGE` | true / `python:3.12-slim` | Contenedor del Corrector (en test-entry: apuntar a la imagen del sandbox para disponer de pytest) |
| `CDD_SWEAGENT_TRAJECTORY_DIR` | — | Persistencia de trayectorias JSON |
| `CDD_TESTER_TIMEOUT` | 180 s | Timeout del Tester |
| `CDD_REPORTS_DIR` / `CDD_LOG_LEVEL` / `CDD_LOG_JSON` | `./reports` / INFO / true | Informes y logging |
| `GITHUB_TOKEN` | — | Clone/push/PR |
| `OPENAI_API_KEY` + `OPENAI_API_BASE` | — | Proveedor OpenAI-compatible (Azure AI Foundry) |

Comandos canónicos: `python -m src.main` (demo mock offline) ·
`python scripts/run_benchmark.py` (benchmark) ·
`python scripts/heal_and_pr.py --repo o/n --base main --auto [--no-pr|--draft]` (despliegue).

## Apéndice B: cronología de commits clave

| Commit | Hito |
|---|---|
| `a2abc9e` | Refactor test-first → **fix-first** (39 ficheros): grafo, puertos, dominio, cadena |
| `9dac58f` | Desbloqueo del Corrector: prompt tool-calling-native + `tool_choice=required` → 8/8 |
| `f431b08` | Tester gen-2 (contrato smoke) + compuerta de inmunización |
| `044d12d` | Regla "nunca omitas código no mostrado" (incidente `dividir`) |
| `ea2d57c` | Higiene de commits: skip-worktree de la BD runtime por rama |
| `f1a07d2` | Telemetría por decoradores (spans por puerto y por nodo) |
| `72958d5` | Tester gen-3: **test dirigido** con diff + fuente real + compuerta |
| `976fa80` | Tokens/coste **por agente** (callback litellm + contextvars) |
| `019b049` | Despliegue GitHub: `GitHubPort/Adapter`, `heal_and_pr`, cuerpo de PR |
| `65c2acd` | Parser de firmas: preferir la línea `E` no truncada de pytest |
| `771e83d` | Sonda `--auto` + workflow on-push (bucle PR-on-CI cerrado) |
| `0260edb` | Prompts **PICCO** en los tres agentes (validado con run completo) |
| `dbcf8f4` | D12b: fichero de sesión solo en crash-entry |
| `f8a280a` / `efb8f67` | README reescrito / docx v2 (despliegue como implementado) |
| `18f2585` | Auditoría post-freeze completa (registro D15–D19) |
| `079e2b3` / `9b2e2f0` / `c1ec986` | Fixes D15 (CPU ×10) / D16 (escudo de artefactos) / D17 (purga legacy) |
