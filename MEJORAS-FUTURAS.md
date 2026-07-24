# MemoryGraf — Mejoras futuras (deuda consciente + potenciación)

> **Estado:** backlog vivo. **Fecha:** 2026-07-23.
> **Autor:** Jefferson J. Patiño Ortega (con Claude como copiloto).
> **Relación:** complementa `DESIGN.md` (principios §3, vinculantes). El roadmap de las
> capas 0–3 ya está implementado (vive en el código). Aquí viven las **limitaciones conscientes** anotadas en
> el código y las oportunidades de potenciación, cada una con **plan de implementación**
> y **pruebas a realizar después**. Nada de esto es un bug: el sistema funciona y degrada
> bien sin estas mejoras.

---

## 0. Cómo usar este documento

Cada mejora tiene: **contexto** (la limitación actual y dónde vive en el código),
**plan de implementación** (pasos concretos), **pruebas post-implementación** (cómo
validar sin introducir bugs) y **riesgo/esfuerzo**. Regla de oro (DESIGN §11): cada
mejora es entregable y medible por sí sola; se implementa con test de regresión y
verificación en vivo antes de dar por buena.

## 1. Backlog priorizado

| # | Mejora | Valor | Riesgo | Esfuerzo |
|---|---|---|---|---|
| M1 | Co-cambio por símbolo con **historia completa** — ✅ **on-demand** (`impact --deep` + disparador + LLM); 🔬 full-repo diferido | Alto | Alto | Alto |
| ~~M2~~ | ✅ **Implementado** · `tested_by` a nivel **símbolo→test** (contextos de cobertura) | Alto | Medio | Medio |
| ~~M3~~ | ✅ **Implementado** · Narrar el "por qué" del co-cambio de **símbolos** | Medio | Bajo | Bajo |
| ~~M4~~ | ✅ **Implementado** (multi-lenguaje TS/JS) · `resolved_type` + params/vars *(params/vars pend.)* | Medio | Medio | Medio |
| ~~M5~~ | ✅ **Implementado** · `digest`: formatos **agrupados** (eslint stylish, jest, go test, tsc) | Medio | Medio | Medio |
| ~~M6~~ | ✅ **Implementado** · Escala: `git blame` **paralelo** (lectura) en repos grandes | Medio | Medio | Medio |
| ~~M7~~ | ✅ **Implementado** · Narrativa/rerank con **LLM local** opt-in (Ollama) | Bajo | Bajo | Bajo |
| ~~M8~~ | ✅ **Implementado** · Co-cambio **cross-project** por símbolo, gateado y conservador | Bajo | Medio | Medio |
| ~~M4b~~ | ✅ **Implementado** (params Python **y TS/JS**) · `resolved_type` de **params individuales** (hover por offset) | Bajo | Medio | Medio |
| M9 | `calls`/`imports` **cross-file** para los lenguajes nuevos (C/C++/Java/C#/Go/Rust/PHP/R/VB) | Medio | Medio | Alto |

---

## M1 · Co-cambio por símbolo con historia completa

**Estado (2026-07-23) — ✅ INTEGRADO BAJO DEMANDA (A+B+C).** En vez de pagar el coste
global en cada `sync` (medido en ~14-23x, ver más abajo), se integró la ruta **on-demand y
acotada** — más barata, honesta y alineada con la filosofía "paga por lo que consultas":

- **A · `impact <símbolo> --deep`** (`memorygraf/deep_history.py::deep_cochange`): co-cambio
  por historia completa restringido al **historial del archivo** del símbolo (`git log
  --follow -- <archivo>`), así el walk es proporcional a ESE archivo, no al repo. Expuesto en
  CLI (`impact --deep`) y MCP (parám. `deep`; resuelve la raíz desde el meta `git_roots`, así
  funciona sin config). Marca `[NUEVO vs blame]` lo que el co-cambio del sync no vio.
- **B · disparador heurístico** (`Query._suggest_deep`): si un símbolo vive en un archivo de
  **churn alto** (historia completa, numstat) pero **no tiene co-cambios** registrados,
  `impact` sugiere `--deep`. No es certeza: reconoce la FORMA de una pérdida probable (honesto
  — no se puede "detectar" globalmente lo que el blame no computó).
- **C · narrativa** (`deep_history.explain`): el "por qué" del acoplamiento profundo con el
  LLM local si YA está activo (sin cold-start sorpresa), si no heurístico (reusa M3). Degrada.

Tests: `TestDeepImpact` (A halla lo que el blame pierde, B sugiere, C degrada, determinismo +
evidencia, resolución por `git_roots`). El **full-repo siempre-encendido** queda diferido (el
prototipo de abajo mide su coste y por qué no compensa integrarlo global).

**Prototipo full-repo (medido, no integrado).** Prototipo en
`prototype_m1_history_cochange.py` (raíz del repo, NO importado por el paquete). Sigue la
opción (b): por cada commit, `git show --unified=0` para los rangos post-image + `git show
<sha>:<path>` y **re-extracción AST** de esa versión, con caché por (sha,path). Los ids que
produce el extractor ya son `{project}/{path}::{qn}`, así que casan con el grafo actual sin
mapeo extra.

- **Beneficio CONFIRMADO.** Test `TestM1Prototype.test_finds_cochange_that_blame_misses`:
  dos funciones co-editadas en commits viejos y luego reescritas por completo → el prototipo
  emite el par (cnt=2); el blame actual NO (aristas 0). Determinismo verificado.
- **Coste MEDIDO (veredicto).** Frente al `sync` actual (blame): **~14x** (20 archivos ×
  30 commits) y **~23x** (50 × 50). Domina el `git show`+re-AST por cada (commit, archivo).
  Es O(commits × archivos-tocados), 1-2 órdenes de magnitud más caro que el blame.
- **Recomendación:** NO integrar tal cual. Para integrarlo haría falta: (1) **acumulador
  incremental por SHA** persistido (procesar solo commits nuevos cada sync — amortiza el
  walk único), (2) **batching** de `git show`/AST, (3) **tope de profundidad** de historia.
  El prototipo aísla el riesgo y da el dato para decidir: hoy el blame (M2/M3 ya entregados)
  cubre el caso común; la historia completa solo compensa si aparece necesidad concreta de
  acoplamiento "profundo" que el blame pierda.

**Contexto.** Hoy el co-cambio por símbolo (`git_layer._rebuild_symbol_cochange`) se deriva
del **blame**, que atribuye cada línea a su ÚLTIMO commit. Es un acoplamiento "de
superficie": si dos símbolos se co-editaron en commits viejos cuyas líneas luego se
reescribieron, esa señal se pierde. El de **archivo** sí usa historia completa (`git log
--numstat`).

**Plan de implementación.**
1. Recorrer `git log -p --unified=0 -M` por commit y parsear los *hunks* (`@@ -a,b +c,d @@`)
   para obtener los rangos de línea cambiados por archivo **en ese commit**.
2. El problema duro: los números de línea del diff son **históricos** (estado en ese
   commit), no los actuales. Opciones: (a) mapear con `git blame --reverse` o
   `git log -L`, (b) reconstruir spans por commit con un checkout ligero (`git show
   <sha>:<path>` + re-extracción AST). Preferir (b) con caché por (sha,path).
3. Acumular co-cambio símbolo↔símbolo en un acumulador persistido (como el de archivo),
   incremental por SHA. Sustituye o complementa a `_rebuild_symbol_cochange`.
4. Mantener el tope anti-sweep (`cochange_max_symbols`) y los umbrales.

**Pruebas post-implementación.**
- Repo de prueba donde dos funciones se co-editan en un commit **viejo** y luego sus
  líneas se reescriben en commits posteriores independientes → la arista símbolo debe
  existir (con blame NO existiría). Regresión unitaria dedicada.
- Determinismo: dos corridas → mismas aristas. Incremental: un commit nuevo no recomputa
  todo.
- Escala: medir tiempo del diff-walk en un repo de miles de commits (comparar con el
  walk `--numstat` actual). Documentar el sobrecosto.
- No-regresión: el co-cambio de archivo y los tests existentes intactos.

**Riesgo.** Alto: mapear líneas históricas→spans actuales es la parte frágil; el checkout
por commit puede ser costoso. Requiere caché agresiva y límites. **Recomendación:** hacer
solo si M2/M3 no bastan; empezar por un prototipo medido antes de integrar.

---

## M2 · `tested_by` a nivel símbolo→test  ✅ IMPLEMENTADO

**Estado (2026-07-23).** Hecho. `runtime/tests.py` parsea `coverage.json` con contextos
(`coverage json --show-contexts`, tras `pytest --cov-context=test` o
`dynamic_context = test_function`) y emite aristas `tested_by` **símbolo(código)→símbolo(test)**,
EXTRACTED (se observó la ejecución; `confidence.py` promueve provenance `coverage-*` a
EXTRACTED). Mapea línea→span de símbolo (el más ajustado) y contexto→símbolo de test
(acepta nodeid `a/b.py::C::m` y qualname punteado `a.b.C.m`). El fallback archivo→archivo
por imports se mantiene cuando no hay contextos; al retirar el artefacto, las aristas de
símbolo se limpian (anti-staleness). Config opcional: `runtime.coverage_contexts` (ruta) o
autodescubre `coverage.json`. Tests: `test_tested_by_symbol_from_coverage_contexts`,
`test_symbol_tested_by_falls_back_and_clears`, y el caso EXTRACTED en `TestConfidence`.
El resto de esta sección queda como registro del plan original.

**Contexto.** Antes `tested_by` (`runtime/tests.py::_build_tested_by`) era **archivo→archivo**,
INFERRED por imports del test. No decía qué **función** ejercita un test.

**Plan de implementación.**
1. Usar cobertura **por contexto de test**: `coverage run --context=test` /
   `pytest --cov-context=test` produce, por línea, qué test la ejecutó.
2. Parsear el `coverage.xml`/SQLite de coverage con contextos: línea → tests que la tocan.
3. Mapear líneas cubiertas por un test a los **spans de símbolo** (ya lo hacemos para
   `covered`) → emitir arista `tested_by` símbolo→test (EXTRACTED, alta confianza).
4. Mantener el fallback archivo→archivo por imports cuando no hay contextos.

**Pruebas post-implementación.**
- Un test que cubre solo `foo()` → arista `tested_by` a `...::foo`, no a otros símbolos
  del archivo. Regresión unitaria con coverage-contexts real.
- `impact(<símbolo>)` marca "SIN test" correctamente a nivel símbolo.
- Anti-staleness: borrar contextos → vuelve al fallback por imports sin basura.
- Degradación: sin coverage-contexts, comportamiento actual intacto.

**Riesgo.** Medio: depende del formato de contextos de coverage.py (bien documentado).
Contenido a `runtime/tests.py`.

---

## M3 · Narrar el "por qué" del co-cambio de símbolos  ✅ IMPLEMENTADO

**Estado (2026-07-23).** Hecho. `compile_cochange_notes` ahora itera las aristas
`co_changes_with` reales (archivo↔archivo Y símbolo↔símbolo) en vez del acumulador de
archivos; `_shared_subjects` ya servía por node id (símbolos persisten sus commits en
`_attr_symbol`). Además `query.history()` muestra el co-cambio de símbolos leyéndolo de
las aristas (antes solo consultaba el acumulador de archivo). Test:
`TestCompilerCochange.test_symbol_cochange_is_narrated`. El resto de esta sección queda
como registro del plan original.

**Contexto.** `context_compiler.compile_cochange_notes` iteraba solo el acumulador de
**archivos** (`git_cochange_all`), así que las aristas **símbolo↔símbolo** existían pero
no llevaban narrativa (`impact`/`history` las mostraban sin "↳ ...").

**Plan de implementación.**
1. Iterar también las aristas `co_changes_with` con extremos símbolo (o el nuevo
   acumulador de M1 si existe).
2. Reusar `_shared_subjects` (funciona por node id, incluidos símbolos) + el heurístico/LLM
   de narrativa ya existentes. Cachear por `content_hash` como las de archivo.
3. `cochange_note()` ya normaliza el par; extenderlo a símbolos.

**Pruebas post-implementación.**
- Test unitario: una arista símbolo↔símbolo obtiene su `ctx_note` (`kind='cochange'`).
- `impact`/`history` de un símbolo muestran "↳ co-cambian por ...".
- Auto-invalidación por versión de lógica (ya implementada) sigue funcionando.

**Riesgo.** Bajo: reusa toda la maquinaria existente. Buen primer paso tras M-actuales.

---

## M4 · `resolved_type` multi-lenguaje y de params/vars  ✅ IMPLEMENTADO (parcial)

**Estado (2026-07-23).** Hecho el multi-lenguaje. `runtime/lsp.py` pasó de Python-only a
un **registro por lenguaje** (`_LANGUAGES`): Python (pyright/pylsp/jedi) y TS/JS
(`typescript-language-server`, con `languageId` por extensión: ts/tsx/js/jsx). `sync`
agrupa los archivos por lenguaje, arranca un LSP efímero por cada uno con servidor
disponible (los que falten se omiten con degradación elegante) y limpia una sola vez para
que cada lenguaje solo AÑADA. `_parse_hover` descarta el fence de cualquier lenguaje.
`find_server()` mantiene la firma histórica (Python) por compat. Tests offline del
despacho + `TestLspTypeScript` (E2E guardado por disponibilidad del server).
**Pendiente:** tipos de **params/vars individuales** (hover en sus offsets) — sigue solo
la firma de la definición. El resto de esta sección queda como registro del plan.

**Contexto.** Antes `runtime/lsp.py` cubría solo **Python** (pylsp/pyright) y tomaba la
firma de la **definición**. No cubría TS/JS ni resolvía tipos de params/variables.

**Plan de implementación.**
1. Añadir servidores por lenguaje a `_PY_SERVERS` → un registro `{lang: (bin, args,
   languageId)}` (p.ej. `typescript-language-server --stdio`).
2. Agrupar archivos por lenguaje y arrancar el server correspondiente (efímero, como hoy).
3. (Opcional) Hover adicional en posiciones de params/atributos si se guardan sus offsets.

**Pruebas post-implementación.**
- Con `typescript-language-server`: un `.ts` tipado → `resolved_type` de una función.
- Cobertura: % de símbolos con tipo en un archivo real (reportar, no exigir 100%).
- Degradación: sin el server del lenguaje X, ese lenguaje se omite; Python intacto.

**Riesgo.** Medio: cada server tiene matices de handshake/hover. Contenido a `lsp.py`.

---

## M5 · `digest`: formatos de log agrupados  ✅ IMPLEMENTADO

**Estado (2026-07-23).** Hecho. `context_compiler.digest_log` suma cuatro parsers
AISLADOS (`_parse_tsc`, `_parse_go_test`, `_parse_eslint_stylish`, `_parse_jest`), cada uno
con su propio estado, que corren sobre todo el log DESPUÉS de los lineales (así el dedup da
prioridad a pytest/py). Son estrictos para no cruzar falsos positivos: tsc self-contained
(`file.ts(l,c): error TSxxxx`), go dentro de bloques `--- FAIL`, eslint por encabezado de
archivo + filas `l:c sev msg`, jest por `FAIL <path>` + `● título` + frame `at file:line:col`.
Tests: un fixture por herramienta + no-regresión (un log de pytest no dispara ninguno).
El resto de esta sección queda como registro del plan original.

**Contexto.** Antes `context_compiler.digest_log` reconocía formatos **lineales** (traceback
Python, pytest condensado, `path:línea: error:` de mypy/gcc). No parseaba formatos
**agrupados** (eslint "stylish": encabezado de archivo + `línea:col` debajo; jest;
`go test`; `tsc` con `archivo(l,c)`).

**Plan de implementación.**
1. Añadir parsers **con estado** (recordar el "archivo actual" del encabezado) para eslint
   stylish y jest; regex específicas para `go test` (`--- FAIL` + `file_test.go:NN:`) y
   `tsc` (`file.ts(12,5): error TSxxxx`).
2. Mantener cada parser aislado y con salida al mismo formato `(path, line, msg)`; sumar
   sin romper los actuales (orden de intento controlado, sin falsos positivos cruzados).

**Pruebas post-implementación.**
- Un fixture de log por herramienta (eslint/jest/go/tsc) → hallazgos con procedencia.
- No-regresión: los logs de pytest/traceback siguen extrayéndose igual (tests actuales).
- Robustez: logs mezclados/ruidosos no rompen (best-effort).

**Riesgo.** Medio: los formatos agrupados dan falsos positivos si el estado se contamina;
por eso quedaron fuera de la v1. Mitigar con parsers estrictos + muchos fixtures.

---

## M6 · `git blame` paralelo/por lotes (escala)  ✅ IMPLEMENTADO

**Estado (2026-07-23).** Hecho. `_blame_symbols` separa LECTURA de ESCRITURA: (1) construye
la work-list respetando la caché por `content_hash`; (2) corre los `git blame` (I/O-bound)
en un `ThreadPoolExecutor` ACOTADO (`_resolve_blame_workers`: `git.blame_workers`, 0=auto =
`min(8, cpu+2)`, 1=secuencial); (3) escribe a la BD en el HILO PRINCIPAL (SQLite/WAL a salvo
de concurrencia). Determinista: `ex.map` conserva el orden y cada símbolo se atribuye
independiente → mismas aristas/atributos que en secuencial (test dedicado que compara
paralelo vs. secuencial), + `PRAGMA integrity_check` == ok tras el sync paralelo. Medición
local (WSL): 300 archivos ~x2 (mayor en repos grandes / discos rápidos). El resto queda como
registro del plan; el punto 3 (`git log -L` agregado) sigue opcional/no necesario.

**Contexto.** Antes `_blame_symbols` hacía un `git blame` **por archivo** (secuencial). En la
prueba de escala, 2000 archivos → ~39 s de sync (blame es O(archivos)). En repos enormes
era el cuello de botella (limitación conocida de la capa Git: blame O(archivos)).

**Plan de implementación.**
1. Paralelizar el blame por archivo con un pool de procesos/hilos acotado (git es
   proceso externo → I/O-bound; hilos bastan).
2. Respetar el caché por `content_hash` (solo blamear lo cambiado).
3. (Opcional) Sustituir por `git log --numstat -L` agregado donde convenga.

**Pruebas post-implementación.**
- Escala: medir sync en 2.000/10.000 archivos, antes vs después; documentar speedup.
- Determinismo: el paralelismo NO debe alterar resultados (mismas aristas/atributos).
- Estabilidad de la BD (WAL) bajo el pool (integrity_check tras el sync).

**Riesgo.** Medio: concurrencia + escritura a SQLite (mantener la escritura en el hilo
principal; paralelizar solo la lectura de blame).

---

## M7 · Narrativa/rerank con LLM local por defecto  ✅ IMPLEMENTADO

**Estado (2026-07-23).** Hecho, como **opt-in** (no "por defecto": se respeta el coste del
`sync`, DESIGN §11). (1) Narrativa: `compile(force_llm=True)` fuerza el backend Ollama sin
tocar la config → CLI `memorygraf compile --llm` (espejo de `digest --llm`); también
`compiler.backend=ollama` para cada sync. (2) Rerank LLM: `context_compiler.rerank_llm`
DESTILA (solo permuta la lista dada; guardarraíl §6.4) con **presupuesto de latencia
estricto** (`_LocalLLM.generate(timeout=...)`), **fallback determinista** (`rerank`) si no
hay LLM/expira/responde inválido, y **caché** por (query, candidatos) en
`ctx_note(kind='rerank')`. Expuesto en `Query.search(rerank='llm', config=...)` y CLI
`search --rerank` / `--rerank-llm`. Tests offline con LLM falso (`TestRerankLlm`,
`test_compile_force_llm_uses_local_model`). El resto queda como registro del plan.

**Contexto.** Antes, en el `sync`, la narrativa de co-cambio usaba solo el **heurístico** (el
LLM era opt-in por coste). El rerank en consulta era determinista (LLM diferido por latencia).

**Plan de implementación.**
1. Si Ollama está disponible y el usuario opta (`compiler.backend=ollama`), usar el modelo
   local para narrativas más ricas (ya cableado; falta hacerlo cómodo/documentado).
2. Rerank LLM opt-in en consulta con presupuesto de latencia estricto y caché.

**Pruebas post-implementación.**
- Narrativa LLM vs heurística en un set fijo (calidad subjetiva + que no rompa el budget).
- Rerank: latencia acotada; fallback determinista si expira.

**Riesgo.** Bajo: todo detrás de flags, con fallback existente.

---

## M8 · Co-cambio cross-project por símbolo  ✅ IMPLEMENTADO

**Estado (2026-07-23).** Hecho, y además CORRIGE un comportamiento previo poco honesto:
`_rebuild_symbol_cochange` agrupa por SHA global, así que en un repo COMPARTIDO ya formaba
pares cross-project **sin gating** (mismo umbral laxo → falsos positivos: cualquier commit
que tocara ambos proyectos los enlazaba). Ahora un par cross-project solo se emite si (1)
los dos proyectos comparten la MISMA raíz de repo git (historia común real), (2) supera
umbrales MÁS ESTRICTOS (`cochange_cross_min`=3, `cochange_cross_threshold`=0.5), y (3) están
CONFIRMADOS por `cross_link` (endpoints compartidos → aristas `references` cross-project),
salvo que se ponga `cochange_cross_confirm=false`. Provenance distinta `git-cochange-sym-xproj`
(identificable). El co-cambio de ARCHIVO sigue intra-proyecto (bump por proyecto). Tests:
suprimido sin confirmación, formado con endpoint compartido, `confirm=false` solo-umbral, y
umbral estricto que suprime. El resto queda como registro del plan (los "commits
correlacionados" entre repos DISTINTOS quedan fuera: SHAs disjuntos, señal demasiado
especulativa). 

**Contexto.** El co-cambio de ARCHIVO se computa dentro de cada proyecto. El de SÍMBOLO
cruzaba proyectos accidentalmente (SHA global) sin validación; dos símbolos de repos/carpetas
distintas que cambian juntos (monorepo lógico) necesitaban un enlace honesto y acotado.

**Plan de implementación.**
1. Cuando varios proyectos comparten repo git (o commits correlacionados), permitir pares
   cross-project bajo umbrales más estrictos.
2. Reusar `cross_link` (endpoints) como señal de confirmación para no cruzar ruido.

**Pruebas post-implementación.**
- Workspace de 2 proyectos con commits que tocan ambos → arista cross-project acotada.
- No cruzar proyectos que solo comparten nombres de archivo por casualidad.

**Riesgo.** Medio: falsos positivos entre proyectos; empezar muy conservador.

---

## M4b · `resolved_type` de params individuales  ✅ IMPLEMENTADO (Python y TS/JS)

**Estado (2026-07-24).** Hecho para **parámetros** en **Python y TS/JS**. Proveedor por
lenguaje en `_LANGUAGES` con contrato `param_offsets(source, ext) -> {qualname: [(param,
[posiciones])]}`:
- **Python** (`python_ast.param_offsets`, vía `ast`): posiciones candidatas = DEFINICIÓN en la
  firma (pyright resuelve ahí) + PRIMER USO en el cuerpo (jedi/pylsp resuelve ahí).
- **TS/JS** (`ts_treesitter.param_offsets`, vía tree-sitter): posición del identificador del
  param en la firma (typescript-language-server resuelve ahí); cubre función/método/arrow
  (con y sin paréntesis), params opcionales y `rest`; salta `this` y destructuring (sin nombre
  único). El `ext` elige el parser (ts/tsx/js).

`runtime/lsp.py` hace hover en esas posiciones (def primero) y guarda `param_types` (JSON) en
`runtime_node` (columna + migración idempotente `ALTER TABLE`). `query.get()` lo renderiza
(`params: a: int, ...`). Respeta el presupuesto de hover y el toggle `runtime.param_types`.
Tests: offsets Python (def+uso, salta self) y TS/JS (func/método/arrow/rest, salta
destructuring), render determinista, y E2E guardados (Python + TS).

**Veredicto honesto (medido).** El mecanismo funciona, pero para código **anotado** el tipo
del param YA está en la firma (`resolved_type`), así que `param_types` es en buena parte
**redundante**; el valor NUEVO (params **inferidos** sin anotación) depende del servidor
(**pyright** limpio en la definición; **pylsp/jedi** vía uso, más verboso; **tsserver** limpio
en la definición). **Variables locales** (no params) quedan fuera (muchísimas, valor marginal).

**Contexto.** M4 dejó el multi-lenguaje. Faltaba el punto 3 de su plan: tipos de parámetros
individuales, no solo la firma. `_collect_types` hacía UN hover por símbolo (definición) y
guardaba un único `resolved_type`.

**Plan de implementación.**
1. Extractores (`python_ast.py`, `js_ts.py`, `ts_treesitter.py`): capturar y persistir los
   **offsets** (línea, col) de cada parámetro/variable relevante del símbolo.
2. Modelo/store: los params/vars NO son nodos; persistir un atributo estructurado en el
   símbolo, p.ej. `params_types = {"a": "int", "b": "int"}` (JSON, caché regenerable).
3. `runtime/lsp.py`: un hover por offset (respetando el presupuesto de tiempo del LSP).
4. `query.py`: renderizar esos tipos en `get`/`neighbors`.

**Pruebas post-implementación.**
- Función con params anotados vs. inferidos → los tipos por-param quedan poblados.
- Presupuesto: N hovers extra no revientan `hover_budget`; degradación por-símbolo intacta.
- Determinismo y anti-staleness (re-sync limpia y repuebla).

**Riesgo.** Medio: toca cada extractor por lenguaje (offsets) y añade peticiones LSP.
**Recomendación:** hacer solo si aparece necesidad concreta de tipos de locales/vars
inferidas; el 80% del valor (firma con tipos) ya se entregó en M4.

---

## M9 · `calls`/`imports` cross-file para los lenguajes nuevos

**Contexto.** El indexado multi-lenguaje (§18.7 de DESIGN, `extractors/ts_generic.py`) entrega
para C/C++/Java/C#/Go/Rust/PHP/R/VB: símbolos + `defines` + `calls` **intra-archivo**. Falta el
`calls`/`imports` **cross-file** de alta fidelidad, que hoy solo tienen Python (`ast`) y JS/TS
(`ts_treesitter`, vía `bindings` de import + resolución diferida en el indexador).

**Plan de implementación.**
1. Por gramática, parsear los **imports** y construir `raw_imports` (módulos) y `bindings`
   (nombre local → módulo/símbolo): Go `import_spec`, Java `import_declaration` (scoped), Rust
   `use_declaration`, C/C++ `preproc_include`, C# `using_directive`, PHP `use`/`require`, R
   `library()`/`source()`, VB `imports_statement`.
2. Emitir `calls_out` (llamadas no resueltas localmente) desde `ts_generic` — ya se detecta el
   callee en el pase intra-archivo; basta propagar los no resueltos con el alias del objeto.
3. Reusar la resolución cross-file del indexador (la misma que usa JS/TS) para atar
   `calls_out` + `bindings` → aristas `calls`/`imports` entre archivos.
4. Mapear módulos a nodos `file`/`external` por lenguaje (rutas relativas vs paquetes).

**Pruebas post-implementación.**
- Por lenguaje: `f` en `a` importa y llama a `g` de `b` → arista `calls` cross-file + `imports`.
- No-regresión: intra-archivo y los lenguajes actuales (Python/JS/TS) intactos.
- Degradación: import no resoluble → se omite (sin aristas colgantes).

**Riesgo.** Medio, esfuerzo Alto: la sintaxis de import y la resolución de módulos difiere mucho
por lenguaje (rutas relativas en C/Go/Rust vs paquetes en Java/C#). Empezar por 1–2 lenguajes
de import simple (Go, Java) como prueba, y extender.

---

## 9. Validación de ENTORNO pendiente (no son features)

No requieren código nuevo, solo ejecutar el guion `E2E-INTEGRATION-TEST.md` en entornos
que no cubrimos en Linux/WSL:

- **Windows nativo real** (PowerShell + Python de Windows): rutas con `\`, mapeo de
  cobertura (`<sources>`), instaladores `install.ps1`, `setup-ollama` con winget. Aquí
  solo probamos WSL + un proxy de rutas.
- **Escala real** en un repo grande propio (miles de archivos con historia) para validar
  M6 y los tiempos de `sync`/`blame`.
- **macOS**: `setup-ollama` con brew, FSEvents del watcher.

**Criterio de "listo pleno":** el reporte §14 del E2E en verde en Windows nativo, sin
bugs de degradación (§12) ni de rutas.
