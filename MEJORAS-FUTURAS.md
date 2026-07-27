# MemoryGraf — Deuda consciente y validación pendiente

> **Estado (2026-07-26).** El backlog **M1–M9 está IMPLEMENTADO y probado**. La fuente de
> verdad es el **código + los tests (`tests/test_memorygraf.py`) + el historial de commits**;
> este documento ya no es un "roadmap de cosas por hacer", sino un registro corto de:
> **(a)** lo hecho, **(b)** la **deuda consciente** que conviene recordar (por qué algo va
> gateado/acotado), y **(c)** lo único pendiente de ejecución: la **validación de ENTORNO** (§9).
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

## M11 · Ampliar la capa LSP a más lenguajes  (PROPUESTA)

**Estado (2026-07-26).** Propuesta. Hoy `runtime/lsp.py` (`_LANGUAGES`) cubre LSP solo para
**Python** y **TypeScript/JS**. El resto de lenguajes que MemoryGraf **sí indexa** con
tree-sitter (Go, Rust, C/C++, Java, C#, PHP, R, VB, asm) obtiene **símbolos/`calls`/`imports`**
pero **no** la capa LSP (diagnósticos, `resolved_type`, `param_types`). `doctor`/`configure` ya
lo reportan honestamente ("indexado, SIN capa LSP"). No es imposibilidad: es alcance actual.

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

**Orden sugerido:** M11a = Grupo A (Go/Rust/C/C++) en una corrida · M11b = Java · M11c = C# ·
M11d = PHP/R. Cada sub-hito con su **instalable en `doctor`** (respetando entorno/OS, como
`ts-lsp`) y su **test** (fixture mínimo: un símbolo tipado + un error → el server devuelve
`resolved_type` y ≥1 diagnóstico; skip limpio si el server no está, como hoy).

**Riesgo/Esfuerzo.** Grupo A: **bajo** (config + tests, sin motor nuevo). Grupo B: **medio-alto**
(init por server). Reutiliza todo el andamiaje existente (cliente efímero, mapeo a `runtime_node`,
degradación elegante): el grueso es "conectar server + instalable + test", no lógica nueva.

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
