# MemoryGraf — Deuda consciente y validación pendiente

> **Estado (2026-10-27).** El backlog **M1–M12 está IMPLEMENTADO y probado**, y del anexo
> M13–M20 ya entraron **M14 (`path A B`)** y **M15 completo**: M15a (Ruby, Kotlin, Swift,
> Dart), M15b (Scala, Lua, Elixir, Bash) y M15c (SQL, Terraform/HCL, PowerShell); y la deuda registrada (§2 coste incremental de M1, deuda de
> M15a incluidos sus dos últimos límites, Java Maven/Gradle, LSP con TS 7) está **saldada**. Desde julio, además: **CI** en cada PR (Linux +
> Windows nativo, modos portable y potencia; PR #13–#16 corrigieron los bugs que destapó)
> e **imagen Docker** (`Dockerfile`). La fuente de verdad es el **código + los tests
> (`tests/test_memorygraf.py`) + el historial de commits**; este documento registra:
> **(a)** lo hecho, **(b)** la **deuda consciente** que conviene recordar (por qué algo va
> gateado/acotado), **(c)** las propuestas abiertas (el **Anexo M13–M20**, roadmap de
> paridad con Graphify, y el **Anexo B M21–M26**, capa de dependencias y runtime para
> planificar actualizaciones) y **(d)** lo pendiente de ejecución sin código nuevo: la
> **validación de ENTORNO** (§9).
> Complementa `DESIGN.md` (principios §3, vinculantes).

> **▶ Siguiente paso (actualizar al cerrar cada hito).**
> 1. **M16** (instaladores multi-host): cierra el paquete de mayor retorno por esfuerzo
>    junto con M14 y M15.
> 2. Luego **M13** (comunidades) y **M17** (always-on). §9 sigue abierto en paralelo.
> 3. **Después del plan anterior:** el **Anexo B** (M21–M26), en el orden M21 → M22 → M24 →
>    M26 → M23 → M25. No se adelanta: M21 absorbe la parte de manifiestos de M15c y conviene que
>    la matriz de gramáticas esté cerrada antes (M26 lee accesos a miembros con ellas).

---

## 1. Backlog M1–M12 (+ M14, M15) — implementado

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
| M11 | **Capa LSP multi-lenguaje**: +Go/Rust/C/C++ (a), Java (b), C# (c), PHP/R (d) | `runtime/lsp.py` + `doctor`. Ver §M11. |
| M12 | **Respetar `.gitignore`** en el descubrimiento de archivos | `indexer`. Ver §M12. |
| M14 | **`path A B`**: camino(s) más corto(s) entre dos nodos, salto a salto (CLI + tool MCP `path`) | `query.Query.path`. Ver §M14. |
| M15a | **Ruby, Kotlin, Swift, Dart**: símbolos + `calls` intra + `imports`/`calls` cross-file | `ts_generic` + `indexer`. Ver §M15. |
| M15b | **Scala, Lua, Elixir, Bash**: ídem, cross-file solo donde es inequívoco | `ts_generic` + `indexer`. Ver §M15. |
| M15c | **SQL** (extractor léxico propio, todos los dialectos, incluido PL/SQL), **Terraform/HCL**, **PowerShell** | `sql_lex` + `ts_generic` + `indexer`. Ver §M15. |

Cada mejora entró con **test de regresión** y verificación en vivo (regla DESIGN §11).

---

## 2. Deuda consciente — por qué M1 full-repo va OPT-IN (medición)  (coste incremental SALDADO ✅ 2026-10-26)

El co-cambio por símbolo del `sync` sale del **blame** (atribuye cada línea a su ÚLTIMO
commit): un acoplamiento "de superficie". Si dos símbolos se co-editaron en commits viejos
cuyas líneas luego se reescribieron, esa señal se pierde. El **full-repo**
(`git_layer._full_symbol_cochange`, provenance `git-cochange-sym-full`) la recupera
re-extrayendo la versión histórica de cada commit — pero **cuesta ~14-23×** el blame
(medido antes de integrar con un prototipo aislado, ya retirado: 20 archivos×30 commits ≈ 14×,
50×50 ≈ 23×; domina `git show`+re-AST por cada (commit, archivo)).

**Por eso va OPT-IN** (`git.symbol_cochange_full=false` por defecto) y **acotado por
profundidad** (`git.cochange_full_depth`). Es aditivo (no borra el blame) y recomputa contra
los símbolos vigentes (sin ids obsoletos). El on-demand (`impact --deep`, acotado al
historial del archivo) cubre el caso puntual sin coste global.

**Saldado (2026-10-26): acumulador incremental por SHA + batching.** Lo que toca cada commit
(los ids de símbolos históricos cuyo span cruza sus rangos) es inmutable, así que se persiste
en `git_sha_touch` (clave proyecto+SHA, con una `version` que cambia si cambian los
extractores o el prefijo del proyecto). Cada sync solo procesa los commits nuevos; los diffs
salen de un `git show` multi-commit y los blobs de un único `git cat-file --batch`. La
intersección con los símbolos VIGENTES se sigue haciendo en cada sync. Medido sobre este repo
(88 commits): resultado **idéntico** commit a commit al algoritmo anterior; 1ª pasada 7,3 s →
5,1 s (domina el AST); syncs siguientes sin commits nuevos 7,3 s → ~1 ms. Sigue siendo
opt-in solo por el coste de esa primera pasada en repos con mucha historia.

---

## 3. M9 — matriz de cobertura cross-file (determinista, precisión-primero)

Solo enlaza si el destino es **INEQUÍVOCO** → sin aristas falsas (tests de precisión por
lenguaje). Encima, capa **LLM opcional** (`resolver.llm` / `MEMORYGRAF_XLINK_LLM`) que
desempata candidatos ambiguos (confidence 0.55 + provenance `llm`, fallback determinista).

| Lenguaje | imports | calls | Mecanismo |
|---|:--:|:--:|---|
| Python, JS/TS | ✅ | ✅ | `ast` / `ts_treesitter` (base histórica) |
| Java / Kotlin / Scala | ✅ | ✅ | header `package` (índice compartido: interop, layout Maven/Gradle); mismo paquete sin import; `import a.b.*` (y `import Obj._` / `import static C.*` = miembros); imports relativos de Scala; KMP `expect`/`actual` |
| Ruby | ✅ | ✅ | `require_relative` / `require` → `lib/`; autoload Zeitwerk (Rails, gemas) |
| Swift | ✅ | ✅ | módulo SwiftPM o target de Xcode (`project.pbxproj`) + módulos importados |
| Dart | ✅ | ✅ | relativo / `package:<pubspec>/`; sigue `export`; `part` hereda el scope |
| Lua | ✅ | ✅ | `local m = require("a.b")` → `a/b.lua` / `a/b/init.lua` (raíz, `lua/`, `src/`), solo si es único; `m.f()` |
| Elixir | ✅ | ✅ | índice global de `defmodule`; `alias` (también `as:` y `A.{B, C}`), nombre completo e `import` |
| Bash | ✅ | ✅ | `source` relativo al script (`$(dirname "$0")`, `${BASH_SOURCE%/*}`, `SCRIPT_DIR=...`); literal solo si existe en un único sitio |
| SQL | ✅ (`@@x`, `\ir x`) | ✅ | espacio de nombres GLOBAL del proyecto; `references` a tablas/vistas/secuencias, `calls` a funciones/procedimientos; nombre único (por sufijo), desempate por paquete y archivo |
| Terraform | ✅ (`source` local) | refs ✅ | un módulo = un directorio; `module.m.out` → `output` del módulo |
| PowerShell | ✅ | ✅ | dot-sourcing / `Import-Module` / `using module` relativos al script; ámbito de módulo (`.psm1`); sin mayúsculas |
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

## M11 · Ampliar la capa LSP a más lenguajes  (COMPLETO ✅)

**Estado (2026-07-27).** **M11a–d IMPLEMENTADOS.** `runtime/lsp.py` (`_LANGUAGES`) cubre LSP
para **Python**, **TypeScript/JS**, **Go (`gopls`)**, **Rust (`rust-analyzer`)**, **C/C++
(`clangd`)**, **Java (`jdtls`)**, **C# (`csharp-ls`)**, **PHP (`intelephense`, alt. `phpactor`)**
y **R (`languageserver`, vía R)** — diagnósticos + `resolved_type` vía hover, reusando el
cliente efímero. Para el Grupo B se añadió al cliente un **hook de init a medida** (claves
opcionales `workspace` e `init_options` en la spec): jdtls recibe un **workspace de datos
propio** (`-data <dir>` temporal, creado y limpiado por `_run_language`) y sus
`initializationOptions`; requiere **JDK 17+**. C#/PHP/R resultaron config-only (reusan el cliente
tal cual). Todos los servers son toolchain/OS-específicos, así que **no se auto-instalan**:
`doctor` los detecta (`shutil.which`) y muestra el comando correcto (`go install …` / `rustup
component add …` / clangd por apt·brew·winget / jdtls por brew·choco·descarga / `dotnet tool
install --global csharp-ls` / `npm i -g intelephense` / paquete R `languageserver`). Tests en
`tests/test_memorygraf.py` (`TestGroupALspHints`, mapeo de extensiones, hook
`workspace`/`init_options`, reporte por lenguaje). **Solo VB y Assembly** quedan symbols-only
(indexados con símbolos/`calls`/`imports` pero sin LSP standalone práctico); `doctor`/`configure`
lo marcan honestamente como "indexado, SIN capa LSP".

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
- **Grupo B — por lenguaje (launch/init server-específico):** **Java (`jdtls`)** ✅ hecho
  (M11b: workspace dedicado + JVM + `initializationOptions`, vía el hook `workspace`/`init_options`
  del cliente); **C# (`csharp-ls`)** ✅ hecho (M11c: single-binary por stdio, config-only pese a ser
  project-aware; OmniSharp queda como alternativa pesada).
- **Grupo C — nicho / parcial:** **PHP (`intelephense` [node] o `phpactor`)** ✅ y **R
  (`languageserver`, vía R)** ✅ hechos (M11d, config-only). **VB** y **Assembly** no tienen LSP
  standalone práctico → se quedan **symbols-only** permanente (marcado así en `doctor`).

**Orden ejecutado:** ~~M11a = Grupo A (Go/Rust/C/C++)~~ ✅ · ~~M11b = Java~~ ✅ · ~~M11c = C#~~ ✅ ·
~~M11d = PHP/R (Grupo C)~~ ✅. Cada sub-hito entró con su **detectable en `doctor`** (respetando
entorno/OS) y su **test** (mapeo de extensión, reporte por lenguaje supported con hint, skip
limpio si el server no está). Los servers de M11 se **detectan y sugieren** (no se auto-instalan,
por ser toolchain/OS-específicos): el hint dice qué correr; ejecutar la instalación queda al
usuario.

**Riesgo/Esfuerzo (retrospectiva).** Grupo A/C#/PHP/R: **bajo** (config + tests, sin motor
nuevo). Grupo B (Java): **medio** — la única lógica nueva fue el hook `workspace`/`init_options`
del cliente, que luego C# ni siquiera necesitó. Todo reutilizó el andamiaje existente (cliente
efímero, mapeo a `runtime_node`, degradación elegante).

---

## M12 · Respetar `.gitignore` en el indexado  (IMPLEMENTADO ✅)

**Estado (2026-07-27).** **IMPLEMENTADO.** El descubrimiento (`indexer._iter_files` y
`docs._iter_docs`) ahora respeta `.gitignore` como capa **adicional** a los excludes de dir.
Helpers en `indexer.py`: `_gitignored(root, paths)` (delega en `git check-ignore --stdin`, batch),
`_apply_gitignore(...)` (aplica el filtro + escape hatch) y `_index_settings(config)` (lee el
bloque `index`). Config: `index.respect_gitignore` (default `true`; sin git es no-op) e
`index.unignore` (rutas relativas por prefijo/glob a RE-INCLUIR). Tests: `TestGitignoreRespected`
(fuente entra / gitignorado fuera, negación `!` vía contenido, `respect_gitignore=false`,
`unignore`, sin-git cae a excludes, y `.md` gitignorado no genera docs). **Nota clave:**
`check-ignore` respeta el índice → los archivos YA rastreados nunca se saltan aunque encajen un
patrón; M12 solo elimina lo **no versionado** que git ignora (generado/local), que por definición
no es parte del proyecto.

**Contexto (por qué).** Antes se filtraba solo por **`DEFAULT_EXCLUDES` + `config.excludes`**
(nombres de directorio), sin consultar `.gitignore`: MemoryGraf miraba el **disco**, no el índice
de git, así que un dir gitignorado como `.next/` se indexaba igual hasta añadirlo a los defaults
(en un Next.js real eran 476/993 nodos = 48% de ruido generado).

**Problema.** `DEFAULT_EXCLUDES` es una **lista fija** que persigue frameworks uno a uno
(`.next`, `.nuxt`, `.turbo`, …). Siempre llegará tarde al siguiente framework/dir generado.
El propio proyecto **ya declara** qué NO es código fuente en su `.gitignore` — esa es la
fuente de verdad natural para "esto es generado/local, no lo indexes".

**Diseño (implementado).** Filtrar también por `.gitignore` durante el descubrimiento, con
degradación elegante y sin romper el modo portable:
1. **Si hay git** (ya es dependencia opcional de la capa temporal): los archivos ignorados se
   resuelven con `git check-ignore --stdin` (batch, rápido). Cero parsing propio de patrones →
   fidelidad total (negaciones `!`, `**`, anclajes, `.gitignore` anidados) sin reimplementar la
   semántica de git.
2. **Sin git / sin repo:** se mantiene el comportamiento previo (`DEFAULT_EXCLUDES` +
   `config.excludes`). Nada se rompe (regla DESIGN §3.2).
3. **Escape hatch:** algunos proyectos tienen **código fuente legítimo gitignorado** (módulos
   locales, generado-que-sí-es-código). `index.respect_gitignore` (default `true`, reversible) +
   `index.unignore` para volver a incluir rutas concretas (prefijo/glob).

**Interacción con lo existente.** `DEFAULT_EXCLUDES` se queda (cubre el caso sin git y da un
mínimo sano); `.gitignore` es una capa ADICIONAL, no un reemplazo. Orden: excludes de dir
(barato, poda ramas del `os.walk`) → luego filtro gitignore sobre los archivos que sobrevivan.

**Pruebas (`TestGitignoreRespected`).** Repo git: un `.py` fuente + un `generated/gen.py`
gitignorado → el fuente entra, el gitignorado no. Negación respetada (vía `generated/*` +
`!generated/keep.py`; git no permite re-incluir bajo un dir excluido, delegamos en su semántica).
Sin git: cae a `DEFAULT_EXCLUDES`. `respect_gitignore=false` → comportamiento previo. `unignore`
re-incluye. Un `.md` gitignorado tampoco genera docs (consistencia `_iter_docs`).

**Riesgo/Esfuerzo (retrospectiva).** Bajo. Sin parser propio (delega en git) el riesgo fue
mínimo; el grueso fue cablear el filtro en `_iter_files`/`_iter_docs` con batch a git y el
bloque de config. Se aplicó también a la extracción de docs por consistencia.

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
| **M14** ✅ | `path A B` — cómo se conectan dos nodos | Consulta que hoy no existe (hay `neighbors`, no camino) | **Bajo** | **Alto** |
| **M15** (a ✅) | Ampliar matriz de gramáticas (~12 → ~30) | 36 gramáticas vs ~12; hoy MemoryGraf no sirve en repos Kotlin/Swift/Ruby | **Bajo** | **Alto** |
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

### M14 · `path A B` — cómo se conectan dos nodos  (IMPLEMENTADO ✅ 2026-10-20)

**Cómo quedó** (`query.Query.path`, CLI `memorygraf path`, tool MCP `path` → 11 tools):
- Dos BFS (desde A y desde B) + DFS en orden de id: devuelve los K caminos **mínimos**
  (`--all K`), deterministas ante empates. El grafo se recorre **sin dirección**, pero
  cada salto se imprime con su sentido real (`-[calls]->` / `<-[imports]-`), su
  procedencia y su confianza. Un camino que se apoya en aristas INFERRED/AMBIGUOUS lo dice.
- **Desvío de la propuesta:** "ofrecer el nodo más cercano a ambos extremos" no aplica.
  En un recorrido sin dirección, si existe un nodo alcanzable desde ambos, entonces hay
  camino. En su lugar, sin camino se distingue: (a) existe, pero es más largo que
  `max_hops` (dice cuántos saltos tiene); (b) solo existe sin el filtro `--via`; (c)
  componentes desconectadas, y sugiere `neighbors` de cada extremo.
- **Añadido:** los nodos `external` (os, react…) no sirven de puente, solo de extremo
  (si no, dos archivos que importan `os` saldrían "conectados"). Un id inexistente
  sugiere ids con ese nombre (`Store.find_by_name`).
- Tests: `TestPath` (cadena, desconexión, `--via`, empate, `max_hops`, external, id
  desconocido, registro MCP).
- **Bug que destapó (corregido):** `from . import X` / `from pkg import sub` solo
  generaban la arista `imports` hacia el paquete (`__init__.py`), nunca hacia el
  submódulo. Los módulos importados así quedaban aislados (en este repo `viz.py` y
  `ollama.py`). Ahora el extractor ofrece `mod.nombre` al resolutor, y este deduplica
  por archivo. En este repo, `imports` pasa de 76 a 169 aristas sin perder ninguna.
  Test: `test_python_from_package_import_submodule`.

<details><summary>Propuesta original</summary>


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

</details>

---

### M15 · Ampliar la matriz de gramáticas (~12 → ~30 lenguajes)  (COMPLETO ✅: M15a 2026-10-26, M15b y M15c 2026-10-27)

**Hecho — M15a (2026-10-26).** Ruby (`.rb`), Kotlin (`.kt`/`.kts`), Swift (`.swift`) y Dart
(`.dart`) entran con **símbolos + `defines` + `calls` intra-archivo + `imports`/`calls`
cross-file**, ninguno como symbols-only. Cómo resuelve cada uno (todo determinista):

| Lenguaje | Imports | Calls cross-file | Particularidad del AST |
|---|---|---|---|
| Ruby | `require_relative` → relativo; `require "x"` → `lib/x.rb` (si no, gema externa) | `Const.m` / `Mod::Const.m` / `f` libre → símbolo único en los archivos requeridos | `module` es contenedor; `def self.x` → `Clase.x` |
| Kotlin | header **`package`** (no la ruta: vale con `src/main/kotlin/`) + símbolo top-level **único** del paquete; progresivo para anidadas | receptor importado (`Validator.isValid`) y **función importada**, también con alias (`as h`) | sin campo `name`; `companion object` → `Clase.f` |
| Swift | `import M` → módulo SwiftPM `Sources/M/` (1 archivo, como Go) | scope = **módulo propio** (no hay imports dentro de un módulo) + módulos importados; `Tipo.m`, `f` libre, `Modulo.f` | `extension T` → prefija `T.m` y cuelga del archivo; struct/enum/class son `class_declaration` |
| Dart | relativo, o `package:<name de pubspec.yaml>/x` → `lib/x`; `part` | scope = imports directos; `p.f` con `as p`; lo importado con prefijo NO se ve sin él | firma y cuerpo son **hermanos** (span sintético); sin nodo de llamada (`identifier` + `selector`) |

Un import que apunta a código propio pero no resuelve inequívocamente (Kotlin con el nombre
en dos archivos del paquete, Dart relativo a un archivo inexistente) **no** se convierte en
`external:` falso. Tests: fixtures en `tests/fixtures/crossfile/{ruby,kotlin,swift,dart}/`,
`TestPrecisionM15a` (7 casos que NO deben enlazar; verificado por mutación que fallan si se
quita la regla). Verificación en vivo sobre repos reales (sinatra, timber,
swift-argument-parser, dart-lang/args): Ruby/Kotlin/Dart parsean sin nodos `ERROR`; en Swift
37/171 archivos tienen alguno (`#if` alrededor de declaraciones, macros `#_sourceLocation`,
genéricos ambiguos con `<`), pero la recuperación es local: en esos archivos se extraen
493 funciones frente a 492 que cuenta un regex de `func`. 341 calls cross-file en
swift-argument-parser, 57 en args, 11 en sinatra.

**Deuda de M15a — SALDADA (2026-10-26).** Todo con test propio (`TestDebtM15a`, cada
mecanismo verificado por mutación: si se desactiva, su test cae):
- **Rails / Zeitwerk:** con `config/application.rb` (o `Zeitwerk::Loader.for_gem` en
  `lib/*.rb`), `Billing::Invoice.build` → `<raíz>/billing/invoice.rb` en las raíces de
  autoload (cada `app/*`, sus `concerns/`, `lib/`), probando primero el anidamiento léxico
  del llamante como hace Ruby. Sin Zeitwerk no se aplica (no se adivina por convención).
  **Saldado también (2026-10-27): autoload paths personalizados.** Se leen de
  `config/application.rb` y `config/initializers/*.rb`: `autoload_paths`/`eager_load_paths`/
  `autoload_once_paths` (`<<`, `+=`, `push`, `concat`, `%W(...)`), `Rails.root.join(...)`,
  `"#{root}/x"`, `push_dir(...)` (también en gemas) y `config.autoload_lib` (Rails 7.1+).
  Corrección de paso: `lib/` ya **no** se añade siempre (antes de Rails 7.1 no se autocarga
  si no se configura). En vivo (maybe-finance, Rails 7.2 con `autoload_lib`): 134 calls
  cross-file correctas, p. ej. `Money::Currency` → `lib/money/currency.rb`.
- **Kotlin (y Java):** el índice por header `package` es compartido Java+Kotlin; los nombres
  del **mismo paquete** (sin import) y de `import a.b.*` entran al scope; Kotlin
  Multiplatform desempata `expect`/`actual` (mismo source set que quien llama; si no, la
  `expect`). Un `import a.b.*` interno ya no produce `external:` falso.
- **Swift + Xcode:** se leen los `PBXNativeTarget` de `*.xcodeproj/project.pbxproj`
  (carpeta del target y, Xcode 16+, `fileSystemSynchronizedGroups`); cada target es un
  módulo, el código sin target conocido es visible para todos, y `@testable import App`
  resuelve al target. **Saldado también (2026-10-27): pertenencia explícita.** Un parser
  mínimo del plist ASCII de `project.pbxproj` reconstruye la ruta real de cada archivo
  desde los grupos (`sourceTree` `<group>`/`SOURCE_ROOT`) y lee las fases "Compile Sources"
  de cada target: un archivo de un solo target es de ese módulo aunque viva fuera de su
  carpeta; uno de varios targets es compartido. Prioridad: SwiftPM > pertenencia explícita
  > carpeta sincronizada > carpeta con el nombre del target. En vivo: Alamofire (formato
  clásico, 10 targets): 96/96 rutas reconstruidas existen en disco y sus fuentes quedan como
  compartidas; NetNewsWire (Xcode 16, 8 targets): grupos sincronizados + 3 archivos fuera
  de carpeta.
- **Dart:** se siguen los `export` de forma transitiva (barriles `lib/x.dart`), y un `part`
  hereda el scope de su librería (sus imports, la librería y los demás parts).
- **Hallazgo colateral — Java en layout Maven/Gradle:** resuelto con el índice de paquetes;
  en timber desaparece `external:timber` y las calls cross-file pasan de 0 a 111.

**Fallo preexistente arreglado — LSP de TypeScript con TypeScript 7.** El test
`TestLspTypeScript` fallaba en este entorno porque el `typescript` global es la 7 (nativa, en
Go), que ya no trae `tsserver.js`: `typescript-language-server` respondía error al
`initialize` y MemoryGraf, que no esperaba esa respuesta, lo reportaba en silencio como
"0 tipos" (y `doctor` lo daba por instalado). Ahora: se espera la respuesta de `initialize`
y, si falla, se dice por qué y se prueba el siguiente servidor; TS ≥7 se usa con su LSP
nativo (`tsc --lsp --stdio`); se soportan diagnósticos por **pull** (LSP 3.17, lo único que
TS 7 ofrece); `doctor` solo da el LSP de TS por bueno si es usable, y el instalador pasa a
`npm i -g typescript`.

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
  **✅ Hecho (2026-10-27)** — los cuatro con cross-file, porque en los cuatro hubo una forma
  inequívoca:
  - **Scala:** comparte el índice de paquetes JVM (interop con Java/Kotlin). Paquetes
    encadenados (`package a` + `package b`), selectores `{A, B => C}`, `_`/`*`, **imports
    relativos** (`import TestUtil.prep` dentro de `package os` → `os.TestUtil.prep`;
    `import collection.X` → `scala.collection`; encadenados a un import anterior) e
    `import Obj._` (miembros del objeto sin prefijo).
  - **Lua:** solo `local m = require("a.b")` liga un nombre; destino `a/b.lua` o
    `a/b/init.lua` bajo la raíz, `lua/` (plugins de Neovim) o `src/`, y **solo si es único**.
    Las funciones globales sin `require` no se enlazan (cualquier archivo cargado antes
    podría definirlas).
  - **Elixir:** los módulos tienen nombre GLOBAL (`defmodule A.B`, anidados = `Padre.A.B`,
    `defimpl P, for: T` = `P.T`), así que hay un índice módulo → archivo; `alias`
    (`as:`, `A.{B, C}`), nombre completo e `import` (para las llamadas libres). Un módulo
    definido en dos archivos no se enlaza; un `alias MiApp.X` que no resuelve no se
    convierte en `external:MiApp`.
  - **Bash:** solo `source`/`.` relativos AL SCRIPT (`$(dirname "$0")/x`,
    `${BASH_SOURCE%/*}/x`, o una variable asignada así, como `SCRIPT_DIR`). Una ruta
    literal depende del directorio de trabajo: se enlaza solo si existe en un único sitio
    plausible (junto al script o en la raíz). Variables de entorno (`$BATS_ROOT/...`) no se
    adivinan; Bash nunca genera `external:`.

  **Fallo de precisión encontrado en la validación en vivo (y arreglado en todos los
  lenguajes):** una llamada con receptor complejo (`java.nio.file.Files.size(x)`,
  `proc(...).call()`, Ruby `user.save`, C++ `obj.size()`) se trataba como llamada LIBRE y
  podía enlazar con una función top-level del mismo nombre. Ahora "sin receptor" y "receptor
  no resoluble" son distintos y el segundo nunca se resuelve como libre. También: en Scala y
  Kotlin los `object`/clases anidados se cualifican (`makeDir.all` y `remove.all` de os-lib
  colisionaban en un único `all` que parecía top-level).

  **Validación en vivo** (0 archivos con nodos `ERROR` salvo 1/111 en plenary.nvim):
  os-lib (Scala) 82 calls cross-file, plenary.nvim (Lua) 114, plug (Elixir) 335. En
  bats-core (Bash) 0, y es lo correcto: todos sus `source` usan `$BATS_ROOT`, una variable
  de entorno.
- **M15c — infra/datos:** **SQL**, **Terraform/HCL**, **PowerShell**, **JSON de manifiestos**
  (`package.json`, `pyproject.toml`, `go.mod` → nodos de paquete + `depends_on`, que hoy
  no existen y son de los más consultados en onboarding). **La parte de manifiestos la
  absorbe y amplía M21** (lockfiles, versiones, runtimes, imágenes): M15c se queda con
  SQL/HCL/PowerShell como gramáticas.
  **✅ Hecho (2026-10-27)**, los tres con cross-file:
  - **SQL, con un extractor LÉXICO propio (`extractors/sql_lex.py`), no tree-sitter.** La
    gramática SQL de tree-sitter rompe con PL/SQL de Oracle (`BEGIN … END;` y el `/` de
    SQL*Plus parten las sentencias; se pierde hasta el nombre del paquete), y ahí vive la
    lógica de muchos sistemas. Un tokenizador que descarta comentarios y literales sirve
    para todos los dialectos (PostgreSQL con `$$`, MySQL, T-SQL con `GO`, Oracle con `/`)
    y, al no depender de tree-sitter, funciona también en modo **portable**. Símbolos:
    tablas, vistas, funciones, procedimientos, paquetes y sus `pkg.proc`, triggers, tipos,
    secuencias y sinónimos. Aristas: `references` a lo que se lee o escribe (FROM/JOIN, INTO,
    UPDATE, DELETE, MERGE, TRUNCATE, FK `REFERENCES`, `ON t` de índices y triggers,
    `seq.NEXTVAL`) y `calls` (`f(...)`, `pkg.proc;` de PL/SQL, `EXEC @r = dbo.proc` de
    T-SQL). Resolución contra el espacio de nombres global del proyecto, filtrando por
    tipo (una ref nunca casa con una función ni una llamada con una tabla), por sufijo
    (`app.orders` ~ `orders`) y solo si es único (desempate: mismo paquete, como PL/SQL; si
    no, mismo archivo). Se excluyen las CTE, los alias y el `SELECT … INTO variable`.
    T-SQL idempotente: `ALTER PROCEDURE … AS` tras un stub creado por SQL dinámico también
    define el objeto (no el `ALTER … COMPILE` de Oracle). Includes: `@@x`/`\ir x` relativos
    al script; `@x`/`\i x`/`START`/`SOURCE` literales, solo si son únicos.
    Las `references` llevan la procedencia `ast-ref`, que `confidence` clasifica como
    **EXTRACTED** (están escritas en el código, no deducidas).
    **Literales y dialecto (2026-10-27, PR #18 ronda 2).** La barra invertida escapa la
    comilla en MySQL/MariaDB (`'O\'Brien'`) pero es un carácter más en SQL estándar,
    T-SQL, Oracle y PostgreSQL (`'C:\'` es un literal completo; en PostgreSQL solo
    escapa dentro de `E'…'`). Regla, por archivo: (1) con pistas de MySQL (backticks,
    `/*!40101 … */`, `ENGINE=`, `AUTO_INCREMENT`, `DELIMITER`, `LOCK TABLES`,
    `UNSIGNED`) y **ninguna** de T-SQL (líneas `GO`, `NVARCHAR`, `IDENTITY(`,
    `SET NOCOUNT`, `dbo.`), `\'` escapa; (2) recuperación: si con ese modo un literal
    queda abierto hasta el final del archivo y con el otro no, se usa el otro. Límite
    consciente: un MySQL **sin** pistas con un número **par** de `\'` cierra en ambos
    modos y se lee en modo estándar (puede perder lo que hay entre los dos literales,
    nunca inventa aristas). Comentarios `/* … */` anidados (PostgreSQL/T-SQL) solo si
    el anidamiento cierra; si no, cierre plano (Oracle/MySQL). `GRANT/REVOKE … ON t`
    da una `references` a `t` (no a los privilegios), y `FROM f(...)` una `calls` a `f`.
  - **Terraform/HCL:** un módulo es un DIRECTORIO (sus `.tf` comparten nombres y Terraform
    prohíbe los duplicados), así que `var.x`/`local.x`/`aws_x.y`/`data.t.n`/`module.m` se
    resuelven en el directorio del archivo y `module.m.out` va al `output "out"` del
    directorio del `source` local. Las variables de un `for` y el iterador de un `dynamic`
    no son recursos. Módulos remotos -> `external:` con nombre legible
    (`terraform-aws-modules/vpc`, `acme/vpc` de un registro privado, `repo` de un git).
  - **PowerShell:** como Bash: dot-sourcing, `Import-Module` y `using module` relativos al
    script (`$PSScriptRoot`, `Join-Path $PSScriptRoot …`); una ruta literal solo si es
    única; `Import-Module Az.Accounts` -> `external:Az.Accounts`. El código dot-sourced
    corre en el ámbito de quien lo carga, así que los `.ps1` de un `.psm1` se ven entre sí.
    Nombres sin distinguir mayúsculas; `function Global:X` define `X`; `[Tipo]::M()`.

  **Validación en vivo:** db-sample-schemas de Oracle (150 objetos: 99 `references`, 14
  `calls`), la solución de mantenimiento de Ola Hallengren en T-SQL (11 procedimientos con
  sus cuerpos de miles de líneas completos; las llamadas a `dbo.CommandExecute` desde los
  archivos sueltos quedan sin enlazar porque el repo lo define 3 veces: correcto),
  terraform-aws-vpc (1.890 símbolos, 3.382 `references`), posh-git (31 calls cross-file
  por ámbito de módulo). Tres fallos destapados así y corregidos con test: el stub +
  `ALTER` de T-SQL, el `CREATE TABLE #tmp` que cortaba el cuerpo de un procedimiento, y el
  ámbito de módulo de PowerShell.

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

## Anexo B · Capa de dependencias, runtime y actualizaciones (M21–M26)  (PROPUESTA)

**Estado (2026-10-26).** Propuesta. Se implementa **después** del plan vigente (M15, M16,
M13, M17; ver "Siguiente paso" al inicio). A diferencia del Anexo M13–M20, no nace de
comparar con otra herramienta: nace de un **caso real medido**, la actualización de
seguridad de un sistema en producción (Node 20 → 24, dependencias npm y Python) que tomó
varios días y tuvo cinco incidentes que nadie vio venir (casos 3, 4, 5, 7 y 9).

**El caso que lo origina (vmd_sys, octubre 2026).** Un backend Node/Express sobre Oracle Linux
8 (podman), un frontend React/Vite detrás de nginx, dos agentes Python y un bot Python en otro
repo. Lo que pasó, en orden:

| # | Qué pasó | Dónde vivía el problema | ¿El grafo actual lo veía? |
|---|---|---|:--:|
| 1 | Backend y frontend en **Node 20, ya fuera de soporte**: sin parche para 10 CVEs (uno de RCE) | `ARG NODE_MAJOR=20` del Dockerfile, `FROM node:20-alpine` | No |
| 2 | 38 vulnerabilidades npm (`npm audit`). El triage (cuáles importan) se hizo **a mano con grep**: `xlsx` lee archivos subidos en 4 pantallas (real); `postcss`/`vite` solo corren en el build (no) | lockfiles + uso en el código | Parcial: solo `external:xlsx` → archivos |
| 3 | **npm 11 bloquea por defecto los install scripts**: sin `allowScripts`, `better-sqlite3` no compilaba y el backend no arrancaba | `hasInstallScript` en el lockfile + versión de npm del runtime | No |
| 4 | Build roto: node-gyp 12.4 (de npm 11.19) acepta Python ≥3.6 pero su gyp exige ≥3.9; el builder de OL8 traía `python3` = 3.6 | Cadena nativo → node-gyp → `python3` del `RUN microdnf install` | No |
| 5 | En producción el backend **moría cada ~2 min** (202 reinicios) sin error de JS: `better-sqlite3` ≤12 usa la API C++ de Node (`node::ObjectWrap`) y con Node 24.19–24.21 el GC aborta con `RemoveEnvironmentCleanupHook … (env) != nullptr`. Arreglo: `better-sqlite3` 13 (Node-API) | Acoplamiento ABI nativo ↔ runtime exacto | No |
| 6 | El prebuild de `better-sqlite3` 13 exige **GLIBC_2.34**; OL8 tiene 2.28 → hubo que compilarlo en el builder | ELF del `.node` vs glibc de la imagen | No |
| 7 | La regresión A/B pasó limpia porque corrió con **Node 24.22.1** (ya corregido) y producción tenía el RPM **24.21.0** | Versión verificada ≠ versión desplegada | No |
| 8 | El parche de Python de octubre existía para `python3.12` en OL8 pero **no para `python3.11`**, que usaban los agentes | Disponibilidad del arreglo en la fuente de paquetes de la imagen | No |
| 9 | Ya desplegado y estable, **todo PL/SQL con parámetros OUT fallaba** con `util.isDate is not a function` (Finalizar/Retomar/Cancelar órdenes, Agregar Servicio, Reaprovisionamiento). Node 23 eliminó `util.is*` (DEP0047) y `node-oracledb` 5.x la llama en cada bind-objeto. La regresión A/B no lo vio: su simulador **reemplazaba el driver entero** | Código JS **de una dependencia** que usa una API que el runtime nuevo eliminó | No |

`impact external:better-sqlite3` respondía bien lo que respondía (los 5 archivos que la usan),
pero el nodo no tenía **versión, ni proyecto** (`project: null` en los 71 externos), ni sabía
que era un **módulo nativo**, ni con qué runtime, toolchain e imagen se construye y corre.
Todos los incidentes vivieron en esos eslabones. Esta capa los modela.

Ninguno de los nueve fue un error del código propio del proyecto: todos estuvieron **debajo**
(paquetes, binarios, toolchain, runtime, imagen) o **en la forma de verificar**. El caso 9 lo
deja más claro que ninguno: la línea que fallaba estaba en `node_modules/oracledb/lib/`, y el
código del proyecto que la disparaba no había cambiado en meses.

**Qué NO es (posicionamiento honesto).** No compite con Dependabot/Renovate (abren PRs de
versión), ni con `npm audit`/`pip-audit`/OSV/Snyk (saben qué versión es vulnerable). Esos
responden **qué** cambiar. Ninguno responde lo que MemoryGraf ya sabe y ellos no: **dónde toca
eso tu código**, **qué cadena de build y runtime arrastra**, **qué componentes hay que
reconstruir** y **a través de varios repos y lenguajes a la vez**. La capa **consume** sus
salidas y **agrega** el grafo; no reimplementa ninguna.

**Criterio (los mismos del Anexo M13–M20, más uno).** (a) núcleo **stdlib** con degradación
elegante (§3.2): lockfiles y Dockerfile se leen con `json`/`tomllib`/texto, ELF con `struct`;
(b) **determinista** (§3.10); (c) **procedencia** en todo (§3.5): `package-lock.json#<ruta>`,
`Dockerfile:línea`; (d) **sin tools MCP nuevas** (§3.9): todo enriquece `get`/`neighbors`/
`impact`/`overview`; y **(e) solo lectura (§4.2)**: MemoryGraf **no ejecuta** `npm`, `pip`,
auditores, builds ni tests. Lee lockfiles, binarios ya instalados y los JSON/logs que esas
herramientas ya produjeron. La red es **opt-in explícito** y con caché.

### Prioridad sugerida

| # | Hito | Incidentes que cubre | Esfuerzo | Valor |
|---|---|:--:|---|:--:|
| **M21** | Manifiestos, lockfiles, runtimes e imágenes como nodos | base de todo (1, 3, 4 parcial) | Medio | **Alto** |
| **M22** | `upgrade-plan`: impacto de actualizar un paquete o runtime | 3, 4, 5 (riesgo), 7 | Medio | **Alto** |
| **M24** | Compatibilidad de binarios nativos con la imagen | 5 (señal fuerte), 6 | Bajo-medio | **Alto** |
| **M26** | APIs del runtime eliminadas o cambiadas, también dentro de las dependencias | 9 | Medio | **Alto** |
| **M23** | Vulnerabilidades y fin de soporte, con alcanzabilidad | 1, 2, 8 | Medio | Alto |
| **M25** | Evidencia de verificación y paridad de runtime | 7 | Bajo | Medio |

M24 va antes que M23 a propósito: es determinista, sin red y detecta lo que más dolió (5 y 6).
M26 va detrás de M24 por el mismo motivo (determinista, sin red) y porque reutiliza su opt-in
para mirar dentro de `node_modules`/venv. M23 depende de datos externos y es lo que más se
parece a herramientas que ya existen.

---

### M21 · Manifiestos, lockfiles, runtimes e imágenes como nodos  (PROPUESTA)

**Contexto.** Hoy un import no resuelto crea `external:<pkg>` (`indexer._resolve_imports`) con
`depends_on` desde el archivo (confianza 0.8, `regex`). Es útil pero plano: sin versión, sin
ecosistema (`external:requests` de Python y uno de npm con el mismo nombre serían el mismo
nodo), sin proyecto y sin nada de lo que hay **debajo** del paquete. M15c ya proponía leer
manifiestos; M21 lo toma y lo extiende hasta la imagen.

**Propuesta.**
1. **Identidad por Package URL (purl), sin versión.** `pkg:npm/better-sqlite3`,
   `pkg:pypi/requests`, `pkg:golang/…`, runtimes como `pkg:generic/node`, `pkg:generic/python`,
   paquetes de SO como `pkg:rpm/oracle/python3.12` y bases como `pkg:docker/oraclelinux`. purl es
   el identificador que usan OSV, CycloneDX y SPDX: M23 consulta por él sin traducciones. La
   **versión va como atributo**, no en el id: el nodo sobrevive a la actualización (§6.4) y su
   historial de versiones sale de git (cada cambio del lockfile).
2. **Esquema mínimo, sin tipos nuevos de más.** Los paquetes, runtimes, imágenes y toolchains
   siguen siendo `external`, con tags `package` / `runtime` / `image` / `toolchain` y
   `ecosystem:npm`. Las unidades desplegables (imagen de backend, imagen de frontend, venv de un
   agente) son `module` con tag `deployable`. Aristas: `depends_on` (archivo→paquete y
   paquete→paquete, con atributos `range`, `resolved`, `scope` = `prod|dev|optional|build`),
   y tres nuevas: `requires_runtime` (de `engines`, `python_requires`, nivel de Node-API),
   `runs_on` (desplegable → runtime/imagen) y `built_with` (desplegable/etapa → toolchain). Se
   documentan en DESIGN §6 al implementarse.
3. **Fuentes por ecosistema** (todas lectura de archivos, stdlib):
   - **npm:** `package.json` + `package-lock.json` v2/v3 (versión resuelta, `dev`, `optional`,
     `hasInstallScript`, `engines`, `os`/`cpu`). El lockfile v1 degrada a solo directas.
   - **Python:** `requirements*.txt` (pins y pisos), `pyproject.toml` (`tomllib`), y
     `uv.lock`/`poetry.lock` si existen.
   - **Go:** `go.mod`/`go.sum`. Composer/Cargo quedan para después, con el mismo patrón.
4. **Dockerfile (best-effort, procedencia `Dockerfile:línea`).** Etapas (`FROM … AS x`),
   sustitución de `ARG` con su default (`ARG NODE_MAJOR=24` → `nodejs:24`), paquetes de SO de
   `RUN microdnf|dnf|yum|apt-get|apk install …`, `COPY --from=` entre etapas y `CMD`/`ENTRYPOINT`.
   **No se evalúa shell**: lo que no sea una línea de instalación reconocible se ignora (precisión
   primero, como M9). Imágenes oficiales (`FROM node:24-alpine`) → runtime implícito.
5. **CI (best-effort, INFERRED).** `.gitlab-ci.yml`/GitHub Actions: imágenes de jobs y elección de
   intérprete (`python3.12 -m venv`, `${VAR:-python3.11}`) → `runs_on` con confianza < 1.0 y
   procedencia `ci:línea`. Si no se entiende, no se emite.
6. **Marca de módulos nativos** (lo que más importó en el caso real): `hasInstallScript`,
   `gypfile`/`binding.gyp`, carpeta `prebuilds/`, y **cuál API nativa usa**: depende de
   `node-addon-api` → `native:napi` (estable entre versiones de Node); de `nan` o compila contra
   la API C++ de Node → `native:node-api-cpp` (frágil). Lo que requiere mirar `node_modules`
   (excluido del indexado) es **opt-in** (`deps.scan_installed`); sin eso se marca lo que diga el
   lockfile.
7. **Corrige de paso** el `project: null` de los externos y separa ecosistemas.

**Superficie.** Ninguna tool nueva: `get pkg:npm/better-sqlite3` muestra versión, scope, si es
nativo, quién lo usa y en qué desplegables viaja; `neighbors`/`impact` lo recorren como cualquier
nodo. `overview` gana una línea por desplegable: runtime + imagen + nº de dependencias.

**Pruebas.** Fixtures por ecosistema: lockfile v3 con una dependencia nativa, una de dev y una
transitiva → nodos, scopes y aristas esperados. Dockerfile multi-etapa con `ARG` sustituido y un
`RUN … install` con continuaciones `\` → paquetes de SO correctos; un `RUN` de shell arbitrario
→ nada inventado. Lockfile v1 → degrada sin fallar. Incremental: lockfile sin cambios no
re-procesa (hash, §3.6). Determinismo: mismo lockfile, mismos nodos y orden.

**Riesgo/Esfuerzo.** Medio. Lockfiles son JSON/TOML bien especificados (bajo riesgo). El riesgo
real está en el Dockerfile y el CI: son lenguajes con shell dentro. Mitigación: reconocer solo
patrones explícitos y declararlo INFERRED; jamás ejecutar ni evaluar.

---

### M22 · `upgrade-plan` — impacto de actualizar un paquete o un runtime  (PROPUESTA)

**Contexto.** Con M21 el grafo sabe qué hay debajo de cada archivo. M22 lo convierte en la
pregunta que se hace antes de cada actualización: *"si subo X a la versión Y, ¿qué toco, qué se
puede romper, qué reconstruyo y qué tengo que probar?"*. En el caso real esa pregunta se respondió
a mano, un incidente a la vez.

**Propuesta.**
1. **CLI** `memorygraf upgrade-plan <purl|nombre> [--to <versión>]` y
   `memorygraf upgrade-plan --runtime node --to 24.21.0`. **Modo revisión:**
   `--from-ref <git-ref> --to-ref <git-ref>` compara los lockfiles/Dockerfiles de dos commits y
   arma el plan de un cambio **ya hecho** (el PR de actualización), que es el uso más frecuente.
2. **MCP sin tool nueva (§3.9):** `impact` acepta `to_version` opcional. Sobre un nodo
   `package`/`runtime`, su respuesta añade las secciones de abajo, dentro del presupuesto de tokens.
3. **El reporte, en secciones fijas (determinista):**
   - **Quién lo usa:** archivos/símbolos con `archivo:línea` (ya existe), separados por
     desplegable y por `scope` (runtime vs solo build).
   - **Hacia abajo:** dependencias transitivas que cambian (con `--from/--to-ref`, el diff exacto
     del lockfile), cuáles son nativas y con qué API.
   - **Hacia arriba:** etapas del Dockerfile y toolchain implicados (node-gyp ← npm del runtime;
     `python3` del builder; compilador), imagen base y su glibc (M24).
   - **Señales de riesgo:** reglas declarativas (punto 4), cada una con su porqué y su fuente.
   - **Qué reconstruir y desplegar:** los `deployable` que lo contienen (y los que no: "el
     frontend no cambia" también es información).
   - **Qué probar:** `tested_by` de los archivos usuarios (ya existe) + las pruebas de sistema
     que exijan las reglas (p. ej. resistencia) + la evidencia que ya haya (M25).
4. **Reglas de riesgo como tabla declarativa** (`upgrade_rules.py`), cada una con test. El
   set inicial sale **exactamente** de los incidentes del caso real:

   | Regla | Dispara cuando | Mensaje (resumido) |
   |---|---|---|
   | `major-bump` | salto de versión mayor | revisar changelog; listar usos |
   | `engines-mismatch` | `engines`/`python_requires` excluye el runtime destino | no arranca / no instala |
   | `install-script-npm11` | paquete con `hasInstallScript` y npm ≥ 11 en el runtime | falta en `allowScripts` → se salta en silencio |
   | `node-gyp-python` | nativo compilado en el build + Python del builder < el que exige el node-gyp del npm destino | build roto (caso 4) |
   | `native-cpp-api-runtime-change` | `native:node-api-cpp` + cambio de versión de Node (incluso menor) | ABI frágil: **exigir prueba de resistencia** (caso 5) |
   | `prebuild-glibc` | prebuild con GLIBC/GLIBCXX > imagen (M24) | no carga en la imagen (caso 6) |
   | `eol-runtime` | runtime destino o actual fuera de soporte (M23) | sin parches |
   | `runtime-api-removed` | código propio **o de una dependencia** usa una API que el runtime destino eliminó (M26) | falla en tiempo de ejecución, solo en el camino que la usa (caso 9) |
   | `runtime-behavior-change` | se usa una API cuyo comportamiento cambió entre origen y destino (M26) | revisar; no es un error seguro |
   | `build-only` | solo `dev`/etapa de build | bajo impacto en producción |

   Las reglas son **heurísticas**: su salida es INFERRED con confianza < 1.0 y cita qué hecho
   del grafo la disparó. Ninguna se presenta como certeza.
5. **Límite honesto.** El crash del caso 5 **no es predecible en estático**: lo que M22 puede
   hacer es clasificar el riesgo (nativo con API C++ + cambio de runtime) y exigir la prueba que
   lo habría encontrado. Eso ya habría cambiado el resultado: la prueba de resistencia que lo
   detectó se escribió **después** del incidente.

**Pruebas.** Fixture que reproduce el caso real en miniatura: lockfile con un nativo `nan`,
Dockerfile con `ARG NODE_MAJOR=20` y `python3` del sistema; plan de `--runtime node --to 24` →
deben disparar `native-cpp-api-runtime-change`, `node-gyp-python` e `install-script-npm11`, y
**no** `prebuild-glibc` (no hay prebuild). Modo `--from-ref/--to-ref` sobre un repo git temporal.
Determinismo: mismo par de refs → mismo reporte byte a byte.

**Riesgo/Esfuerzo.** Medio. El motor es recorrido de grafo (ya existe); el trabajo está en las
reglas y en sus tests. Riesgo de ruido: una regla que dispara siempre se ignora. Mitigación: cada
regla cita el hecho concreto que la disparó, y `--explain <regla>` muestra por qué.

---

### M24 · Compatibilidad de binarios nativos con la imagen  (PROPUESTA)

**Contexto.** Dos de los incidentes estaban **escritos en los binarios** y se diagnosticaron a mano
con `objdump`: el prebuild de `better-sqlite3` 13 pedía `GLIBC_2.34` (la imagen tenía 2.28) y el
`.node` de la 12 importaba `node::RemoveEnvironmentCleanupHook`, un símbolo C++ de Node, que es lo
que lo hacía frágil ante el cambio de runtime. Ambas cosas son **deterministas y verificables sin
ejecutar nada**.

**Propuesta.**
1. **Lector ELF en stdlib** (`struct`, ~200 líneas): de cada `.node`/`.so` extrae `DT_NEEDED`,
   las versiones requeridas (`.gnu.version_r`: `GLIBC_x`, `GLIBCXX_x`, `CXXABI_x`) y los símbolos
   indefinidos. De ahí:
   - **glibc/libstdc++ mínimos** que exige el binario;
   - **API nativa real**: si solo importa `napi_*` → Node-API (estable); si importa símbolos
     `_ZN4node…`/`_ZN2v8…` → API C++ de Node/V8 (frágil ante cualquier cambio de versión).
2. **Glibc de la imagen destino:** de la config (`runtime.targets`) o de una tabla declarativa
   para bases comunes con su fuente (OL8/RHEL8 = 2.28, Debian 12 = 2.36, Ubuntu 22.04 = 2.35,
   Alpine = musl). Si la base no está en la tabla y no hay config, se dice "desconocida", no se
   adivina.
3. **Dónde mira:** `node_modules/**/*.node` y `*.so` de un venv, **opt-in**
   (`deps.scan_installed` + rutas), porque esas carpetas no se indexan. Prioriza `prebuilds/` y
   `build/Release/`, que es donde vivían los dos binarios del caso real.
4. **Salida:** en `get`/`impact` del paquete y como regla `prebuild-glibc` de M22.
   Ejemplos reales: *"prebuilds/linux-x64.node exige GLIBC_2.34 > OL8 (2.28): no cargará"*;
   *"better_sqlite3.node importa node::RemoveEnvironmentCleanupHook (API C++ de Node, no
   Node-API): sensible a la versión exacta del runtime"*.
5. **Fuera de alcance inicial, declarado:** PE (Windows) y Mach-O (macOS). En la tabla de `doctor`
   se marca como "solo ELF".

**Pruebas.** Fixtures ELF mínimos versionados en `tests/fixtures/elf/` (uno que exige GLIBC_2.34
y otro solo-Node-API) → versiones y API detectadas. Binario truncado/no-ELF → se ignora sin fallar.
Imagen sin entrada en la tabla → "desconocida". Corre en cualquier OS (es lectura de bytes).

**Riesgo/Esfuerzo.** Bajo-medio. El formato ELF está bien especificado y la lectura es acotada.
Riesgo: falsos "frágil" si una librería importa símbolos C++ de V8 pero vía una capa estable;
por eso la conclusión es una **señal con su evidencia** (los símbolos), no un veredicto.

---

### M23 · Vulnerabilidades y fin de soporte, con alcanzabilidad  (PROPUESTA)

**Contexto.** `npm audit` reportó 38 hallazgos y casi todo el trabajo fue **clasificarlos**:
cuáles llegan a producción, cuáles solo al build, cuáles tocan una función que el código usa de
verdad. Eso es exactamente un recorrido del grafo. Lo mismo con los runtimes: que Node 20 estaba
fuera de soporte se supo leyendo la página de releases, y que el parche de Python no existía para
`python3.11` en OL8, leyendo erratas de Oracle.

**Propuesta.**
1. **Ingesta, no auditoría (§4.2):** `memorygraf vulns ingest <archivo>` acepta la salida que el
   usuario ya generó: `npm audit --json`, `pip-audit -f json`, `osv-scanner --format json`.
   MemoryGraf no ejecuta esas herramientas.
2. **Red solo opt-in** (`--online` o `deps.vulns.online=true`): consulta por purl a la API pública
   de OSV y fechas de soporte de runtimes a endoflife.date, con **caché fechada** en
   `.memorygraf/cache/` y uso offline por defecto. Sin red, todo lo demás funciona.
3. **Nodos `advisory`** (`advisory:GHSA-…`/`CVE-…`, severidad, rango afectado, versión con
   arreglo, URL de la fuente) con arista `affects` → paquete. Es el único tipo de nodo nuevo del
   anexo y se justifica: un advisory no es código, ni paquete, ni decisión.
4. **Alcanzabilidad (el valor real):** cada advisory se clasifica, con su porqué:
   - **runtime-alcanzable:** el paquete lo importa código de un `deployable` en `scope: prod`;
   - **función usada:** si el advisory nombra una función (`XLSX.read`, `qs.parse` con coma) y
     el grafo tiene llamadas a ella → prioridad máxima (best-effort por nombre, INFERRED);
   - **transitiva sin uso directo** y **solo build** (`dev` o etapa de build) → prioridad baja.
5. **Fin de soporte de runtimes:** los nodos `runtime` llevan su fecha de EOL (de la caché o de
   la config), y `overview`/`stats` muestran un aviso como el de frescura:
   *"⚠ runtime node 20 fuera de soporte desde 2026-04-30"*.
6. **Disponibilidad del arreglo en la imagen** (caso 8): fuera de alcance como dato propio; si el
   usuario ingiere erratas de su distribución (o OSV las trae para esa distro), se muestra
   *"arreglo publicado para python3.12, no para python3.11"*. Si no, se dice que no se sabe.

**Pruebas.** Fixtures de `npm audit --json` y `pip-audit` → nodos `advisory` y clasificación
esperada en un mini-proyecto con un paquete de runtime, uno de dev y uno transitivo. Sin
`--online` no hay ni un intento de red (test con socket bloqueado). Caché vencida → se avisa la
fecha, no se usa en silencio.

**Riesgo/Esfuerzo.** Medio. La línea que no se cruza: **no** mantener una base de vulnerabilidades
propia ni decidir severidades; solo enlazar las de fuentes públicas con el grafo.

---

### M25 · Evidencia de verificación y paridad de runtime  (PROPUESTA)

**Contexto.** El caso 7 es el más caro de todos por lo engañoso: una regresión A/B completa
(API, SQL, GUI, agentes) dio verde, y el sistema se cayó en producción. No era mala la prueba:
corrió con **otra versión exacta** de Node. Nada registraba con qué runtime se verificó ni con
cuál se despliega.

**Propuesta.**
1. **Versión desplegada, desde el log del build:** el compilador de contexto (`digest`) ya
   destila logs. Se agregan patrones configurables para líneas como `node v24.21.0 / npm 11.19.0
   / Python 3.12.15` → atributo `deployed_version` del runtime del `deployable`, con procedencia
   `build.log:línea`. Alternativa sin logs: `runtime.targets` en la config.
2. **Nodos de verificación** a partir de resultados que el usuario ya produjo (JSON de un harness
   de regresión, JUnit XML): `verification:<id>` con fecha, ref de git, **runtime exacto usado** y
   fases que pasaron, enlazado con `verified` al desplegable. MemoryGraf no ejecuta pruebas (§4.2).
3. **Avisos:** *"la última verificación del backend corrió con node 24.22.1; la imagen usa
   24.21.0"*; *"el upgrade-plan exige prueba de resistencia (regla `native-cpp-api-runtime-change`)
   y no hay evidencia de una"*. Se integran en la sección "Qué probar" de M22.

4. **Qué simuló la verificación** (caso 9). Una prueba que **reemplaza** un paquete (mock,
   simulador, fake del driver) no ejecutó su código, y por lo tanto no es evidencia sobre él.
   El JSON de verificación puede declarar `simulated: ["pkg:npm/oracledb"]`; con eso, un hallazgo
   de M26 o M22 sobre ese paquete **no** se da por cubierto: *"la regresión del 2026-10-23 simuló
   oracledb; `runtime-api-removed` sobre oracledb no tiene evidencia de ejecución"*.

**Pruebas.** Log de build sintético → versión extraída con su línea. JSON de verificación con
runtime distinto al desplegado → aviso. Sin log ni config → "versión desplegada desconocida", sin
inventar. Verificación que declara un paquete simulado + hallazgo sobre ese paquete → sigue
abierto.

**Riesgo/Esfuerzo.** Bajo. Es ingesta y comparación de strings sobre piezas que ya existen
(`digest`, nodos `module`).

---

### M26 · APIs del runtime eliminadas o cambiadas, también dentro de las dependencias  (PROPUESTA)

**Contexto.** El caso 9 apareció **después** de que el sistema ya estaba desplegado y estable: el
backend arrancaba, la API respondía y la regresión A/B había pasado, pero el primer operador que
quiso finalizar una orden recibió `util.isDate is not a function`. Node 23 eliminó las funciones
`util.is*` (obsoletas desde Node 4, DEP0047); `node-oracledb` 5.x las sigue usando: su capa
nativa llama a `_isDate` → `util.isDate` con cada bind que sea un objeto, para distinguir una
fecha de una definición `{ dir, type, maxSize }`. Las consultas con binds escalares no pasan por
ahí, así que solo fallaba lo que tenía parámetros OUT. M21–M25 no lo ven: M22 no tiene regla
para "API eliminada", M24 mira binarios y esto era JavaScript, y `node_modules` no se indexa.

Se diagnosticó con dos scripts de una tarde que vale la pena convertir en hito, porque el
método resultó **preciso**: correr el **mismo** escaneo con el runtime viejo y con el nuevo y
quedarse solo con la diferencia. Sobre vmd_sys (backend + dependencias de producción, Node 20 →
24) dio **un único hallazgo, el real**; sobre las dependencias de build del frontend, 69
resultados idénticos en ambas versiones (falsos positivos del método que se anulan solos) y
cero diferencias. En Python (bot + agentes, 3.11 → 3.12) dio cero, y un archivo de control con
cinco eliminaciones conocidas (`SafeConfigParser`, `ssl.wrap_socket`, `imp`, `distutils`,
`asyncore`) salió entero solo con 3.12.

**Propuesta.**
1. **Catálogo declarativo de eliminaciones por runtime** (`runtime_apis/`, datos versionados
   con el código, cada entrada con su fuente):
   - **Node:** las obsolescencias en *End-of-Life* de la documentación oficial (código `DEPxxxx`,
     versión en que se eliminó, reemplazo). Ej.: `util.isDate` → eliminada en 23.0.0 →
     `util.types.isDate` / `instanceof Date`.
   - **Python:** las secciones *Removed* del *What's New* de cada versión (PEP 594 incluida):
     módulos (`imp`, `distutils`, `asyncore`, `asynchat`, `smtpd`…) y miembros
     (`ssl.wrap_socket`, `configparser.SafeConfigParser`, `unittest.TestCase.assertEquals`…).
   - **Generación, no tipeo:** un script de mantenimiento **del repo de MemoryGraf** (no del
     usuario) importa cada módulo del núcleo en cada versión soportada y diferencia sus
     exportaciones; la documentación aporta el porqué y el reemplazo. Así el catálogo no depende
     de que alguien se acuerde, y MemoryGraf sigue sin ejecutar runtimes en la máquina del
     usuario (§4.2).
2. **Detección estática en el código propio.** Con las gramáticas existentes (M11/M15): ligadura
   del módulo (`const util = require('util')`, `import util from 'node:util'`,
   `const { isDate } = require('util')`, `require('util').isDate`, `import ssl`/`from ssl import
   wrap_socket`) + acceso a miembro → `uses_api` hacia un nodo `pkg:generic/node#util.isDate`.
   Solo se reporta lo que **existe en la versión origen y falta en la destino**: es la misma
   diferencia que hizo preciso al script, aplicada al catálogo.
3. **Dentro de las dependencias, opt-in** (`deps.scan_installed`, el mismo de M24): un escaneo
   más simple (ligaduras + `\bvar\.miembro`, sin AST) sobre `node_modules/**/*.{js,cjs,mjs}` y
   `site-packages/**/*.py`, saltando `test/`, `docs/`, `examples/`. Cada hallazgo queda colgado
   del paquete (`pkg:npm/oracledb` → `uses_api` → `node#util.isDate`, procedencia
   `oracledb/lib/connection.js:línea`) y hereda la **alcanzabilidad** de M23: un paquete de
   `scope: prod` importado por un desplegable es prioridad alta; uno de build, baja.
4. **Cambios de comportamiento, nivel "revisar".** Entradas del catálogo de otro tipo: la API
   sigue existiendo pero cambió. Ejemplos del propio caso: `AsyncLocalStorage` reimplementado
   sobre `AsyncContextFrame` en Node 24 (relevante si se usa `enterWith`; con `run` no cambia),
   versión de ICU (formato de fechas y números con locale implícito), OpenSSL incluida vs la del
   sistema (cifrados que acepta `ssh2`). Se marcan solo si el proyecto usa esa API **y** la
   actualización cruza la versión del cambio, y se cierran con evidencia (M25): en vmd_sys,
   `podman exec … node -p process.versions` mostró ICU 78.3 con el mismo formato byte a byte que
   Node 20, y la OpenSSL 1.1.1k del sistema operativo (no una incluida en Node).
5. **Shims reconocidos.** Si el proyecto restaura la API antes de usarla (`util.isDate = …`
   en un módulo que se carga al arrancar), el hallazgo pasa a **mitigado** con la procedencia
   del shim, en vez de marcarse para siempre. Y al revés: cuando ninguna dependencia la usa ya
   (p. ej. al subir a `oracledb` 6.x), avisa *"shim retirable: src/config/nodeCompat.js"*.
6. **Superficie, sin tools nuevas (§3.9):** regla `runtime-api-removed` /
   `runtime-behavior-change` en el `upgrade-plan` de M22 (con la lista de usos por desplegable),
   y `get node#util.isDate` muestra quién la usa, en qué versión desapareció y su reemplazo.

**Límite honesto.** Es análisis estático y lo dice. No ve acceso dinámico (`util[nombre]`,
`getattr(mod, x)`), alias que cruzan archivos, ni una API llamada **desde código nativo** sin
pasar por JS (en el caso 9 sí pasaba por JS: `_isDate` envuelve `util.isDate`, y eso es lo que
se encuentra). Tampoco sabe si el camino que la usa se ejecuta: por eso el mensaje dice **qué
funcionalidad probar** (los archivos del proyecto que llegan a ese paquete), no que vaya a
fallar. Todo hallazgo es INFERRED con su evidencia.

**Pruebas.** Fixture de `node_modules` mínimo con un paquete que usa `util.isDate` en un
`_isDate` envoltorio y otro que usa `util.types.isDate` → solo el primero, con su línea, en un
plan `--runtime node --from 20 --to 24`; el mismo plan `--to 22` → nada. Variable local llamada
`util` que no viene de `require('util')` → nada. Shim en el proyecto → "mitigado". Python: el
archivo de control de cinco eliminaciones → las cinco con `--to 3.12`, ninguna con `--to 3.11`.
Sin `deps.scan_installed` → no se abre ni un archivo de `node_modules` (test que lo verifica).
Catálogo: cada entrada tiene fuente y versión, y el generador es determinista.

**Riesgo/Esfuerzo.** Medio. El detector es sencillo (ya probado en el caso real); el trabajo está
en el catálogo y en mantenerlo por versión. Riesgo de ruido en dependencias grandes: mitigado por
la diferencia origen/destino (lo que ya fallaba igual en ambas no se reporta) y por la
alcanzabilidad (lo que no llega a producción baja de prioridad).

---

### Criterio de aceptación del anexo: reproducir el caso real

El anexo está **listo** cuando, sobre el repo de vmd_sys y su historia, `upgrade-plan
--from-ref <antes> --to-ref <después de subir a Node 24>` y los hitos asociados reportan,
**sin intervención manual**:

| Caso | Debe detectarlo | Cómo |
|---|---|---|
| 1 · Node 20 EOL | M23 | aviso de EOL en el runtime del desplegable |
| 2 · triage de 38 vulnerabilidades | M23 | `xlsx` runtime-alcanzable con usos; `postcss`/`vite` solo build |
| 3 · npm 11 y los install scripts | M22 | regla `install-script-npm11` sobre los nativos |
| 4 · node-gyp y Python 3.6 | M21 + M22 | regla `node-gyp-python` con `Dockerfile:línea` del `python3` |
| 5 · crash de `better-sqlite3` 12 | M22 + M24 | `native-cpp-api-runtime-change` con los símbolos `node::` como evidencia → exige resistencia |
| 6 · prebuild GLIBC_2.34 | M24 | `prebuild-glibc` contra OL8 (2.28) |
| 7 · verificado con otra versión | M25 | aviso de paridad 24.22.1 ≠ 24.21.0 |
| 8 · `python3.11` sin parche en OL8 | M23 (si hay datos de la distro) | si no hay datos, lo dice: no se finge |
| 9 · `util.isDate` dentro de `oracledb` | M26 + M25 | `runtime-api-removed` en `oracledb/lib/connection.js` con prioridad alta (prod), "mitigado" al aplicar el shim, y aviso de que la regresión simuló ese paquete |

El caso 8 queda condicionado a datos externos y se acepta que salga como "no se sabe". El resto
debe salir en verde, y ese repo se convierte en el fixture de integración del anexo.

### Lo que esta capa NO hará (y por qué)

| Tentación | Por qué NO |
|---|---|
| Subir versiones o abrir PRs | Dependabot/Renovate lo hacen bien. MemoryGraf explica el impacto; no edita (§4.2). |
| Base de vulnerabilidades propia | OSV ya existe y es pública. Se consume por purl. |
| Ejecutar `npm audit`, `pip`, builds o tests | Solo lectura (§4.2). Se ingiere lo que el usuario ya generó. |
| Prometer que predice fallos de runtime | No se puede en estático. Se clasifica el riesgo y se exige la prueba (M22 + M25). |
| Ejecutar el código del proyecto con el runtime nuevo para "ver si algo falla" | Es la prueba del usuario, no del grafo (§4.2). M26 lee código y un catálogo; los runtimes solo se ejecutan al **generar** el catálogo, en el repo de MemoryGraf. |
| Modelar la red/infra (DNS, proxies, nginx) | El caso real tuvo un 502 por nginx resolviendo el backend una sola vez; es configuración de infraestructura, no una dependencia. Queda fuera para no diluir la capa. |

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
