# MemoryGraf — Guion de prueba de integración End-to-End

> **Para:** una sesión de Claude Code (u otro asistente) en una **máquina limpia**.
> **Objetivo:** verificar de punta a punta que MemoryGraf funciona (Capas 0–3: estructura,
> Git, runtime, compilador local), medir el ahorro de tokens, y **encontrar bugs o mejoras**.
> **Modo de trabajo:** escéptico y adversarial. No te fíes de los docs: ejecuta, compara con
> lo esperado, y reporta divergencias. Al final, entrega el reporte de la sección §11.

Este proyecto es un **grafo de contexto local, portable y agnóstico al LLM** que expone
consultas dirigidas por MCP + CLI. Principios vinculantes (DESIGN §3): fuentes de verdad
legibles, **portabilidad con degradación elegante** (el núcleo corre solo con stdlib de
Python; toda dependencia es opcional), trazabilidad (`archivo:línea`), incremental, y
**caché regenerable que nunca es fuente de verdad** (Git/tests/embeddings/resúmenes).

---

## 0. Contexto de lo implementado (para calibrar, no para confiar)

Roadmap de capas **completo** e implementado (Capas 0–3). Resumen:

| Capa / Fase | Módulo(s) | Qué aporta |
|---|---|---|
| Capa 0 — Estructura | `indexer`, `extractors/`, `cross_link`, `docs`, `entities`, `semantic`, `summarizer` | símbolos, llamadas, imports, decisiones, entidades, embeddings, resúmenes |
| Capa 1 — Temporal/Git (Fase 6) | `git_layer.py` | churn, fechas, fix_touches, autores; arista `co_changes_with`; `working_set`/`impact`/`history` |
| Capa 3 — Compilador local (Fase 7) | `context_compiler.py` | `digest_log`; narrativa "por qué" del co-cambio; `rerank` (opt-in) |
| Capa 2 — Runtime (Fase 8) | `runtime/tests.py`, `runtime/lsp.py` | cobertura+JUnit → `covered`/`last_test_status`; arista `tested_by`; diagnósticos LSP |
| Adopciones (Fase 9) | `confidence.py`, `analyze.py`, `report.py` | etiquetas EXTRACTED/INFERRED/AMBIGUOUS; god-nodes/hotspots; `GRAPH_REPORT.md` |
| Métrica | `benchmark.py` | ahorro de tokens (recuperación selectiva) |

**Ya diferido conscientemente** (NO lo reportes como bug — verifica que degrada bien):
- `resolved_type` por hover LSP: no implementado (la columna existe; `lsp.py` solo trae diagnósticos).
- `tested_by` es a nivel **archivo→archivo** (INFERRED por imports), no símbolo→test.
- `rerank` vía LLM en tiempo de consulta: diferido; el `rerank` actual es determinista y opt-in.
- Historia **pre-rename** no se arrastra (no se usa `git log --follow`).
- Co-cambio solo **file↔file** (no símbolo↔símbolo).

**Ya auditado y corregido** (no re-descubrir; sí re-verificar que siguen bien): determinismo
de top-N commits, `rerank` cableado en `search(rerank=True)`, `history()` surfacea co-cambio,
titular del benchmark como *límite superior*, Cobertura lee `<sources>`, anti-staleness de
runtime, heurística de test por segmento, umbral de god-nodes sin externos, `provenance`
funcional en `confidence`.

---

## 1. Prerrequisitos

- **Python 3.10+** (probado en 3.12). El núcleo NO necesita nada más.
- **git** en el PATH (para la Capa 1). Sin git, la capa se omite sola.
- Opcionales (potencian, no obligatorios): `tree-sitter` + `tree-sitter-languages` (JS/TS exacto),
  `model2vec` (embeddings neuronales), `watchdog` (watch).
- Para la **prueba del LLM local**: Ollama (se instala con un comando, §3).
- Para la **prueba de runtime real** (§7): `pytest` + `coverage`/`pytest-cov`; y opcional un
  language-server (`pyright` o `python-lsp-server`) para diagnósticos LSP.

```bash
python3 --version
git --version
```

---

## 2. Clonar e instalar

```bash
git clone git@github.com:JeffersonJ03/MemoryGraf.git
cd MemoryGraf                       # el paquete Python es memorygraf/

# Opción A (recomendada, aislada): venv + editable con extras
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[full]"            # instala extras opcionales (tree-sitter, model2vec, watchdog)
# Si ".[full]" falla por red/deps, el núcleo sigue: pip install -e .  (o usar python3 -m memorygraf.cli)

# Opción B (sin instalar): usar siempre  python3 -m memorygraf.cli <cmd>  desde la raíz del repo
```

> **Nota de portabilidad:** todos los comandos de abajo funcionan como `memorygraf <cmd>`
> (si instalaste) o como `python3 -m memorygraf.cli <cmd>` (si no). Elige uno y sé consistente.

---

## 3. Instalar el LLM local (Ollama) — opcional pero recomendado

```bash
python3 -m memorygraf.cli setup-ollama
```
Espera: detecta plataforma (windows/macos/wsl/linux), instala el binario (sin sudo en
WSL/Linux), descarga el modelo `qwen2.5-coder:3b` (~2 GB, primera vez) y escribe el bloque
`summary` en la config con `backend=auto`. **Verifica**: al terminar debe decir "Listo".

Sin Ollama, MemoryGraf usa el summarizer **heurístico** determinista (degradación elegante).

---

## 4. Suite de pruebas unitarias (primer gate)

```bash
python3 -m unittest discover -s tests -v
```
**Esperado:** todo en verde (49 tests al momento de escribir esto; el número puede crecer).
- ⚠️ **Verifica que NO aparezca ningún `ResourceWarning`** en la salida (se corrigió un leak de
  model2vec; si reaparece, es regresión).
- Los tests que crean repos git de prueba se **omiten** (`skip`) si no hay `git` — anótalo.

Si algo falla: captura el traceback completo, es un hallazgo de §11.

---

## 5. Sincronización completa (construye el grafo)

```bash
memorygraf init          # crea .memorygraf/config.json apuntando al repo (si no existe)
memorygraf sync          # index → git_layer → runtime → compiler → summaries → embed
```
Observa el log del `sync`. **Esperado (orden y forma):**
```
index: N cambiados, M sin cambio, 0 eliminados
enlaces cross-project: ... | decisiones: 9, convenciones: 40 | entidades: ...
git: K commits nuevos · P archivos blame · Q aristas co_changes_with
runtime/tests: sin artefactos ... (R aristas tested_by por heurística de imports)
resúmenes (ollama:... o heuristic-v1): ... nuevos, ... de cache
embeddings: ... nuevos, ... sin cambio
{"sync_version": ...}
```
**Verifica:**
- La capa `git:` produce commits/blame/co-cambio (si es un repo git con historia).
- Si instalaste Ollama, el resumen dice `ollama:qwen2.5-coder:3b` y el servidor efímero
  **arranca y se apaga solo** (debe verse "arrancando…"/"apagando…"). Si no, dice `heuristic-v1`.
- `memorygraf stats` muestra nodos/aristas por tipo (incluye `co_changes_with`, `tested_by`).

```bash
memorygraf stats
```

---

## 6. Capa 0 + Capa 1: consultas dirigidas (MCP/CLI)

Ejecuta y **lee críticamente** cada salida (¿tiene procedencia `path:línea`? ¿respeta el
presupuesto de tokens? ¿es coherente?):

```bash
memorygraf overview
memorygraf search "indexer"
memorygraf decisions
# elige un archivo real del grafo (mira 'overview' o 'stats'); ejemplos:
memorygraf get "MemoryGraf/memorygraf/store.py"
memorygraf neighbors "MemoryGraf/memorygraf/store.py"
# --- Capa 1 · Temporal/Git ---
memorygraf working-set                       # ¿qué se está tocando ahora?
memorygraf impact  "MemoryGraf/memorygraf/store.py"   # blast radius: dependientes ∪ co-cambio
memorygraf history "MemoryGraf/memorygraf/store.py"   # churn, fragilidad, autores, commits, co-cambio
```
> El prefijo del node id es `<nombre-proyecto>/<ruta>`. El nombre del proyecto lo fija
> `memorygraf init` (por defecto, el nombre de la carpeta). Ajusta los ids a lo que muestre
> `overview`/`search` en tu máquina.

**Verifica en `impact`:** debe listar **quién depende** del nodo (entrantes) + co-cambios, y
anotar co-cambios con su narrativa (`↳ …`). **En `history`:** churn/fix/edad/autores + "co-cambia con".
**Casos borde a probar:** un nodo inexistente (mensaje claro), un símbolo (`...store.py::Store`).

---

## 7. Capa 2 — Verdad de runtime (necesita artefactos reales)

Genera cobertura + JUnit **de verdad** e ingiérelos:

```bash
pip install pytest coverage pytest-cov         # si no están
# genera coverage.xml (Cobertura) + junit.xml corriendo la suite bajo pytest:
python3 -m pytest tests --junitxml=junit.xml --cov=memorygraf --cov-report=xml -q
memorygraf runtime                             # ingiere coverage.xml/junit.xml (auto-descubre)
```
**Verifica:**
- El log dice `cobertura=… nodos · tests=… · … aristas tested_by`.
- `memorygraf get "<un símbolo cubierto>"` muestra una línea `runtime: cobertura: sí (NN%)`.
- `memorygraf get "<un test que falle si lo rompes>"` muestra `último test: passed/failed`.
- `memorygraf impact "<nodo>"` anota afectados con ⚠ "SIN cobertura / test failed" cuando aplique.
- **Anti-staleness:** borra `coverage.xml`, vuelve a correr `memorygraf runtime`, y confirma que
  `covered` vuelve a vacío (no se queda pegado).

**LSP (opcional, diagnósticos):**
```bash
pip install pyright        # o: pip install python-lsp-server
memorygraf runtime --lsp   # arranca el LSP efímero, mapea diagnósticos a nodos
```
Verifica que, sin language-server instalado, **se omite en silencio** (no rompe el sync).

---

## 8. Capa 3 — Compilador de contexto local

```bash
# Digestión de logs (el mayor sumidero de tokens):
python3 -m pytest tests -q > /tmp/testlog.txt 2>&1 || true   # genera un log real (con o sin fallos)
memorygraf digest /tmp/testlog.txt                 # núcleo determinista
memorygraf digest /tmp/testlog.txt --llm           # + línea de "situación (LLM local)" si hay Ollama

# Narrativa del "por qué" del co-cambio (heurística por defecto; LLM opt-in):
memorygraf compile                                 # backend=auto → heurístico (no arranca modelo)
# luego revisa que impact()/history() muestran la narrativa ↳
```
**Verifica (guardarraíles §6.4):** el `digest` siempre lista los hallazgos deterministas con
procedencia `@archivo:línea`; el LLM solo **añade** una línea de resumen (no decide). Con
`--llm` y Ollama, el servidor efímero arranca y se apaga. Sin Ollama, `--llm` degrada sin error.

**Rerank (opt-in):** no hay CLI directa; pruébalo en Python:
```bash
python3 - <<'PY'
from memorygraf import workspace
from memorygraf.store import Store
from memorygraf.query import Query
db = workspace.resolve_db_path(workspace.resolve_config_path(None))
print(Query(Store(db)).search("store", rerank=True))   # el modo debe decir "...+rerank"
PY
```

---

## 9. Fase 9 — Análisis y reporte

```bash
memorygraf analyze          # JSON: god-nodes (excluye externos) + hotspots de fragilidad
memorygraf report           # genera GRAPH_REPORT.md (regenerable, gitignored)
cat GRAPH_REPORT.md
```
**Verifica:** el reporte tiene secciones Resumen / Confianza (EXTRACTED/INFERRED/AMBIGUOUS) /
Riesgo arquitectónico / Hotspots / Co-cambio con "por qué". Los god-nodes **no** deben ser
`os`/`json`/`__future__` (externos excluidos). Un hotspot debe implicar **churn real** (≥2 o
con `fix`), no solo "sin cobertura".

---

## 10. Benchmark de tokens

```bash
memorygraf sync             # asegúrate de tener el grafo fresco
python3 benchmark.py
```
**Verifica la HONESTIDAD del número, no solo que salga alto:**
- El TOTAL debe excluir la tarea marcada `*` (log sintético).
- El pie debe declarar que es un **límite superior de este repo** y citar la meta honesta (≥40%).
- Reproduce a mano una tarea (p.ej. cuenta caracteres de un archivo vs la salida de `get`) para
  confirmar que `est_tokens` se aplica igual a ambos lados. Reporta si crees que el baseline
  está amañado.

---

## 11. Servidor MCP (integración con un cliente real)

```bash
memorygraf mcp-config                 # imprime el JSON para pegar en un cliente MCP
memorygraf install claude             # (si tienes el CLI 'claude') registra el MCP en 1 paso
```
Smoke test del protocolo sin cliente (stdio JSON-RPC):
```bash
printf '%s\n%s\n' \
 '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05"}}' \
 '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | memorygraf mcp 2>/dev/null
```
**Verifica:** responde `initialize` y lista **10 herramientas** (overview, search, neighbors,
get, decisions, stats, working_set, impact, history, digest_log). Luego prueba una llamada:
```bash
printf '%s\n%s\n%s\n' \
 '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05"}}' \
 '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
 '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"overview","arguments":{}}}' \
 | memorygraf mcp 2>/dev/null
```
Si tienes un cliente MCP real (Claude Desktop/Code), regístralo y pídele "ponte en contexto"
para ver el retrieval selectivo en acción.

---

## 12. Matriz de degradación elegante (crítico — es un principio vinculante §3.2)

Prueba que **quitar cada dependencia opcional NO rompe el núcleo**:

| Escenario | Cómo probar | Esperado |
|---|---|---|
| Sin `git` | `PATH` sin git, o repo no-git (`memorygraf init` en carpeta sin `.git`) + `sync` | Capa 1 se omite; resto OK |
| Sin Ollama | `MEMORYGRAF_SUMMARY_BACKEND=heuristic memorygraf sync` | resúmenes heurísticos; sin red |
| Sin `model2vec` | desinstálalo / `pip uninstall model2vec` | embeddings caen a TF-IDF; search sigue |
| Sin `tree-sitter` | desinstálalo | JS/TS caen a regex; Python intacto (usa `ast`) |
| Sin artefactos de test | `sync` sin coverage.xml/junit.xml | Capa 2 se omite (solo `tested_by` por imports) |
| Sin language-server | `memorygraf runtime --lsp` sin pyright/pylsp | se omite en silencio |

Cualquier crash o traceback en estos escenarios es un **bug de degradación** → §13.

---

## 13. Qué buscar activamente (bugs / mejoras)

- **Determinismo:** corre `memorygraf sync` dos veces sobre un grafo sin cambios; ¿la 2ª es
  incremental (index 0 cambiados)? ¿`export` (JSON) es estable entre corridas?
- **Portabilidad de rutas:** prueba en Windows nativo y en WSL. ¿Los node ids y el mapeo de
  cobertura (`<sources>`) resuelven bien con rutas absolutas y separadores `\`?
- **Multi-proyecto:** `memorygraf init --project ../otroRepo`; ¿co-cambio y `tested_by` no se
  cruzan mal entre proyectos? ¿el fallback de rutas relativas elige el proyecto correcto?
- **Incremental de la capa Git:** haz un commit nuevo y re-sincroniza; ¿el churn sube en +1
  (incremental por SHA) sin recomputar todo? Reescribe la historia (`git commit --amend`) y
  confirma que detecta y recomputa (full_rebuild).
- **Presupuesto de tokens:** ¿alguna consulta excede su `--budget`? ¿el recorte degrada bien?
- **Escala:** prueba en un repo grande (miles de archivos). ¿Tiempos de `sync`? ¿el blame por
  archivo se vuelve caro? ¿`analyze`/`report` siguen siendo rápidos?
- **Concurrencia:** corre `memorygraf watch` y edita archivos; ¿el MCP recarga en caliente
  (bump de `sync_version`) sin corromper la BD (WAL)?
- **Robustez de parsers:** dale a `digest_log` logs de otros formatos (jest, go test, mypy,
  eslint); ¿extrae algo útil o falla en silencio? (mejora potencial).
- **Unicode/encoding:** archivos con BOM, no-UTF8, nombres con espacios/acentos.

---

## 14. Reporte de resultados (entrega esto)

Rellena y devuelve:

```markdown
# Reporte E2E MemoryGraf — <fecha> — <OS/Python/versión de git>

## Resumen
- Suite unitaria: <N/N OK | fallos>
- ResourceWarning presente: <sí/no>
- Sync completo: <OK | error>  | Ollama usado: <sí(modelo)/no(heurístico)>
- MCP tools/list: <10 herramientas? sí/no>
- Benchmark total (tareas medidas): <XX%>  | ¿titular honesto?: <sí/no>

## Verificación por capa (OK / con notas / roto)
- Capa 0 (estructura/search/decisions): 
- Capa 1 (git: working_set/impact/history/co-cambio): 
- Capa 2 (runtime: cobertura/tested_by/LSP): 
- Capa 3 (compiler: digest/narrativa/rerank): 
- Fase 9 (analyze/report/confidence): 

## Matriz de degradación (§12)
- sin git / sin ollama / sin model2vec / sin tree-sitter / sin artefactos / sin LSP: 

## Bugs encontrados (más severo primero)
1. [archivo:línea] síntoma → repro exacto → esperado vs obtenido → severidad

## Mejoras propuestas
1. ...

## Cosas que se verificaron y ESTÁN bien (para dar confianza)
- ...
```

> Recordatorio: los ítems de §0 "ya diferido" **no** son bugs; verifica que degradan bien y,
> si aportan valor, propónlos como mejoras con su coste estimado. Sé honesto con los números
> (no infles el ahorro del benchmark).
