# MemoryGraf — Plan de Acción: Capas de Contexto Vivo

> **Estado:** propuesta v1.0 · **Fases 6–7 implementadas** (ver §15).
> **Fecha:** 2026-07-23.
> **Autor:** Jefferson J. Patiño Ortega (con Claude como copiloto de diseño).
> **Relación con DESIGN.md:** este documento **extiende** la visión (DESIGN §1–§17) con
> nuevas capas de conocimiento. No reemplaza los principios: los respeta (§3) y continúa
> la numeración de fases (DESIGN §11 llega hasta Fase 5; aquí empieza en Fase 6).
> **Propósito no comercial:** el objetivo es **innovar en la forma de desarrollar con
> asistentes de IA**, aportando valor real y diferenciado, no lucrar.

---

## 0. Propósito y posicionamiento (el hueco que nadie llena)

El grafo de código estático (símbolos, llamadas, imports, decisiones) ya es un problema
**resuelto y con un líder dominante** (Graphify: ~94k★, YC S26, 36 lenguajes). Competir
ahí de frente no aporta nada nuevo.

La oportunidad está en lo que **ningún competidor cubre bien hoy**:

| Herramienta | Pregunta que responde |
|---|---|
| Graphify | *"¿Qué **es** el código?"* (estructura estática) |
| memory-graph (falkordb) | *"¿Qué **aprendimos**?"* (memoria episódica **manual**) |
| **MemoryGraf (esta propuesta)** | *"¿Qué está **haciendo y en qué se está convirtiendo** el código?"* |

La tesis: **estructura + tiempo (Git) + verdad de runtime + destilación local privada.**
Eso es una **capa de contexto vivo** sobre el grafo estático — automática, determinista,
trazable y de costo-token cloud cercano a cero.

---

## 1. Principios que este plan respeta (vinculantes, DESIGN §3)

Todo lo que sigue cumple:

1. **Fuente de verdad legible** — Git, tests y tipos son fuentes objetivas; el grafo solo
   los referencia con procedencia.
2. **Portabilidad + degradación elegante** — sin Git, sin LSP o sin Ollama, cada capa se
   desactiva sola; el núcleo sigue funcionando.
3. **Retrieval selectivo, no compresión mágica** — se devuelve el subgrafo relevante compacto.
4. **El trabajo pesado vive fuera del contexto del LLM cloud** — parseo, Git, digestión de
   logs y destilación ocurren local.
5. **Trazabilidad** — cada dato lleva `archivo:línea` o `commit:hash`.
6. **Incremental** — solo se procesa lo cambiado (por hash de contenido / commits nuevos).
7. **Presupuesto de tokens explícito** — cada consulta acota su salida.
8. **Vectores y destilados LLM = caché regenerable, nunca fuente de verdad** — crítico: la
   salida del modelo local es no-determinista; siempre respaldada por la fuente cruda.
9. **Superficie de herramientas mínima** — se añaden pocas consultas MCP, ortogonales.
10. **Determinismo** — todo lo derivado de Git/AST es determinista; lo del LLM local es
    caché con `content_hash` y se regenera, nunca decide la respuesta final.

---

## 2. Arquitectura: 4 capas + el compilador local transversal

```
                         ┌─────────────────────────────────────────┐
   Fuentes locales  →    │  CAPA 3 · Compilador de contexto (LLM    │
   (código, .git,        │  local, Ollama): destila · planifica ·   │  ← transversal:
    tests, LSP, docs)    │  rerankea · digiere logs · narra "por    │    potencia TODAS
                         │  qué" · detecta drift                    │    las capas
                         └───────────────┬─────────────────────────┘
                                         │ enriquece
   ┌─────────────────────────────────────┼─────────────────────────────────────┐
   │ CAPA 0 · Estructura   │ CAPA 1 · Temporal/Git │ CAPA 2 · Verdad de runtime │
   │ (YA EXISTE)           │ (NUEVA)               │ (NUEVA)                    │
   │ símbolos, llamadas,   │ churn, working-set,   │ tipos exactos (LSP),       │
   │ imports, decisiones,  │ co-cambio, "por qué", │ cobertura y resultados de  │
   │ entidades, embeddings │ riesgo, autoría       │ tests, diagnósticos        │
   └───────────────────────┴───────────────────────┴────────────────────────────┘
                                         │ expone
                                  ┌──────┴───────┐
                                  │  Servidor MCP │  → subgrafo compacto y trazable
                                  └──────────────┘
```

**Regla de oro del plan:** cada capa es **entregable y medible por sí sola** (DESIGN §11).
No se avanza sin demostrar ahorro de tokens o mejora de calidad real.

---

## 3. CAPA 0 — Estructura (base actual, resumen)

Ya implementada (Fases 0–5). Aporta: nodos `file`/`symbol`/`entity`/`external`,
aristas `defines`/`calls`/`imports`/`depends_on`/`references`/`models`, decisiones y
convenciones desde markdown, embeddings neuronales (model2vec) y resúmenes en prosa
(Ollama efímero). Es el sustrato sobre el que se cuelgan las capas nuevas.

Módulos existentes clave: `indexer.py`, `extractors/`, `cross_link.py`, `docs.py`,
`entities.py`, `semantic.py`, `summarizer.py`, `ollama.py`, `mcp_server.py`, `viz.py`,
`store.py`, `model.py`, `query.py`, `pipeline.py`.

---

## 4. CAPA 1 — Temporal / Git

### 4.1 Objetivo y valor
Responder lo que el asistente peor resuelve hoy: *"¿qué estamos tocando ahora?"* y
*"si cambio esto, ¿qué más se ve afectado?"* — usando la historia de Git, que es
**gratis, determinista y ya está en disco**.

### 4.2 Señales y modelo de datos
**Atributos nuevos por nodo** (`file` y `symbol`):
- `churn` — nº de commits que tocaron el span del nodo.
- `last_changed` — fecha del commit más reciente.
- `age_days` — antigüedad desde su introducción.
- `fix_touches` — nº de commits con mensajes tipo `fix|bug|hotfix` (zona frágil).
- `authors` — lista corta de autores (bus factor / a quién preguntar).

**Nueva arista `co_changes_with`** (símbolo↔símbolo o file↔file):
- Peso = frecuencia de co-cambio (nº de commits donde ambos cambian / commits del nodo).
- Umbral mínimo configurable para evitar ruido.
- Marcada `INFERRED` (ver §7). **Este es el diferenciador central**: captura acoplamiento
  *real* que el AST no ve.

**Nuevo enlace nodo→commit**: top-N commits relevantes (`hash`, fecha, primera línea del
mensaje) como fuente del "por qué". No se guarda el `git log` completo (rompería el
presupuesto de tokens) — solo la referencia compacta.

### 4.3 Módulo `git_layer.py`
- Corre dentro de `pipeline.full_sync()` tras `index`.
- Usa `git log --numstat --follow` y `git blame -L` (o `git log -L` por span) por archivo.
- **Incremental**: guarda el último SHA procesado en `meta`; en cada sync solo lee commits
  nuevos y actualiza contadores.
- Mapea líneas cambiadas → nodos vía los `span_start/span_end` que ya guarda el indexador.
- Construye la matriz de co-cambio a partir de qué nodos cambian juntos por commit.

### 4.4 Nuevas consultas MCP
- `working_set` — nodos calientes: cambiados sin commitear + en los últimos N commits.
  *Reemplaza la exploración a ciegas del "¿en qué estamos?".*
- `impact(node)` — unión de **llamadas estáticas** ∪ **co-cambios** (± profundidad).
  Predice impacto mejor que el call-graph solo.
- `history(node)` — churn + edad + fragilidad + top commits (compacto).

### 4.5 Dependencias / degradación
Solo necesita el binario `git` (ya presente en casi todo entorno de dev). Sin repo Git o
sin historia → la capa se omite silenciosamente; el resto del grafo intacto.

### 4.6 Por qué ahorra tokens
El asistente deja de leer archivos completos para inferir "qué es relevante ahora" y "qué
se acopla con qué": lo obtiene en una consulta compacta. La señal de co-cambio evita
lecturas exploratorias de archivos que "por si acaso" podrían estar relacionados.

### 4.7 Riesgos
- Repos con historia reescrita/squash pierden granularidad → degradar a nivel de archivo.
- Renombrados: usar `--follow` y detección de rename de Git.
- Costo de `git log -L` en repos enormes → cachear e incrementalizar por SHA.

---

## 5. CAPA 2 — Verdad de runtime (LSP + tests + diagnósticos)

### 5.1 Objetivo y valor
Pasar de "lo que el código *parece* según el texto" a "lo que el código *es y hace* según
las herramientas que ya corren en la máquina del dev". Es la capa **menos servida** por
cualquier competidor y la de mayor valor de calidad.

### 5.2 Sub-capa A — Tipos y referencias exactos (LSP)
- Conectarse al **language server** ya instalado (pyright/tsserver/rust-analyzer/gopls) vía
  el protocolo LSP (JSON-RPC sobre stdio).
- Enriquecer nodos con: **tipo resuelto** (hover), **referencias exactas** (no heurísticas),
  **definición canónica**, y **diagnósticos actuales** (errores/warnings) mapeados al nodo.
- Ventaja: elimina que el asistente lea archivos solo para inferir tipos o buscar usos.

### 5.3 Sub-capa B — Cobertura y resultados de tests
- Parsear artefactos que ya produce el proyecto: `coverage.xml`/`.coverage`,
  `lcov.info`, JUnit XML, salida de `pytest`/`bun test`/`go test -json`.
- **Nueva arista `tested_by`** (símbolo→test) y atributos `covered` / `last_test_status`.
- Responde: *"¿es seguro cambiar esto? ¿está cubierto? ¿falló la última vez?"*.

### 5.4 Sub-capa C — Diagnósticos de build/lint
- Ingerir salida de compilador/linter y ligar cada diagnóstico a su nodo.
- El asistente arranca sabiendo qué está roto **ahora**, sin correr nada.

### 5.5 Módulos
- `runtime/lsp.py` — cliente LSP mínimo (arranca el server, consulta, cierra). Efímero como
  Ollama: se usa durante el sync y se apaga.
- `runtime/tests.py` — parsers de cobertura/resultados (por formato, degradación elegante).
- `runtime/diagnostics.py` — normaliza diagnósticos de build/lint.

### 5.6 Dependencias / degradación
Cada sub-capa es independiente: sin LSP instalado → se omite; sin artefactos de cobertura →
se omite. Nada es obligatorio.

### 5.7 Por qué ahorra tokens y sube calidad
Tipos exactos + referencias exactas + estado de tests son datos que hoy el asistente
**reconstruye leyendo y razonando** (caro y falible). Entregados pre-computados: menos
lectura, menos alucinación, decisiones más seguras.

### 5.8 Riesgos
- Arranque de LSP puede ser pesado → efímero + timeout + caché por `content_hash`.
- Variedad de formatos de test → empezar por 1–2 (pytest + JUnit) y crecer.

---

## 6. CAPA 3 — Compilador de contexto local (el "plus" del LLM local)

> **Eje transversal.** No es una capa aislada: es el modelo local (Ollama efímero, ya
> integrado) puesto a **destilar y planificar** sobre las otras tres capas, para que el
> asistente cloud reciba contexto compacto y gaste menos tokens. **Es el diferenciador que
> la competencia no tiene** (Graphify usa el LLM *del propio asistente* y paga tokens cloud;
> memory-graph no tiene LLM).

### 6.1 Principio: cognición de dos niveles (arbitraje de tokens)
- **Bibliotecario local** (modelo pequeño, gratis, privado): digiere continuamente.
- **Autor cloud** (el asistente): consume solo el destilado trazable.
- Cada unidad de comprensión pre-computada local = tokens que el cloud no paga.

### 6.2 Funciones (todas caché regenerable, nunca fuente de verdad)
1. **Destilación orientada a acción** — resúmenes "qué saber para modificar esto sin
   romper" (extiende `summarizer.py`).
2. **Planificación de retrieval** — NL → selección de nodos/aristas; el cloud recibe el
   subgrafo exacto en vez de una búsqueda difusa.
3. **Rerank local** — reordena candidatos de `search` por relevancia (mejor que coseno solo).
4. **Digestión de logs** — salida gigante de test/build → 1 línea ligada al nodo. Ataca uno
   de los mayores sumideros de tokens.
5. **Narrativa del "por qué" (Capa 1)** — cluster de mensajes de commit → una frase de
   racional. Etiqueta y **explica** las aristas `co_changes_with`.
6. **Detección de drift** — ¿el resumen/decisión sigue coincidiendo con el código?
   ¿el README contradice la implementación? Señal de calidad única.
7. **Extracción semántica de docs/PDF/imágenes local** — lo que Graphify paga con tokens
   cloud del usuario, aquí gratis y privado.

### 6.3 Módulo `context_compiler.py`
- Orquesta llamadas al modelo local (vía `ollama.py`) sobre nodos/aristas/logs.
- Cachea por `content_hash` (igual que `summarizer`); regenerable al cambiar de modelo.
- Corre en el `sync`, con el arranque efímero ya construido (huella cero entre syncs).

### 6.4 Guardarraíles (honestidad técnica)
- El modelo local **destila y planifica; no razona la respuesta final**.
- Toda salida lleva procedencia para que el cloud verifique contra la fuente.
- No-determinismo → tratado como caché con hash, nunca como verdad canónica (§3.8/§3.10).

### 6.5 Por qué es el mayor diferenciador
Convierte "entender el proyecto" en un proceso **de fondo, gratis y privado**. La
competencia, o no tiene modelo local, o gasta tokens cloud del usuario para lo mismo.

---

## 7. Adopciones puntuales de Graphify (baratas, en paralelo)

- **Etiquetas de confianza en aristas**: `EXTRACTED` (explícita: import/llamada) /
  `INFERRED` (deducida: co-cambio, LSP secundario) / `AMBIGUOUS` (revisar). Mejora la
  trazabilidad (§3.5) y encaja natural con `co_changes_with` = `INFERRED`.
- **Detección de "god nodes"/anomalías** en un paso `analyze()`: métricas de grafo simples
  (grado, centralidad) → señal de riesgo arquitectónico.
- **`GRAPH_REPORT.md`**: reporte markdown generado (complemento a `graph.html` de `viz.py`);
  artefacto compartible y revisable en PRs.
- **No adoptar**: clustering Leiden sin embeddings — MemoryGraf ya apostó por embeddings;
  es una apuesta coherente y distinta. No dispersarse.

---

## 8. Modelo de datos consolidado (cambios)

**Nuevos tipos de arista:** `co_changes_with`, `tested_by`.
**Nuevos atributos de nodo:** `churn`, `last_changed`, `age_days`, `fix_touches`,
`authors`, `resolved_type`, `covered`, `last_test_status`, `diagnostics`.
**Atributo en todas las aristas:** `confidence` ∈ {EXTRACTED, INFERRED, AMBIGUOUS}.
**Nuevos enlaces nodo→commit** (referencia compacta).
Todo se persiste en `store.py` (SQLite + JSON regenerables entre sí, DESIGN §7) y es
**regenerable** desde las fuentes (`.git`, artefactos de test, LSP).

---

## 9. Superficie MCP final (mínima y ortogonal, §3.9)

Consultas nuevas (pocas, de alto valor):
- `working_set` — qué se está tocando ahora.
- `impact(node)` — llamadas ∪ co-cambios (predicción de impacto).
- `history(node)` — churn + fragilidad + "por qué" compacto.
- (Las existentes `overview/search/neighbors/get/decisions/stats` heredan los nuevos
  atributos sin crecer en número de herramientas.)

---

## 10. Roadmap por fases (cada una entregable y medible)

| Fase | Nombre | Entregable | Métrica de éxito |
|---|---|---|---|
| **6** | Temporal/Git — núcleo | `git_layer.py`: `churn`, `last_changed`, `working_set`, `co_changes_with`, `impact()` | Demo: `impact()` predice archivos afectados que el call-graph solo no capturó; `working_set` responde "¿en qué estamos?" sin exploración |
| **7** | Compilador local | `context_compiler.py`: rerank local, digestión de logs, narrativa "por qué", etiquetas de co-cambio | Benchmark: tokens del asistente **con vs sin** la capa, en 3 tareas reales |
| **8** | Verdad de runtime | `runtime/tests.py` (cobertura + resultados, `tested_by`) → luego `runtime/lsp.py` (tipos/refs/diagnósticos) | Demo: el asistente decide "seguro de cambiar" con datos de test/tipos, sin leer archivos |
| **9** | Adopciones + reporte | `confidence` en aristas, `analyze()`/god-nodes, `GRAPH_REPORT.md` | Reporte legible generado; aristas trazables por confianza |

**Orden y razón:** empezar por **Fase 6** (barata, determinista, gratis, y el co-cambio es
lo más difícil de copiar rápido). **Fase 7** multiplica el valor de todo con costo-token
cero. **Fase 8** es la de mayor calidad pero más esfuerzo/menos portable → después. **Fase
9** pule y hace demostrable.

---

## 11. Métricas de éxito (cómo se prueba el valor)

- **Ahorro de tokens**: en N tareas reales, tokens que el asistente gasta **con** MemoryGraf
  (capas 1–3) vs **sin** ella. Objetivo: reducción sustancial y reproducible (un
  `benchmark.py` estilo Graphify: corpus vs subgrafo).
- **Calidad de impacto**: % de archivos realmente afectados por un cambio que `impact()`
  predice, vs el call-graph estático solo. El co-cambio debe subir el recall.
- **Cero costo cloud de comprensión**: la destilación/planificación no consume tokens del
  asistente (todo local).
- **Degradación**: en un entorno sin Git/LSP/Ollama, el sistema sigue dando la Capa 0 sin
  errores.

---

## 12. No-objetivos (disciplina de alcance)

- **No** perseguir cobertura de 36 lenguajes ni 17 asistentes: la ventaja es **profundidad**
  en las capas nuevas, no amplitud.
- **No** convertirlo en memoria episódica manual (el terreno de memory-graph): la historia
  se deriva de Git, automáticamente.
- **No** hacer que el LLM local razone la respuesta final: solo destila y planifica.
- **No** romper portabilidad: toda capa nueva es opcional con degradación elegante.

---

## 13. Riesgos transversales y mitigaciones

| Riesgo | Mitigación |
|---|---|
| No-determinismo del LLM local | Caché por `content_hash`, tratado como pista, nunca fuente de verdad; siempre procedencia |
| Costo de sync (Git+LSP+LLM) | Todo incremental + efímero + cacheado; solo se re-procesa lo cambiado |
| Complejidad creciente vs. simplicidad (tu ventaja) | Fases independientes; cada una debe justificar su valor antes de la siguiente |
| Alcance que se dispersa | Los no-objetivos de §12 son vinculantes |
| Ruido en co-cambio | Umbral mínimo + ventana temporal + etiqueta `INFERRED` |

---

## 14. Resumen ejecutivo del diferenciador

MemoryGraf deja de ser "otro grafo de código" para convertirse en la **capa de contexto
vivo**: el único que combina **estructura + historia real (Git) + verdad de runtime**,
todo **pre-compilado por un LLM local privado y gratuito**. Es automático (sin captura
manual), trazable, portable y de costo-token cloud cercano a cero — un ángulo que ni el
líder del mercado ni la alternativa de memoria manual ocupan hoy.

---

## 15. Estado de implementación

### Fase 6 — Temporal/Git ✅ (2026-07-23)

Entregado en `memorygraf/git_layer.py`, integrado en `pipeline.full_sync()` tras `index`.
Todo es **caché regenerable desde `.git`** (nunca fuente de verdad), determinista,
incremental y con degradación elegante (sin `git`/repo la capa se omite en silencio).

**Señales por nodo** (`file` y `symbol`), en tablas nuevas de `store.py` (`git_node`,
`git_commits`, `git_cochange`, `git_blame` — todas regenerables):
- `churn`, `first_changed`/`last_changed`, `age_days`, `fix_touches`, `authors`.
- Nivel **archivo**: recorrido de commits (`git log --numstat`), **incremental por SHA**
  (`meta git_head_sha:<proj>`); solo lee commits nuevos. Historia reescrita → recompute total.
- Nivel **símbolo**: `git blame --line-porcelain` del archivo actual, **cacheado por
  `content_hash`**; mapea el span del símbolo a sus commits → atribución exacta al código de hoy.

**Arista nueva** `co_changes_with` (file↔file, `EDGE_CO_CHANGES` en `model.py`): acoplamiento
real que el AST no ve. Peso = `co / min(churn_a, churn_b)`; umbrales `min_cochange` y
`cochange_threshold`; commits "barredera" (> `cochange_max_files`) no cuentan. `provenance="git-cochange"`.

**Consultas nuevas** (MCP + CLI, en `query.py`):
- `working_set` — archivos sin commitear + cambiados recientemente ("¿en qué estamos?").
- `impact(node[, depth])` — *blast radius*: quién depende del nodo (aristas entrantes:
  calls/imports) **∪** co-cambio (Git). Predice impacto que el call-graph solo no ve.
- `history(node)` — churn + fragilidad (fix) + edad + autores + top commits (el "por qué",
  con procedencia `commit:hash`). `get` también hereda una línea `git:` resumida.

**Config** (bloque `git` en `config.json`, opcional): `enabled`, `min_cochange`,
`cochange_threshold`, `cochange_max_files`, `top_commits`, `max_authors`.

**Pruebas**: `tests/test_memorygraf.py::TestGitLayer` (repo git real efímero) cubre churn/autores,
co-cambio, `impact` con co-cambio, blame de símbolos, `working_set`, incremental y degradación
sin git. Suite completa: 26/26 en verde.

**Nota de campo** (repo actual, ~3 commits no-merge): la señal de co-cambio ya emerge
(6 pares: `cli/pipeline/summarizer/test`, acoplados por el feature de resúmenes Ollama) y
crecerá con la historia. Los imports intra-paquete son relativos (`from .x import`) y el
extractor aún no los resuelve a aristas internas → el co-cambio es justo lo que compensa ese
hueco. Resolver imports relativos queda como mejora del extractor (fuera de Fase 6).

### Fase 7 — Compilador de contexto local ✅ (2026-07-23)

Entregado en `memorygraf/context_compiler.py` — el "bibliotecario local" que destila y
planifica sobre las otras capas. **Guardarraíles §6.4 (vinculantes)**: el LLM local solo
destila/planifica, nunca razona la respuesta final; toda salida lleva procedencia; el
no-determinismo se trata como **caché por `content_hash`** (`ctx_note`), nunca fuente de
verdad. Sin Ollama, todo degrada a un **heurístico determinista** (DESIGN §3.2).

**A. Digestión de logs** (`digest_log`, el mayor sumidero de tokens §6.2.4): extracción
determinista de fallos (tracebacks Python, `FAILED`/`ERROR` de pytest, resumen `N failed`),
ligados a su `archivo:línea` del grafo; el LLM local, si está, añade una línea de "situación"
(no inventa: pule). Expuesto en CLI `digest [log] [--llm]` y MCP `digest_log`.

**B. Narrativa del "por qué" del co-cambio** (`compile_cochange_notes`): etiqueta cada arista
`co_changes_with` con una frase del porqué, derivada de los asuntos de commit compartidos.
Heurístico por defecto (tema + evidencia); LLM opt-in. Cacheada en `ctx_note(kind='cochange')`
por hash de los asuntos. La consumen `impact()` (línea `↳`) e `history()`.

**C. Rerank local** (`rerank`): reordena candidatos de `search` combinando señal léxica +
estructura + "calor" (churn) de la capa Git. **Determinista y sin latencia de LLM** en el
camino caliente (respeta §6.4). *Diferido*: rerank-LLM en tiempo de consulta (compromiso
latencia/calidad a estudiar con el benchmark de §11).

**Integración/coste** (DESIGN §11): el `sync` narra el co-cambio de forma barata —
`compiler.backend=auto` usa el heurístico (no arranca el modelo); el LLM en el sync es opt-in
(`compiler.backend=ollama`). La digestión de logs es on-demand (su entrada es transitoria).
Config: bloque `compiler` (`enabled`, `backend`, `model`, `manage`, `max_log_findings`).

**Pruebas**: `TestContextCompiler` (digestión traceback/pytest, log limpio, rerank) y
`TestCompilerCochange` (narrativa heurística + surfacing en `impact`, caché por hash).
Suite completa: 32/32 en verde. Validado en vivo: `impact` de `cli.py` muestra el porqué del
acoplamiento con `pipeline/summarizer/test` (feature de resúmenes Ollama), con procedencia.

### Pendiente del roadmap
- **Fase 8** (verdad de runtime: tests/cobertura + LSP) y **Fase 9** (confidence en aristas,
  `analyze()`/god-nodes, `GRAPH_REPORT.md`): sin cambios respecto a §10.
- **Benchmark de tokens** (§11) ✅: `benchmark.py` (determinista, offline, estilo Graphify:
  corpus vs subgrafo). En este repo mide **~91% de ahorro agregado** (leer docs/archivos
  completos + vecinos vs subgrafo dirigido de consultas), con desglose por tarea
  (onboarding, impacto/entender, localizar, triage de logs).
