# Correr el sistema sobre un slice de SWE-bench

Validación del **Corrector a escala de repos reales** sobre instancias de SWE-bench, puntuada
por el evaluador oficial. Lee primero el encuadre — determina cómo se presenta en la defensa.

## Encuadre (importante para la defensa)

SWE-bench es **test-entry puro**: cada instancia trae tests `FAIL_TO_PASS` que fallan en
`base_commit` y pasan tras el fix → encaja con `TriggerEvent(TEST_FAILURE)`. En test-entry el
pipeline **salta el Tester** (`immunize_node`, §5.6 de la memoria): el test que falla *es* la
regresión. Por tanto esto ejercita **Corrector + validate + Reporter**, no el sistema completo.
Preséntalo como **prueba de escalado del Corrector**, no como test de los tres agentes. Es honesto
y te cubre.

## Plataforma: WSL2 / Linux (NO Windows nativo)

SWE-bench **no funciona en Windows nativo**, por dos razones duras:
1. El harness oficial `swebench` importa el módulo `resource`, que es **solo-Unix**
   (`ModuleNotFoundError: No module named 'resource'` al ejecutar el scorer en Windows).
2. Las imágenes de evaluación se construyen/orquestan asumiendo Linux.

Haz **todo el flujo dentro de WSL2 (Ubuntu) con Docker**: ahí `resource` existe, las imágenes se
construyen/pullean, y el montaje en `/testbed` es nativo (sin traducir rutas `G:\` → `/g/`). El
código del healer es el mismo; en WSL `platform.system()` es `Linux`, así que no añade el
`--add-host` de Windows y monta la ruta tal cual. **Antes de nada, comprueba que las imágenes
existen**: `docker images | grep sweb` — si no sale ninguna, el run falla con `ImageNotFound`
("pull access denied"), que es exactamente lo que verás si te saltas este paso.

## Por qué necesita las imágenes oficiales

El sandbox propio es `python:3.12-slim`+pytest; cada instancia necesita las **dependencias de su
repo**. No se reinventa el entorno: se usan las **imágenes oficiales por instancia** del harness de
`swebench` (traen el entorno con las deps y el paquete en editable apuntando a `/testbed`). El
sistema monta el clone del host **sobre `/testbed`** (`CDD_SANDBOX_WORKDIR=/testbed`): el registro
del editable-install (`.pth`/finder en site-packages de la imagen) sigue apuntando a `/testbed`,
que ahora sirve nuestro clone → las ediciones del agente se ven al importar. El único cambio al
núcleo fue parametrizar ese punto de montaje (commit `216e9e8`), retrocompatible (default
`/workspace`, el benchmark 8/8 intacto).

## Setup (una vez, DENTRO de WSL2 / Linux)

```bash
pip install datasets swebench          # datasets: runner/selector; swebench: scoring oficial
# Construir las imágenes del slice (lento, GBs; necesita Docker). DOS flags clave:
#  --cache_level instance : sin él (default=env) el harness BORRA las sweb.eval.x86_64.* al acabar.
#  --namespace none       : sin él (default="swebench") NO construye en local; baja imágenes
#                           remotas swebench/sweb.eval.x86_64.<id con __→_1776_>, que NO casan con
#                           la plantilla del runner. Con "none" construye local y nombre canónico.
python -m swebench.harness.run_evaluation \
  --dataset_name princeton-nlp/SWE-bench_Lite \
  --predictions_path gold --run_id build_images \
  --instance_ids <ids del slice> --namespace none --cache_level instance
docker images | grep sweb              # ✓ deben quedar sweb.eval.x86_64.<id>:latest
# Credenciales del LLM (las mismas del benchmark)
export OPENAI_API_KEY=...  OPENAI_API_BASE=...
export CDD_AGENT_MODE=real  CDD_LLM_MODEL=openai/gpt-4.1-mini
# Apuntar el sistema a las imágenes de SWE-bench
export CDD_SANDBOX_WORKDIR=/testbed
# Las imágenes instalan pytest + las deps del repo en el conda env "testbed"; el
# python base está pelado. Sin esto: "No module named pytest" → FAILED en todo.
export CDD_SANDBOX_PYTHON=/opt/miniconda3/envs/testbed/bin/python
# Algunas instancias (p. ej. requests) tienen tests que pegan a la red (httpbin); el
# sandbox corre sin red por defecto. Relájalo SOLO para las imágenes oficiales:
export CDD_SANDBOX_ALLOW_NETWORK=1
export CDD_SWEAGENT_USE_DOCKER=true
export CDD_SWEAGENT_COST_LIMIT=0.5     # tope por instancia
export CDD_SWEAGENT_STEP_LIMIT=15
export CDD_SANDBOX_TIMEOUT=120
```

Las **imágenes** se construyen/descargan con el harness oficial (pesan GB; eso es disco/descarga,
**no** tokens). Con `--namespace none` se construyen en local con el nombre canónico
(`sweb.eval.x86_64.{instance_id}:latest`) que el runner espera. Alternativa **más rápida** (baja
prebuilt en vez de construir): deja el namespace por defecto y, tras el run, renómbralas —
`for id in $IDS; do docker tag swebench/sweb.eval.x86_64.${id//__/_1776_}:latest sweb.eval.x86_64.$id:latest; done`.
El `--image-template` del runner NO sirve para esto porque no puede reproducir el `__→_1776_`.

## Los tres comandos

```bash
# 1. Elegir el slice barato (1 fichero, parche corto, repos ligeros)
python scripts/swebench_select.py --limit 8 --out slice.json

# 2. Sanity SIN LLM: por instancia, FAIL_TO_PASS debe fallar y pasar con el patch dorado.
#    Valida imagen + montaje antes de gastar un token.
python scripts/run_swebench.py --instances-file slice.json --sanity

# 3. Curar el slice → predictions.jsonl
python scripts/run_swebench.py --instances-file slice.json --predictions-out preds.jsonl
```

Resultados en `reports/swebench/`. Por instancia se imprime `{resolved, patch_bytes, cost_usd}`
según **nuestro** `validate` (bucle interno) — no es la verdad oficial.

## Scoring oficial (la verdad)

Nuestro `validate` comprueba `FAIL_TO_PASS↑` pero **no** `PASS_TO_PASS` (regresión). La nota
citable la da el harness oficial, que re-aplica el `model_patch` sobre una imagen limpia, aplica el
`test_patch`, y verifica FAIL_TO_PASS↑ **y** PASS_TO_PASS sin romperse:

```bash
python -m swebench.harness.run_evaluation \
  --dataset_name princeton-nlp/SWE-bench_Lite \
  --predictions_path preds.jsonl --run_id selfheal_slice --max_workers 4
```

## Cómo se mapea cada instancia (resumen del runner)

1. `git init` + `fetch --depth 1 origin <base_commit>` + checkout; `git apply` del `test_patch` y
   **commit T** (deja el árbol limpio para el `git add -A` del pipeline).
2. Reproduce los `FAIL_TO_PASS` **en la imagen** (el host no tiene las deps) → captura el fallo.
3. Construye `TriggerEvent(TEST_FAILURE, FailingTest(node_id, source, last_failure_output))` y corre
   el grafo con `build_real_deps(image_override=<imagen>)` y `state["sandbox_image"]=<imagen>`.
4. `model_patch = git diff <base_commit> HEAD -- . ':(exclude)<rutas-de-test>'` → solo la fuente.

## Caveats

1. Mide **2/3 del sistema** (sin Tester) — dilo tú primero.
2. Imágenes de GB = disco/descarga, **no** tokens; sepáralo del coste LLM en la memoria.
3. Coste por instancia mayor que en la app de juguete (ficheros más grandes): ~$0.02–0.10 incluso
   en las "baratas"; el `CDD_SWEAGENT_COST_LIMIT` lo acota.
4. `python -m pytest` asume repos pytest (el slice barato lo es); otros runners necesitan ajuste.
5. El `validate` propio ignora `PASS_TO_PASS` → el **scorer oficial** es la nota; el nuestro es el
   bucle interno.
6. El sistema **no puede ejecutarse en el entorno de desarrollo de Claude** (sin Docker ni imágenes
   ni red): el código está testeado offline (lógica pura + 126 tests) y el run real lo lanzas tú.
7. Montar un clone fresco de git sobre ``/testbed`` **tapa ficheros generados en el build** (p. ej.
   ``src/_pytest/_version.py`` que `setuptools_scm` crea al instalar). Repos que los importan al
   arrancar (pytest sobre sí mismo) fallan con ``ModuleNotFoundError``/``InvalidVersion`` aunque el
   parche sea correcto. Repos puramente fuente (requests, pylint, seaborn) no se ven afectados. Por
   eso el slice barato evita instancias ``pytest-dev/pytest`` salvo que se siembre desde la imagen.
