# MemoryGraf — Deuda consciente y validación pendiente

> **Estado (2026-07-27).** El backlog **M1–M10 está IMPLEMENTADO y probado**, más **M11a**
> (capa LSP para Go/Rust/C/C++). La fuente de
> verdad es el **código + los tests (`tests/test_memorygraf.py`) + el historial de commits**;
> este documento registra: **(a)** lo hecho, **(b)** la **deuda consciente** que conviene
> recordar (por qué algo va gateado/acotado), **(c)** las propuestas abiertas (M11b–M12 y el
> **Anexo M13–M20**, roadmap de paridad con Graphify) y **(d)** lo único pendiente de
> ejecución sin código nuevo: la **validación de ENTORNO** (§9).
> Complementa `DESIGN.md` (principios §3, vinculantes).

---

## 1. Backlog M1–M10 — implementado

| # | Mejora | Dónde / nota |
|---|---|---|
| M1 | Co-cambio por símbolo con **historia completa** | on-demand `impact --deep` + **full-repo opt-in** (`git.symbol_cochange_full`, acotado). Ver §2. |
| M2 | `tested_by` **símbolo→test** (contextos de cobertura) | `runtime/tests.py` |
| M3 | Narrar el "por qué" del co-cambio de **símbolos** | `context_compiler` |
| M4 / M4b | `resolved_type` multi-lenguaje + **params** (Py y TS/JS) + **variables locales** (Py, opt-in `runtime.local_var_types`) | `runtime/lsp.py` |
| M5 | `digest`: formatos **agrupados** (eslint/jest/go test/tsc) | `context_compiler` |
| M6 | `git blame` **paralelo** (lectura) en repos grandes | `git_layer` |
| M7 | Narrativa/rerank con **LLM local** opt-in (Ollama) | `context_compiler` |
| M8 | Co-cambio **cross-project** por símbolo, gateado y conservador | `git_layer` |
| M9 | `calls`/`imports` **cross-file** multi-lenguaje + capa LLM opcional | `ts_generic` + `indexer`. Ver §3. |
| M10 | **Bootstrap de entidades** de dominio (`bootstrap-entities`): propone desde el código para curar | `entities_bootstrap`. Ver §M10. |

Cada mejora entró con **test de regresión** y verificación en vivo (regla DESIGN §11).

---

## 2. Deuda consciente — por qué M1 full-repo va OPT-IN (medición)

El co-cambio por símbolo del `sync` sale del **blame** (atribuye cada línea a su ÚLTIMO
commit): un acoplamiento "de superficie". Si dos símbolos se co-editaron en commits viejos
cuyas líneas luego se reescribieron, esa señal se pierde. El **full-repo**
(`git_layer._full_symbol_cochange`, provenance `git-cochange-sym-full`) la recupera
re-extrayendo la versión histórica de cada commit — pero **cuesta ~14-23×** el blame
(medido antes de integrar con un prototipo aislado, ya retirado: 20 archivos×30 commits ≈ 14×,
50×50 ≈ 23×; domina `git show`+re-AST por cada (commit, archivo)).

**Por eso va OPT-IN** (`git.symbol_cochange_full=false` por defecto) y **acotado por
profundidad** (`git.cochange_full_depth`). Es aditivo (no borra el blame) y recomputa contra
los símbolos vigentes (sin ids obsoletos). Para bajar aún más el coste en el futuro:
acumulador incremental por SHA persistido + batching de `git show`/AST. El on-demand
(`impact --deep`, acotado al historial del archivo) cubre el caso puntual sin coste global.

---

## 3. M9 — matriz de cobertura cross-file (determinista, precisión-primero)

Solo enlaza si el destino es **INEQUÍVOCO** → sin aristas falsas (tests de precisión por
lenguaje). Encima, capa **LLM opcional** (`resolver.llm` / `MEMORYGRAF_XLINK_LLM`) que
desempata candidatos ambiguos (confidence 0.55 + provenance `llm`, fallback determinista).

| Lenguaje | imports | calls | Mecanismo |
|---|:--:|:--:|---|
| Python, JS/TS | ✅ | ✅ | `ast` / `ts_treesitter` (base histórica) |
| Java | ✅ | ✅ | paquete `a.b.C` → `a/b/C` |
| C / C++ | ✅ | ✅ | `#include` relativo; call libre → stem del include, nombre único |
| Rust | ✅ | ✅ | `mod x;` → hermano; `path::f()` |
| Go | ✅ | ✅ | `go.mod` (prefijo de módulo) → dir de paquete |
| PHP | ✅ | ✅ | PSR-4 (`composer.json`); `Clase::m()` |
| R | ✅ | ✅ | `source("path")`; call libre por nombre único |
| C# / VB | — (namespace) | ✅ | índice namespace→archivos; `Receptor.m()` cualificado |
| Assembly | — | intra ✅ | `call`/salto → etiqueta (whitelist de mnemónicos) |

---

## M10 · Bootstrap de entidades de dominio con LLM local  ✅ IMPLEMENTADO

**Estado (2026-07-26).** Hecho. Comando `memorygraf bootstrap-entities`
(`entities_bootstrap.py`): propone entidades candidatas desde las clases/tipos del grafo
(heurístico determinista por sustantivo de dominio; refuerzo con LLM local si Ollama está,
fallback si no), el usuario las cura (aceptar/editar/omitir) y escribe `memorygraf.entities.json`
(fusiona con el existente). CLI con `-h` detallado + MANUAL. Tests: heurístico por sustantivo,
LLM mockeado, escritura del glosario curado, requiere config. Validado E2E: bootstrap → `sync`
crea nodos `entity` + aristas `models`. El resto de esta sección queda como registro del plan.

**Contexto.** El glosario de dominio (`memorygraf.entities.json` → nodos `entity` + aristas
`models`, Fase 4) es opt-in y su **contenido se redacta a mano** (es conocimiento de negocio;
autocopiarlo del `.example` metería entidades falsas, ya que aliases genéricos como
`user`/`order` matchearían código no relacionado). Se autodetecta si existe, pero un usuario
nuevo rara vez sabe que existe o se anima a escribirlo desde cero → la capa de dominio queda
infrautilizada.

**Propuesta.** Un comando **interactivo** `memorygraf bootstrap-entities` (nombre tentativo),
friendly como `configure`/`doctor`, que:
1. Lee el grafo YA sincronizado (símbolos/archivos + sus nombres/rutas).
2. Con el **LLM LOCAL** (Ollama, opt-in; sin él, fallback heurístico: agrupación por tokens
   frecuentes en identificadores) **PROPONE** entidades candidatas + aliases desde el código.
3. Presenta cada candidata para que el usuario la **CURE** (aceptar / editar / descartar). El
   humano es la fuente de verdad; el LLM solo sugiere (guardarraíl §6.4, DESIGN).
4. Escribe `memorygraf.entities.json` con lo curado. Idempotente (re-ejecutable, fusiona sin
   duplicar). El siguiente `sync` crea los nodos `entity` + aristas `models`.
5. Documentado en `--help` (descripción + ejemplos) y en el MANUAL (`memorygraf -h`).

**Pruebas post-implementación.**
- Con LLM mockeado: propone candidatas desde un grafo de juguete; aceptar 1 → se escribe el
  glosario y el `sync` posterior crea `entity`/`models`. Descartar no la escribe.
- Fallback sin Ollama: agrupación heurística determinista (no rompe, degradación elegante).
- Idempotencia: re-ejecutar fusiona sin duplicar; procedencia (cada candidata cita símbolos
  reales, nada inventado).

**Riesgo/Esfuerzo.** Medio. El LLM propone pero NO es canónico (curación humana obligatoria);
sin Ollama degrada a heurístico. Contenido a un módulo nuevo + subcomando (patrón `configure`).

---

## M11 · Ampliar la capa LSP a más lenguajes  (M11a ✅ · resto PROPUESTA)

**Estado (2026-07-27).** **M11a (Grupo A) IMPLEMENTADO:** `runtime/lsp.py` (`_LANGUAGES`) ya
cubre LSP para **Python**, **TypeScript/JS** y ahora **Go (`gopls`)**, **Rust
(`rust-analyzer`)** y **C/C++ (`clangd`)** — diagnósticos + `resolved_type` vía hover, reusando
el cliente efímero. Su language-server es toolchain/OS-específico, así que **no se auto-instala**:
`doctor` lo detecta (`shutil.which`) y muestra el comando correcto por plataforma
(`go install …` / `rustup component add …` / `apt`·`brew`·`winget` para clangd). Tests en
`tests/test_memorygraf.py` (`TestGroupALspHints`, mapeo de extensiones, reporte por lenguaje).
El resto que MemoryGraf **sí indexa** con tree-sitter (Java, C#, PHP, R, VB, asm) sigue con
**símbolos/`calls`/`imports`** pero **sin** capa LSP (`doctor`/`configure` lo reportan como
"indexado, SIN capa LSP"). No es imposibilidad: es alcance de M11b–d.

**Qué exige añadir un lenguaje** (el cliente LSP efímero ya es genérico y reutilizable):
1. Una entrada en `_LANGUAGES`: `name`, `servers` (binario + args por stdio), `ext_lang`
   (ext→languageId). Con solo eso salen **diagnósticos + `resolved_type`** (hover).
2. Que el server sea **detectable/instalable desde `doctor`** (patrón `ts-lsp`: `shutil.which`
   + comando por gestor; el wrap `cmd /c` de Windows ya generaliza a cualquier `.cmd`).
3. *(Opcional, M4b)* un **provider de offsets de params** (tipo `ts_treesitter.param_offsets`)
   para `param_types` por posición — follow-up por lenguaje, no bloquea lo básico.

**¿Todo en una sola corrida? Depende del server** — por eso conviene agrupar:

- **Grupo A — UNA sola corrida (config-only, reusa el cliente tal cual):** servers LSP
  estándar por stdio, single-binary, sin init especial → **Go (`gopls`)**, **Rust
  (`rust-analyzer`)**, **C/C++ (`clangd`)**. Enchufarlos en `_LANGUAGES` + `doctor` ya da
  diagnósticos + `resolved_type`. Riesgo **bajo**.
- **Grupo B — por lenguaje (launch/init server-específico):** **Java (`jdtls`)** exige
  workspace dedicado + JVM + `initializationOptions`; **C# (`OmniSharp`/`csharp-ls`)** es
  solución/proyecto-aware. Cada uno es su propio PR (el cliente necesita init a medida).
- **Grupo C — nicho / parcial:** **PHP (`intelephense` [node] o `phpactor`)**, **R
  (`languageserver`)** — factibles, menor demanda. **VB** y **Assembly** no tienen LSP
  standalone práctico → se quedan **symbols-only** permanente (marcarlo así en `doctor`).

**Orden sugerido:** ~~M11a = Grupo A (Go/Rust/C/C++) en una corrida~~ ✅ hecho · **siguiente:**
M11b = Java · M11c = C# · M11d = PHP/R. Cada sub-hito con su **instalable/detectable en `doctor`**
(respetando entorno/OS, como
`ts-lsp`) y su **test** (fixture mínimo: un símbolo tipado + un error → el server devuelve
`resolved_type` y ≥1 diagnóstico; skip limpio si el server no está, como hoy).

**Riesgo/Esfuerzo.** Grupo A: **bajo** (config + tests, sin motor nuevo). Grupo B: **medio-alto**
(init por server). Reutiliza todo el andamiaje existente (cliente efímero, mapeo a `runtime_node`,
degradación elegante): el grueso es "conectar server + instalable + test", no lógica nueva.

---

## M12 · Respetar `.gitignore` en el indexado  (PROPUESTA)

**Estado (2026-07-26).** Propuesta. Hoy el descubrimiento de archivos (`indexer._iter_files`)
filtra por **`DEFAULT_EXCLUDES` + `config.excludes`** (nombres de directorio), pero **NO
consulta `.gitignore`**. MemoryGraf mira el **disco**, no el índice de git: por eso un dir
gitignorado como `.next/` (build de Next.js) se indexaba igual hasta añadirlo a los defaults
(ver commit del fix; en un Next.js real eran 476/993 nodos = 48% de ruido generado).

**Problema.** `DEFAULT_EXCLUDES` es una **lista fija** que persigue frameworks uno a uno
(`.next`, `.nuxt`, `.turbo`, …). Siempre llegará tarde al siguiente framework/dir generado.
El propio proyecto **ya declara** qué NO es código fuente en su `.gitignore` — esa es la
fuente de verdad natural para "esto es generado/local, no lo indexes".

**Propuesta.** Filtrar también por `.gitignore` durante el descubrimiento, con degradación
elegante y sin romper el modo portable:
1. **Si hay git** (ya es dependencia opcional de la capa temporal): resolver los archivos
   ignorados con `git check-ignore --stdin` (batch, rápido) o `git ls-files` sobre los
   candidatos. Cero parsing propio de patrones → fidelidad total (negaciones `!`, `**`,
   anclajes, `.gitignore` anidados) sin reimplementar la semántica de git.
2. **Sin git / sin repo:** se mantiene el comportamiento actual (`DEFAULT_EXCLUDES` +
   `config.excludes`). Nada se rompe (regla DESIGN §3.2).
3. **Escape hatch:** algunos proyectos tienen **código fuente legítimo gitignorado** (módulos
   locales, generado-que-sí-es-código). Config `index.respect_gitignore` (evaluar default:
   `true` cuando hay git, pero reversible) + posibilidad de "des-ignorar" por `config` para
   volver a incluir rutas concretas.

**Interacción con lo existente.** `DEFAULT_EXCLUDES` se queda (cubre el caso sin git y da un
mínimo sano); `.gitignore` es una capa ADICIONAL, no un reemplazo. Orden: excludes de dir
(barato, poda ramas del `os.walk`) → luego filtro gitignore sobre los archivos que sobrevivan.

**Pruebas.** Fixture con repo git: un `.ts` fuente + un `build/generado.js` gitignorado →
el fuente entra, el gitignorado no. Sin git: cae a `DEFAULT_EXCLUDES` (el gitignorado entra
salvo que su dir esté en la lista). `respect_gitignore=false` → comportamiento previo.
Negación (`!keep.js`) respetada vía `git check-ignore`.

**Riesgo/Esfuerzo.** Bajo-medio. Sin parser propio (delega en git) el riesgo baja mucho; el
grueso es cablear el filtro en `_iter_files` con batch a git y el flag de config + su default.

---

## Anexo · Roadmap de paridad con Graphify (M13–M20)  (PROPUESTA)

**Estado (2026-07-27).** Propuesta. Nace de comparar MemoryGraf con
[graphify](https://github.com/Graphify-Labs/graphify) (v0.9.27, ~113k LOC, 36 gramáticas
tree-sitter, 20+ hosts) y quedarse **solo con lo que encaja en DESIGN §3**. Parte del
trabajo ya está hecho: `confidence.py` (EXTRACTED/INFERRED/AMBIGUOUS), `report.py`
(GRAPH_REPORT.md), `analyze.py` (god nodes) y `viz.py` (graph.html) ya son adopciones de
Graphify. Este anexo cubre **lo que aún falta**.

**Criterio de selección.** No se copia por copiar. Cada hito debe: (a) respetar el núcleo
**stdlib** con degradación elegante (§3.2), (b) ser **determinista** (§3.10), (c) dejar
**procedencia** (§3.5) y (d) **no inflar la superficie MCP** (§3.9) — casi todo entra por
CLI o enriqueciendo tools existentes, no como tool nueva. Lo que no cumple está en
**§NO adoptar** al final, con el porqué.

### Prioridad sugerida

| # | Hito | Brecha que cierra | Esfuerzo | Valor |
|---|---|---|---|:--:|
| **M13** | Comunidades / subsistemas | La única capacidad **analítica** que Graphify tiene y MemoryGraf no | Medio | **Alto** |
| **M14** | `path A B` — cómo se conectan dos nodos | Consulta que hoy no existe (hay `neighbors`, no camino) | **Bajo** | **Alto** |
| **M15** | Ampliar matriz de gramáticas (~12 → ~30) | 36 gramáticas vs ~12; hoy MemoryGraf no sirve en repos Kotlin/Swift/Ruby | **Bajo** | **Alto** |
| **M16** | Instaladores multi-host | `install claude` es el único; el resto es copiar JSON a mano | **Bajo** | **Alto** |
| **M17** | Always-on: hooks de git + instrucciones de agente | El grafo se queda obsoleto y el asistente no lo usa por su cuenta | Bajo-medio | **Alto** |
| **M18** | Exportadores (GraphML / Cypher / Obsidian / wiki) | Hoy solo JSON + HTML | Bajo | Medio |
| **M19** | Ingesta de docs no-markdown (PDF / Office) | Decisiones solo desde `.md` | Medio | Medio |
| **M20** | Benchmark reproducible y público | El "~21×" sale de UNA tarea, no de un harness | Medio | Medio |

M14 + M15 + M16 son el paquete de mayor retorno por esfuerzo: los tres son **tabla de
configuración + tests**, sin motor nuevo.

---

### M13 · Comunidades / subsistemas del grafo  (PROPUESTA)

**Contexto.** `analyze.py` ya da **god nodes** y hotspots, que responden "¿qué es crítico?".
Lo que no responde nadie hoy es **"¿en cuántos subsistemas se divide esto y cuáles son?"**.
Graphify particiona el grafo con Leiden y etiqueta cada comunidad; es lo que hace que su
`graph.html` y su reporte se lean como un mapa de arquitectura y no como una maraña. Para
un asistente aterrizando en un repo desconocido, "este proyecto son 7 subsistemas y tocas
el de auth" es orientación más barata que cualquier lista de símbolos.

**Propuesta.**
1. **Núcleo stdlib, determinista**: partición por modularidad estilo Louvain implementada
   a mano (~150 líneas, sin dependencias) sobre las aristas estructurales que ya usa
   `analyze._STRUCTURAL`. **El determinismo es el requisito duro** (§3.10): Louvain es
   sensible al orden de visita, así que el recorrido se hace sobre nodos **ordenados por
   id** y con desempates explícitos — misma entrada, misma partición, siempre.
2. **Extra opcional `cluster`** (`graspologic`, patrón de `parsers`/`neural`): habilita
   **Leiden** (mejor calidad, sin comunidades mal conectadas). Si falta → Louvain propio.
   Degradación elegante, nunca error.
3. **Etiquetado sin LLM por defecto**: nombre de comunidad = token dominante en las rutas
   y nombres de símbolo de sus miembros (`auth`, `payments/checkout`), con procedencia
   (los 3 nodos que más pesan en el nombre). Con Ollama disponible y opt-in, prosa mejor
   — mismo patrón que `summarize`, con fallback determinista.
4. **Superficie**: `memorygraf communities` en CLI; en MCP **no se añade tool nueva** —
   `overview` gana una sección "subsistemas" y `get`/`neighbors` devuelven el campo
   `community` del nodo. Así el asistente lo recibe sin pagar esquema extra (§3.9).
5. **Consumidores inmediatos**: `viz.py` colorea por comunidad (hoy colorea por tipo),
   `report.py` añade la sección, e `impact` puede marcar "el blast radius cruza 3
   subsistemas" — señal de riesgo que hoy se pierde.

**Pruebas.** Grafo de juguete con dos clusters separados por una sola arista → dos
comunidades, la arista puente marcada como cross-community. **Determinismo**: 20 corridas
sobre el mismo grafo → partición idéntica (y con los ids barajados de entrada, también).
Con `graspologic` ausente → cae a Louvain sin fallar. Grafo vacío / un solo nodo → no
revienta.

**Riesgo/Esfuerzo.** Medio. El riesgo real no es el algoritmo (es conocido y corto), es el
**determinismo bajo reordenación** y que las etiquetas heurísticas no salgan basura en
repos con nombres pobres. Mitigación: si la etiqueta no supera un umbral de señal, se
queda `Subsistema N` en vez de inventar un nombre malo.

---

### M14 · `path A B` — cómo se conectan dos nodos  (PROPUESTA)

**Contexto.** MemoryGraf responde "¿qué hay alrededor de X?" (`neighbors`) y "¿qué rompo si
toco X?" (`impact`), pero **no** "¿qué relación hay entre X e Y?". Es la consulta que más
usa un agente cuando sospecha un acoplamiento y quiere confirmarlo antes de editar. En
Graphify es `graphify path A B` y devuelve el camino salto a salto.

**Propuesta.** BFS bidireccional sobre el grafo ya cargado — **stdlib pura, sin
dependencias, sin motor nuevo**:
- `memorygraf path <A> <B> [--max-hops N] [--via calls,imports] [--all K]`: camino más
  corto, con **el tipo y la procedencia de cada salto** (§3.5) y su etiqueta de confianza
  (`confidence.py` ya la deriva) — así se distingue un camino real de uno sostenido por
  aristas `INFERRED` débiles.
- `--via` filtra por tipo de arista: el camino "solo por `calls`" y el camino "incluyendo
  `co_changes`" cuentan historias distintas y ambas interesan.
- Sin camino → decirlo, y ofrecer el nodo más cercano a ambos extremos (no dejar al
  asistente sin siguiente paso).
- **Presupuesto de tokens** respetado (§3.7): cortar a K caminos y N saltos.

**Superficie MCP.** Esta **sí** merece tool nueva (`path`): es ortogonal a las 10 actuales
y no se emula con ninguna combinación de ellas. Sería la única alta del anexo — 11 tools
sigue siendo superficie chica (§3.9).

**Pruebas.** Fixture con cadena `a → b → c`: `path a c` = 2 saltos con tipos correctos.
Nodos desconectados → "sin camino" limpio. `--via calls` excluye un camino que solo existía
por `co_changes`. Determinismo: empate entre dos caminos de igual longitud → se resuelve
por orden de id, estable entre corridas.

**Riesgo/Esfuerzo.** **Bajo.** Es el mejor valor/esfuerzo de todo el anexo: algoritmo
trivial, datos ya en memoria, cero dependencias.

---

### M15 · Ampliar la matriz de gramáticas (~12 → ~30 lenguajes)  (PROPUESTA)

**Contexto.** Hoy `ts_generic._GRAMMAR_BY_EXT` cubre C, C++, Java, C#, Go, Rust, PHP, R, VB
y asm (más Python por `ast` y JS/TS por `ts_treesitter`). Graphify cubre 36 gramáticas.
La consecuencia práctica es dura: **en un repo Kotlin, Swift, Ruby, Elixir o Dart,
MemoryGraf no indexa nada** y el usuario no tiene razón para instalarlo.

**Lo importante: el coste es bajo y ya está pagado.** `tree-sitter-language-pack` ya es
dependencia del extra `parsers`, y trae esas gramáticas. Añadir un lenguaje es **rellenar
tablas**, no escribir un motor:
1. `_GRAMMAR_BY_EXT`: extensión → gramática.
2. `_SPEC`: qué tipos de nodo son `func` / `type` / `klass` / `prefix_only` / `scope`.
3. `_CALLS` y `_IMPORT_NODES`: nodos de llamada e import (opcional; sin ellos salen
   símbolos + `defines`, que ya es útil).
4. Regla de resolución cross-file en el `indexer` (M9), **solo si es inequívoca** — si no
   lo es, el lenguaje entra como **symbols-only** y se declara así en `doctor`.

**Orden sugerido** (por demanda y por lo limpio que sale el cross-file):
- **M15a — alta demanda, resolución clara:** **Ruby** (`require_relative`), **Kotlin**
  (paquete → ruta, igual que Java), **Swift** (módulo por target), **Dart**
  (`import 'package:'`). 
- **M15b — alta demanda, cross-file ambiguo:** **Scala**, **Lua**, **Elixir**, **Bash**.
  Entran symbols-only + `calls` intra-archivo; cross-file solo si sale inequívoco.
- **M15c — infra/datos:** **SQL**, **Terraform/HCL**, **PowerShell**, **JSON de manifiestos**
  (`package.json`, `pyproject.toml`, `go.mod` → nodos de paquete + `depends_on`, que hoy
  no existen y son de los más consultados en onboarding).

**Regla que no se negocia:** M9 es **precisión-primero**. Un lenguaje nuevo **nunca** entra
con aristas cross-file adivinadas; antes symbols-only que un grafo con enlaces falsos, que
es peor que no tener grafo (hace al asistente estar seguro y equivocado).

**Pruebas.** Por lenguaje, el patrón M9 ya existente: fixture mínimo con dos archivos →
símbolos esperados, `calls`/`imports` esperados, y **test de precisión** (un caso ambiguo
que NO debe generar arista). Skip limpio si la gramática no está instalada.

**Riesgo/Esfuerzo.** **Bajo por lenguaje**, pero se multiplica: presupuestar ~1 corrida por
grupo, no por lenguaje. Riesgo acotado — si una gramática falla, ese lenguaje se omite y el
resto del `sync` sigue (degradación ya implementada).

---

### M16 · Instaladores multi-host  (PROPUESTA)

**Contexto.** `memorygraf install claude` acepta `choices=["claude"]` — un solo host.
Para los demás hay `mcp-config`, que imprime JSON para **pegar a mano**. Graphify tiene
instalador dedicado para 20+ hosts, y esa es una razón enorme de su adopción: la fricción
del primer minuto decide si la herramienta se usa o se abandona.

**Propuesta.** Extender el subcomando con el mismo patrón ya probado en `install claude`
(escribir config en la ruta correcta, respetar `--scope project|user`, idempotente):
- **MCP nativo** (los que hablan MCP: **Cursor**, **Codex**, **Gemini CLI**, **VS Code /
  Copilot**, **Windsurf**, **OpenCode**): cada uno solo cambia **la ruta del archivo y la
  forma del JSON**. Es una tabla `host → (ruta, plantilla)`, no lógica nueva.
- **Sin MCP** (Aider y compañía): escribir un bloque en `AGENTS.md` con los comandos CLI
  equivalentes. MemoryGraf ya es CLI-completo, así que no pierde capacidades.
- `memorygraf install --list` (qué hosts se detectan instalados) y
  `memorygraf uninstall <host> [--all]` — quitar debe ser tan fácil como poner.
- **Idempotente y no destructivo**: nunca pisar config existente del usuario; fusionar la
  clave `mcpServers` y avisar si ya había una entrada `memorygraf`.

**Pruebas.** Por host: dir temporal → `install` escribe la config esperada; re-ejecutar no
duplica; `uninstall` la deja como estaba; config previa con otros servidores MCP sobrevive
intacta (este es el test que importa — corromper el `.mcp.json` de alguien es imperdonable).

**Riesgo/Esfuerzo.** **Bajo**, con una advertencia: cada host cambia su formato de config
cada pocos meses. Mitigación: tabla declarativa en un módulo aparte (fácil de actualizar)
y test por host que falla ruidosamente si el formato cambia.

---

### M17 · Always-on: hooks de git + instrucciones de agente  (PROPUESTA)

**Contexto.** Dos problemas reales, ambos resueltos en Graphify y ninguno en MemoryGraf:
1. **El grafo se queda obsoleto.** Hay `watch`, pero exige un proceso vivo. Quien no lo
   deje corriendo consulta un grafo viejo — y **un grafo desactualizado es peor que no
   tener grafo**, porque el asistente responde con confianza sobre código que ya cambió.
2. **El asistente no lo usa solo.** Registrar el MCP no hace que el modelo prefiera
   `search` sobre leer archivos a ciegas. Graphify inyecta instrucciones y un hook
   PreToolUse; MemoryGraf no inyecta nada.

**Propuesta.**
1. **`memorygraf hook install|uninstall|status`**: hooks `post-commit` y `post-checkout`
   que lanzan `sync` incremental en segundo plano. Encadenables (no pisar hooks
   existentes: si ya hay uno, se añade una línea, no se sobrescribe el archivo).
2. **Sello de frescura, y que se vea.** Persistir en el grafo el SHA del último `sync`.
   Toda respuesta MCP/CLI lleva la antigüedad cuando importa: *"grafo sincronizado hace 12
   commits — puede estar desfasado"*. **Esto es más valioso que el hook**: convierte un
   fallo silencioso en uno visible, y encaja de lleno con §3.5 (trazabilidad) y con la
   honestidad que ya practica el proyecto en `doctor`.
3. **`memorygraf install claude --always-on`** (y equivalentes de M16): además del MCP,
   escribe en `CLAUDE.md`/`AGENTS.md` un bloque corto y **acotado** — cuándo consultar
   `overview`/`search`/`impact` antes de editar. Opt-in explícito, marcado entre
   delimitadores para poder retirarlo limpio, y **corto**: unas líneas que se cumplen,
   no un manual que el modelo ignora.

**Pruebas.** Repo temporal: `hook install` → un commit dispara `sync`; con un hook previo,
el previo sigue ejecutándose. Sello: `sync`, luego 3 commits → la respuesta reporta la
antigüedad. `--always-on` escribe el bloque una vez (re-ejecutar no duplica) y `uninstall`
lo retira sin tocar el resto del archivo.

**Riesgo/Esfuerzo.** Bajo-medio. El riesgo está en tocar el `.git/hooks` y el `CLAUDE.md`
**del usuario**: ambos son suyos, no nuestros. Regla: nunca sobrescribir, siempre entre
marcas, siempre reversible, y `status` para saber qué se tocó.

---

### M18 · Exportadores adicionales  (PROPUESTA)

**Contexto.** Hoy: `export` a JSON y `graph` a HTML. Graphify exporta además a Cypher
(Neo4j/FalkorDB), GraphML (Gephi/yEd), Obsidian y wiki markdown. No es capacidad de grafo,
es **alcanzar a quien ya tiene sus herramientas** — y es barato, porque el grafo ya está
en memoria en un formato limpio.

**Propuesta.** `memorygraf export --format json|graphml|cypher|obsidian|wiki`:
- **GraphML**: XML estándar, stdlib (`xml.etree`). Abre en Gephi/yEd para análisis visual
  serio, que `viz.py` no pretende cubrir.
- **Cypher**: `.cypher` con `CREATE` de nodos y aristas, para quien ya tiene Neo4j o
  FalkorDB. Fichero de texto, **sin driver ni dependencia** (nada de push directo: eso
  metería dependencia de red y credenciales en un proyecto que presume de local).
- **Wiki markdown**: un `.md` por nodo importante con enlaces `[[wikilink]]`. Esto sirve a
  **dos** públicos: Obsidian para el humano, y un árbol **navegable por el agente** con
  `Read`/`Grep` sin necesidad de MCP.

**Pruebas.** Grafo de juguete → GraphML válido contra el esquema; Cypher parseable
(comillas y saltos de línea escapados — el test que de verdad importa); wiki con enlaces
resueltos y **sin sobrescribir** ficheros que no generó él.

**Riesgo/Esfuerzo.** Bajo. Serializadores puros sobre datos que ya existen, cero
dependencias. Es el hito más seguro del anexo.

---

### M19 · Ingesta de documentación no-markdown  (PROPUESTA)

**Contexto.** Las decisiones y convenciones salen hoy de markdown. En proyectos reales el
"por qué" vive también en PDFs (RFCs, papers), `.docx` de requisitos y hojas de cálculo.
Graphify los ingiere. MemoryGraf los ignora.

**Propuesta, deliberadamente conservadora:**
1. Extras opcionales `pdf` (`pypdf`) y `office` (`python-docx`, `openpyxl`), patrón
   `parsers`/`neural`. Sin ellos, esos ficheros se omiten en silencio (§3.2).
2. Se ingiere **solo lo que ya sabemos ingerir**: el texto pasa por el **mismo extractor de
   decisiones/convenciones** que el markdown. **No** se inventa un pipeline semántico nuevo.
3. **Procedencia obligatoria** (§3.5): `documento.pdf:página` — sin eso no se ingiere, y
   ese requisito es el que marca el límite de qué formatos entran.
4. Opt-in por config (`index.documents`), **no** por defecto: un PDF de 300 páginas puede
   inundar el grafo de ruido, y el default nunca debe sorprender.

**Pruebas.** PDF de 2 páginas con una decisión → nodo `decision` con `pdf:página`. Sin
`pypdf` → omitido, `sync` no falla, `doctor` lo reporta. PDF escaneado (sin capa de texto)
→ omitido limpiamente, **sin OCR** (eso sí queda fuera de alcance).

**Riesgo/Esfuerzo.** Medio, y el riesgo es de **identidad**, no técnico. Ir más allá
—imágenes, vídeo, transcripción— convertiría a MemoryGraf en un graphify peor. El límite
propuesto: **texto con procedencia de página, y nada más**.

---

### M20 · Benchmark reproducible y público  (PROPUESTA)

**Contexto.** El README afirma **~40.000 → ~1.900 tokens (~21×)**. Es un dato real y
honesto, pero sale de **una tarea, medida a mano, no reproducible por terceros**. Graphify
publica LOCOMO y LongMemEval-S con harness, juez validado contra un segundo juez (kappa
0.81) y comandos de reproducción. Esa diferencia es **credibilidad**, y hoy es la brecha
más barata de cerrar en términos de código y la más cara en términos de percepción.

**Propuesta.** Convertir `benchmark.py` en un harness reproducible:
1. **Corpus fijo** de N tareas de orientación (~20) sobre 2-3 repos OSS clonables por SHA
   fijo: *"¿dónde se valida el token de sesión?"*, *"¿qué rompo si cambio esta firma?"*.
2. **Dos brazos**: (a) baseline sin grafo — el agente busca con grep/lectura; (b) con
   MemoryGraf. Medir **tokens hasta la respuesta correcta** y **acierto**, no solo tokens:
   gastar poco y responder mal no es una victoria, y presentarlo como tal sería el mismo
   humo del que el proyecto se distancia (§3.3).
3. **Juez determinista donde se pueda** (¿cita el `archivo:línea` correcto?) y LLM solo
   donde no; publicar el criterio y el desacuerdo entre jueces.
4. `BENCHMARKS.md` con tabla, metodología, **limitaciones** y comando exacto de
   reproducción. Incluir los casos donde el grafo **no** ayuda — eso es lo que hace creíble
   al resto.

**Pruebas.** El harness corre en CI sobre un repo mínimo (rápido); los repos grandes,
manual. Rerun con mismo SHA y misma semilla → mismos números en el brazo determinista.

**Riesgo/Esfuerzo.** Medio, y es sobre todo **trabajo de curación**, no de código. El riesgo
honesto: los números pueden salir peores que el 21× de la tarea suelta. **Publicarlos
igual** — un 5× medido y reproducible vale más que un 21× que nadie puede verificar.

---

### §NO adoptar (y por qué)

Deliberado. Copiar esto haría a MemoryGraf peor, no mejor:

| De Graphify | Por qué NO |
|---|---|
| **Multimodal: imágenes, vídeo, audio, YouTube** | Fuera de alcance (DESIGN §4). Exige LLM/transcripción por defecto y rompe "local, determinista, con procedencia `archivo:línea`". M19 se para en texto con página, a propósito. |
| **"Sin embeddings" como dogma** | MemoryGraf ya tiene búsqueda híbrida (TF-IDF/model2vec + RRF) y §3.8 la deja **regenerable, nunca fuente de verdad**. La postura actual es más flexible que la de Graphify; no hay nada que ganar renunciando a ella. |
| **9 backends LLM (bedrock, kimi, azure, deepseek…)** | Ya hay `ollama` + `api` compatible con OpenAI, que cubre LM Studio, vLLM, llama.cpp y nube. Añadir SDKs propietarios es superficie y dependencias a cambio de casi nada. |
| **28 dependencias obligatorias** | Contradice §3.2 de frente. El núcleo stdlib es la ventaja diferencial de MemoryGraf, no un accidente: **todo lo de este anexo entra como extra opcional o no entra**. |
| **Push directo a Neo4j/FalkorDB** | M18 exporta el `.cypher` y ahí termina. Push implica driver, red y credenciales en una herramienta que promete local-first. |
| **Grafo global cross-repo (`global add`)** | Ya cubierto: `init --project a --project b` mete varios repos en un grafo con enlace cross-project por endpoints. Un registro global aparte sería una segunda forma de hacer lo mismo. |
| **Memoria de trabajo / `reflect` (LESSONS.md)** | Es memoria episódica del agente, otra categoría (la de `memory-graph`). Mezclarla difumina qué es MemoryGraf: hechos verificables del código, no impresiones de sesiones. |

---

## 9. Validación de ENTORNO pendiente (no son features)

Lo ÚNICO genuinamente pendiente. No requiere código nuevo, solo ejecutar el guion
`E2E-INTEGRATION-TEST.md` en entornos que no cubrimos en Linux/WSL:

- **Windows nativo real** (PowerShell + Python de Windows): rutas con `\`, mapeo de
  cobertura (`<sources>`), instaladores `install.ps1`, `setup-ollama` con winget.
- **macOS**: `setup-ollama` con brew, FSEvents del watcher.
- **Escala real** en un repo grande propio (miles de archivos con historia) para validar
  M6 y los tiempos de `sync`/`blame`.

**Criterio de "listo pleno":** el reporte §14 del E2E en verde en Windows nativo, sin
bugs de degradación (§12) ni de rutas.
