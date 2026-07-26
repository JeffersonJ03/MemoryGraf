# MemoryGraf — Deuda consciente y validación pendiente

> **Estado (2026-07-26).** El backlog **M1–M9 está IMPLEMENTADO y probado**. La fuente de
> verdad es el **código + los tests (`tests/test_memorygraf.py`) + el historial de commits**;
> este documento ya no es un "roadmap de cosas por hacer", sino un registro corto de:
> **(a)** lo hecho, **(b)** la **deuda consciente** que conviene recordar (por qué algo va
> gateado/acotado), y **(c)** lo único genuinamente pendiente: la **validación de ENTORNO** (§9).
> Complementa `DESIGN.md` (principios §3, vinculantes).

---

## 1. Backlog M1–M9 — implementado

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
