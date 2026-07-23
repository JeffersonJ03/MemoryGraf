# MemoryGraf — Mejoras futuras (deuda consciente + potenciación)

> **Estado:** backlog vivo. **Fecha:** 2026-07-23.
> **Autor:** Jefferson J. Patiño Ortega (con Claude como copiloto).
> **Relación:** complementa `PLAN-CAPAS-CONTEXTUALES.md` (roadmap) y `DESIGN.md`
> (principios §3, vinculantes). Aquí viven las **limitaciones conscientes** anotadas en
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
| M1 | Co-cambio por símbolo con **historia completa** (diff, no solo blame) | Alto | Alto | Alto |
| M2 | `tested_by` a nivel **símbolo→test** (no archivo→archivo) | Alto | Medio | Medio |
| M3 | Narrar el "por qué" del co-cambio de **símbolos** (hoy solo archivos) | Medio | Bajo | Bajo |
| M4 | `resolved_type`: **multi-lenguaje** (TS/JS) + params/vars | Medio | Medio | Medio |
| M5 | `digest`: formatos **agrupados** (eslint stylish, jest, go test, tsc) | Medio | Medio | Medio |
| M6 | Escala: `git blame` **paralelo/por lotes** en repos grandes | Medio | Medio | Medio |
| M7 | Narrativa/rerank con **LLM local por defecto** cuando Ollama está | Bajo | Bajo | Bajo |
| M8 | Co-cambio **cross-project** por símbolo (hoy dentro de un proyecto) | Bajo | Medio | Medio |

---

## M1 · Co-cambio por símbolo con historia completa

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

## M2 · `tested_by` a nivel símbolo→test

**Contexto.** Hoy `tested_by` (`runtime/tests.py::_build_tested_by`) es **archivo→archivo**,
INFERRED por imports del test. No dice qué **función** ejercita un test.

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

## M3 · Narrar el "por qué" del co-cambio de símbolos

**Contexto.** `context_compiler.compile_cochange_notes` itera solo el acumulador de
**archivos** (`git_cochange_all`), así que las aristas **símbolo↔símbolo** existen pero
no llevan narrativa (`impact`/`history` las muestran sin "↳ ...").

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

## M4 · `resolved_type` multi-lenguaje y de params/vars

**Contexto.** `runtime/lsp.py` cubre **Python** (pylsp/pyright) y toma la firma de la
**definición**. No cubre TS/JS ni resuelve tipos de parámetros/variables individuales.

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

## M5 · `digest`: formatos de log agrupados

**Contexto.** `context_compiler.digest_log` reconoce formatos **lineales** (traceback
Python, pytest condensado, `path:línea: error:` de mypy/gcc). No parsea formatos
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

## M6 · `git blame` paralelo/por lotes (escala)

**Contexto.** `_blame_symbols` hace un `git blame` **por archivo** (secuencial). En la
prueba de escala, 2000 archivos → ~39 s de sync (blame es O(archivos)). En repos enormes
será el cuello de botella (ya anotado en PLAN §4.7).

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

## M7 · Narrativa/rerank con LLM local por defecto

**Contexto.** En el `sync`, la narrativa de co-cambio usa el **heurístico** (el LLM es
opt-in por coste). El rerank en consulta es determinista (LLM diferido por latencia).

**Plan de implementación.**
1. Si Ollama está disponible y el usuario opta (`compiler.backend=ollama`), usar el modelo
   local para narrativas más ricas (ya cableado; falta hacerlo cómodo/documentado).
2. Rerank LLM opt-in en consulta con presupuesto de latencia estricto y caché.

**Pruebas post-implementación.**
- Narrativa LLM vs heurística en un set fijo (calidad subjetiva + que no rompa el budget).
- Rerank: latencia acotada; fallback determinista si expira.

**Riesgo.** Bajo: todo detrás de flags, con fallback existente.

---

## M8 · Co-cambio cross-project por símbolo

**Contexto.** El co-cambio (archivo y símbolo) se computa dentro de cada proyecto. En
workspaces multi-proyecto, dos símbolos de repos distintos que cambian juntos (monorepo
lógico) no se enlazan por co-cambio.

**Plan de implementación.**
1. Cuando varios proyectos comparten repo git (o commits correlacionados), permitir pares
   cross-project bajo umbrales más estrictos.
2. Reusar `cross_link` (endpoints) como señal de confirmación para no cruzar ruido.

**Pruebas post-implementación.**
- Workspace de 2 proyectos con commits que tocan ambos → arista cross-project acotada.
- No cruzar proyectos que solo comparten nombres de archivo por casualidad.

**Riesgo.** Medio: falsos positivos entre proyectos; empezar muy conservador.

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
