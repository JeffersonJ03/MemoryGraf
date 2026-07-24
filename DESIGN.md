# MemoryGraf — Documento de Diseño

> **Estado:** v1.1 — implementado (Fases 0–5 completas, §17). Evolución post-v1.1 (capas
> contextuales Git/runtime/compilador + mejoras, incl. el prototipo M1): ver §18.
> **Fecha:** 2026-07-21 (diseño) · 2026-07-22 (cierre v1.1) · 2026-07-24 (capas contextuales)
> **Autor:** Jefferson J. Patiño Ortega (con Claude como copiloto de diseño)
> **Propósito:** Documento de referencia que define la visión, las reglas, el alcance
> y la arquitectura de MemoryGraf. Es la fuente de verdad del proyecto; se actualiza
> cuando una decisión cambia. El estado real de implementación vive en §17.

---

## 1. Resumen ejecutivo

**MemoryGraf** es un grafo de conocimiento local, portable y agnóstico al LLM que mapea
el contexto completo de un proyecto de software (código, decisiones, convenciones,
dominio de negocio) y lo expone mediante consultas dirigidas.

El objetivo es que un asistente LLM (Claude u otro) traiga a su contexto **solo el
subconjunto relevante** para la tarea actual, en lugar de cargar archivos completos y
documentos como `CLAUDE.md` enteros en cada sesión.

**Tesis de valor (honesta):** el ahorro de tokens NO viene de un "formato mágico que el
modelo entiende sin coste". Todo lo que entra al contexto del LLM son tokens. El ahorro
viene de **recuperación selectiva**: solo se inyecta lo relevante, y se evita la
exploración a ciegas (grep + lectura de archivos completos). El trabajo pesado
(indexar, buscar, recorrer relaciones) ocurre en la máquina local, en código normal,
sin coste de tokens.

---

## 2. Problema

Hoy, cuando se le pide a un asistente "ponte en contexto" de un proyecto:

1. Se carga `CLAUDE.md` / documentación **completa**, entera, cada sesión, aunque la
   tarea toque el 5% del proyecto.
2. El asistente **explora a ciegas**: `grep`, lee archivos completos "por si acaso".
3. Se re-explica lo mismo en cada sesión nueva.

El desperdicio no está en el *formato* (texto vs grafo) sino en cargar **todo** en vez
de **solo lo relevante para la tarea de ahora**.

---

## 3. Principios de diseño (las reglas)

Estas reglas son vinculantes. Cualquier decisión de implementación debe respetarlas.

1. **Fuente de verdad legible.** El grafo almacena hechos y estructura en formato
   abierto y legible por humanos y máquinas. Nada propietario, nada "opaco".
2. **Portabilidad primero.** El artefacto es independiente de cualquier LLM o
   plataforma. Otros modelos (y humanos) deben poder leerlo y usarlo.
   **Dependencias opcionales con degradación elegante** (revisión v1.1): el núcleo
   corre solo con la stdlib de Python (modo portable), pero se admiten dependencias
   OPCIONALES que aumentan la potencia (tree-sitter para JS/TS, embedder neuronal,
   watchdog). Si una no está instalada, el sistema cae automáticamente al modo
   portable. Nunca una dependencia es obligatoria para que el proyecto funcione.
3. **Retrieval selectivo, no compresión mágica.** Se es honesto sobre los tokens: el
   sistema recupera lo relevante; no promete "cero tokens".
4. **El trabajo pesado vive fuera del contexto del LLM.** Parseo, búsqueda y recorrido
   de grafo ocurren en la máquina local. Solo el resultado final se inyecta al modelo.
5. **Trazabilidad.** Toda respuesta incluye procedencia (`archivo:línea`) para que el
   LLM pueda leer el original si lo necesita. Nada de afirmaciones sin fuente.
6. **Incremental.** Re-indexar solo lo que cambió (detección por hash de contenido).
7. **Presupuesto de tokens explícito.** Cada consulta acepta un límite de tokens y
   nunca lo excede; degrada con elegancia (resúmenes en vez de detalle).
8. **Los vectores son caché regenerable, nunca fuente de verdad.** Si se usan
   embeddings, el texto/estructura crudo siempre permanece y permite re-generar el
   índice al cambiar de modelo.
9. **Superficie de herramientas mínima.** Cada herramienta expuesta cuesta tokens de
   esquema; se mantiene el conjunto pequeño y ortogonal.
10. **Determinismo.** La misma consulta sobre el mismo grafo da el mismo resultado
    (salvo el ranking semántico, que debe ser estable).

---

## 4. Alcance

### 4.1 Dentro de alcance (v1)

- Indexar código fuente y extraer símbolos, spans y relaciones estructurales.
- Extraer decisiones de arquitectura, convenciones y entidades de dominio desde
  fuentes designadas (p. ej. `DESIGN.md`, ADRs, `CLAUDE.md`, comentarios marcados).
- Almacenar todo como grafo en un archivo portable.
- Exponer consultas dirigidas vía servidor MCP + una CLI equivalente.
- Búsqueda híbrida (léxica + estructural; semántica opcional).
- Re-indexado incremental.
- Métricas de tokens antes/después.

### 4.2 Fuera de alcance (no-goals)

- **NO** promete "cero tokens" ni "el modelo entiende sin coste".
- **NO** reemplaza leer el código real cuando hace falta; lo hace más dirigido.
- **NO** almacena "el entendimiento del modelo" (no existe tal cosa que exportar);
  almacena hechos y relaciones.
- **NO** es un sistema en tiempo real para monorepos gigantes en v1 (indexado batch).
- **NO** ejecuta ni modifica código; es de solo lectura sobre el proyecto.
- **NO** sustituye al control de versiones ni a la documentación humana.

---

## 5. Arquitectura

Dos capas claramente separadas (esta separación es la que garantiza portabilidad):

```
┌──────────────────────────────────────────────────────────────┐
│  CAPA 2 — ACCESO (portable cross-LLM)                          │
│  ┌────────────────┐        ┌──────────────────────────────┐   │
│  │  Servidor MCP   │        │  CLI  (mismo motor por debajo)│   │
│  │  search/overview│        │  usable sin LLM / por scripts │   │
│  │  neighbors/get  │        │                              │   │
│  └───────┬────────┘        └───────────────┬──────────────┘   │
│          └──────────────┬───────────────────┘                  │
│                    Motor de consultas                          │
│         (ranking híbrido + recorrido de grafo + presupuesto)   │
└───────────────────────────┬──────────────────────────────────┘
                            │
┌───────────────────────────┴──────────────────────────────────┐
│  CAPA 1 — ALMACENAMIENTO (fuente de verdad, portable)          │
│  ┌──────────────┐   ┌──────────────┐   ┌───────────────────┐  │
│  │  Grafo        │   │ Export JSON  │   │ Índice vectorial  │  │
│  │  (SQLite)     │   │ (git-friendly│   │ (CACHÉ regenerable│  │
│  │  fuente verdad│   │  y humano)   │   │  — no es verdad)  │  │
│  └──────────────┘   └──────────────┘   └───────────────────┘  │
└───────────────────────────┬──────────────────────────────────┘
                            │
┌───────────────────────────┴──────────────────────────────────┐
│  INDEXADOR (offline / batch / incremental)                     │
│  descubrir archivos → parsear (tree-sitter) → extraer          │
│  símbolos/relaciones → extraer decisiones/convenciones →       │
│  resumir (cache por hash) → construir aristas → (embeddings)   │
└────────────────────────────────────────────────────────────────┘
```

- **Indexador:** construye y actualiza el grafo. No participa en tiempo de consulta.
- **Almacenamiento:** el grafo (SQLite) es la fuente de verdad; el export JSON es para
  portabilidad máxima y diffs en git; el índice vectorial es caché regenerable.
- **Acceso:** motor de consultas único, expuesto por dos frentes (MCP y CLI) que
  comparten la misma lógica.

---

## 6. Modelo de datos (esquema del grafo)

### 6.1 Nodos

Campos comunes a todos los nodos:

| Campo        | Tipo    | Descripción                                                   |
|--------------|---------|---------------------------------------------------------------|
| `id`         | string  | Identificador estable (ver §6.4).                             |
| `type`       | enum    | Tipo de nodo (ver abajo).                                     |
| `name`       | string  | Nombre legible.                                               |
| `path`       | string? | Ruta del archivo (relativa a la raíz), si aplica.            |
| `span`       | [int,int]? | Rango de líneas `[inicio, fin]`, si aplica.                |
| `summary`    | string  | Resumen corto (1–3 frases). Cacheado por `content_hash`.     |
| `tags`       | string[]| Etiquetas libres (dominio, capa, etc.).                      |
| `content_hash` | string| Hash del contenido fuente; base del re-indexado incremental. |
| `updated_at` | string  | ISO-8601.                                                    |

Tipos de nodo:

| `type`        | Qué representa                                              |
|---------------|------------------------------------------------------------|
| `file`        | Un archivo del proyecto.                                    |
| `symbol`      | Función, clase, método, constante/variable exportada.      |
| `module`      | Agrupación lógica (paquete, carpeta, feature).             |
| `decision`    | Decisión de arquitectura o de negocio (estilo ADR).        |
| `convention`  | Regla/convención del proyecto (naming, estilo, patrón).    |
| `entity`      | Concepto del dominio de negocio.                           |
| `external`    | Dependencia externa, API o servicio de terceros.           |
| `doc`         | Fragmento de documentación relevante.                      |

### 6.2 Aristas

Campos comunes:

| Campo         | Tipo   | Descripción                                             |
|---------------|--------|---------------------------------------------------------|
| `source`      | id     | Nodo origen.                                            |
| `target`      | id     | Nodo destino.                                           |
| `type`        | enum   | Tipo de relación.                                       |
| `confidence`  | float  | 0–1. Certeza (parseo=1.0; inferencia semántica <1.0).  |
| `provenance`  | string | Cómo se derivó (parser, heurística, LLM, manual).      |

Tipos de arista (todas dirigidas; el inverso se deriva en consulta):

| `type`            | Semántica                                              |
|-------------------|--------------------------------------------------------|
| `calls`           | A invoca a B.                                          |
| `imports`         | A importa a B.                                         |
| `defines`         | A (archivo/módulo) define a B (símbolo).              |
| `depends_on`      | A depende de B (build/paquete).                       |
| `implements`      | A implementa la interfaz/contrato B.                  |
| `references`      | A menciona/usa a B sin llamarlo directamente.         |
| `decided_because` | Decisión A tiene como razón B.                        |
| `governs`         | Convención A rige sobre archivos/símbolos B.          |
| `models`          | Entidad de dominio A se modela en símbolo/archivo B.  |
| `relates_to`      | Relación genérica (último recurso, con nota).         |

### 6.3 Reglas del esquema

- Toda arista de tipo estructural (`calls`, `imports`, `defines`, `depends_on`) tiene
  `confidence = 1.0` y `provenance = parser`.
- Aristas semánticas o inferidas por LLM llevan `confidence < 1.0` y son auditables.
- Ningún nodo se emite sin `summary`. Si no hay resumen aún, se genera o se usa un
  extracto determinista (p. ej. la firma + docstring).
- `entity`, `decision`, `convention` no tienen por qué mapear a un solo archivo.

### 6.4 Identidad estable de nodos

Los `id` deben sobrevivir a refactors razonables:

- `symbol` → `path::qualified_name` (p. ej. `validators/field.js::validarTelefono`).
- `file` → ruta relativa normalizada.
- `decision`/`convention`/`entity` → slug estable definido en la fuente
  (p. ej. `decision:telefonos-e164`).

Cuando un símbolo se mueve de archivo, el indexador intenta reconciliar por nombre +
firma para preservar historia y aristas.

---

## 7. Formato de almacenamiento

- **Primario:** **SQLite** (un solo archivo `memorygraf.db`). Portable, transaccional,
  consultable con SQL estándar, sin servidor. Ideal para consultas de grafo con joins
  y para incremental.
- **Export/import:** **JSON** canónico (`memorygraf.json`), ordenado de forma
  determinista para que los diffs en git sean legibles. Es la garantía de portabilidad
  máxima y de que ningún dato queda "atrapado" en un binario.
- **Índice vectorial:** archivo aparte (`memorygraf.vec` o tabla con extensión vectorial
  de SQLite). **Marcado explícitamente como caché regenerable.** Nunca es fuente de
  verdad; se puede borrar y reconstruir.

Regla: SQLite y JSON deben poder regenerarse el uno del otro sin pérdida.

---

## 8. Pipeline de indexado

Ejecución batch, idempotente e incremental:

1. **Descubrir** archivos respetando `.gitignore` y la config de `include/exclude`.
2. **Detectar cambios** por `content_hash`; solo se re-procesa lo modificado.
3. **Parsear** con **tree-sitter** (multi-lenguaje) → símbolos, spans, imports, llamadas.
4. **Extraer conocimiento no-código** desde fuentes designadas:
   - Decisiones: `DESIGN.md`, `docs/adr/*`, bloques marcados.
   - Convenciones: sección declarada en `CLAUDE.md` o `CONVENTIONS.md`.
   - Entidades de dominio: glosario declarado o inferencia marcada.
5. **Resumir** nodos (resumen corto). Estrategia:
   - Determinista primero (firma + docstring/comentario de cabecera).
   - LLM opcional en batch offline para resúmenes ricos; **cacheado por `content_hash`**
     para no re-pagar. (Este es el único punto donde el pipeline puede gastar tokens, y
     es offline, controlado y amortizado.)
6. **Construir aristas** desde datos de parseo (alta confianza) y, opcionalmente,
   inferencias marcadas (baja confianza).
7. **Embeddings (opcional)** de los `summary` para búsqueda semántica → índice vectorial.
8. **Persistir** en SQLite y exportar JSON.

Modo `watch` (fase posterior): re-indexa incrementalmente al detectar cambios en disco.

---

## 9. Capa de acceso — herramientas MCP

Superficie mínima y ortogonal. Todas devuelven **texto estructurado compacto** con
procedencia y respetan un `budget_tokens`.

| Herramienta | Entrada | Devuelve | Cuándo la usa el LLM |
|-------------|---------|----------|----------------------|
| `overview`  | `scope?` (proyecto o módulo), `budget_tokens` | Mapa de alto nivel: módulos, entidades clave, decisiones principales. | Al inicio de una tarea, para orientarse. **Reemplaza volcar `CLAUDE.md` entero.** |
| `search`    | `query`, `budget_tokens`, `types?` | Nodos relevantes rankeados con `summary` + `path:línea`. Ranking híbrido (léxico + estructural + semántico). | Para localizar dónde vive algo. |
| `neighbors` | `node_id`, `edge_types?`, `depth?` | Subgrafo conectado (qué llama/importa/depende, con quién se relaciona). | Para entender impacto y contexto de un nodo. |
| `get`       | `node_id` | Detalle completo del nodo + puntero exacto al span de código. | Para obtener la ubicación precisa antes de leer/editar el archivo real. |
| `decisions` | `topic?` | Decisiones y convenciones aplicables al tema. | Para respetar reglas del proyecto sin adivinar. |

Reglas de la capa de acceso:

- **Siempre incluir procedencia** (`path:línea`) para que el LLM lea el archivo real si
  hace falta. MemoryGraf orienta; no sustituye leer código cuando la tarea lo exige.
- **Degradación por presupuesto:** si el resultado excede `budget_tokens`, se devuelven
  resúmenes y punteros en vez de detalle, señalando que hay más disponible.
- La CLI expone exactamente las mismas operaciones (entrada texto → salida texto), de
  modo que cualquier LLM o script las use sin MCP.

---

## 10. Economía de tokens (cuándo vale la pena — honesto)

El ahorro es **real pero condicional**. Se documenta para decidir con datos.

De dónde sale el ahorro:
1. No cargar documentación/`CLAUDE.md` entero → `overview` devuelve solo lo aplicable.
2. Búsqueda dirigida en vez de exploración a ciegas (evita grep + lecturas "por si acaso").
3. Punteros a `archivo:línea` en vez de leer archivos completos.

Costes nuevos que hay que contar:
- Esquema de las herramientas MCP en contexto (coste fijo por sesión).
- Cada consulta + su respuesta cuesta tokens. Retrieval de baja calidad = más consultas.
- Re-indexado (CPU local, no tokens) y resúmenes LLM offline (tokens amortizados).

Matiz competidor honesto — **prompt caching:** un `CLAUDE.md` que se carga igual cada
sesión se cachea (los tokens cacheados cuestan una fracción). Eso recorta parte de la
ventaja en el escenario "cargar el .md". Donde MemoryGraf gana con claridad es en
**evitar la exploración de archivos**, que cambia por tarea y no se cachea bien.

| Escenario | ¿Ahorra? |
|-----------|----------|
| Proyecto grande (cientos de archivos), sesiones largas | Sí, mucho (estimado 50–80%) |
| Proyecto mediano, tareas puntuales | Sí, moderado |
| Proyecto pequeño (pocos archivos, `.md` chico) | No / negativo → no usar |

**Criterio de decisión:** medir tokens antes/después en un proyecto real (§12) antes de
invertir en fases avanzadas.

---

## 11. Portabilidad y agnosticismo de LLM

- **Capa de almacenamiento = 100% portable.** Son hechos y relaciones en SQLite/JSON;
  los entiende cualquier LLM y también un humano. No hay nada "de un modelo" dentro.
- **Capa de acceso = portable vía estándar.** MCP ya es adoptado por múltiples
  proveedores; el mismo servidor sirve a varios LLMs. La CLI da portabilidad total,
  incluso sin LLM.
- **Embeddings = único punto específico de modelo.** Los vectores dependen del embedder,
  pero **no rompen la portabilidad**: el texto crudo permanece y el índice vectorial se
  regenera al cambiar de modelo. Regla §3.8.

Garantía de diseño: en cualquier momento se puede exportar todo a JSON legible y
reconstruir el sistema con otro LLM/embedder sin pérdida de conocimiento.

---

## 12. Métricas de validación

El proyecto se justifica con números, no con teoría. Se miden:

- **Tokens de "puesta en contexto"** por tarea: baseline (hoy) vs con MemoryGraf.
- **Precisión de retrieval:** ¿las consultas devuelven los nodos correctos? (revisión
  manual sobre un set de tareas representativas).
- **Consultas por tarea:** menos y mejor dirigidas = mejor.
- **Coste/tiempo de indexado** y de re-indexado incremental.
- **Tamaño del grafo** vs tamaño del proyecto.

Meta de la prueba piloto: demostrar ≥40% de reducción de tokens de contexto en un
proyecto mediano-grande real sin pérdida de calidad de respuesta.

---

## 13. Stack tecnológico recomendado

- **Lenguaje:** Python (ecosistema maduro de tree-sitter, embeddings y SDK de MCP;
  ideal para el indexador). TypeScript es alternativa válida si se prefiere un solo
  lenguaje con el tooling de Claude Code.
- **Parseo:** tree-sitter (multi-lenguaje, tolerante a errores).
- **Almacenamiento:** SQLite (stdlib) + export JSON.
- **Búsqueda:** híbrida — SQLite FTS5 (léxica) + recorrido de grafo (estructural) +
  vectores opcionales.
- **Acceso:** SDK oficial de MCP + una CLI delgada sobre el mismo motor.
- **Embeddings (opcional, fase 3):** modelo intercambiable; local si se quiere cero
  dependencia de red.

---

## 14. Hoja de ruta por fases

Cada fase es entregable y medible por sí sola. No se avanza a la siguiente sin validar
la anterior.

- **Fase 0 — Esquema y almacenamiento.** ✅ SQLite + export/import JSON, identidad estable.
- **Fase 1 — Indexador estructural.** ✅ Python (`ast`) + JS/TS/TSX (tree-sitter, con regex
  como fallback). Nodos `file`/`symbol` + aristas `defines`/`imports`/`calls`. Incremental.
- **Fase 2 — Servidor MCP + CLI.** ✅ `overview`/`search`/`neighbors`/`get`/`decisions`/`stats`.
  Presupuesto y procedencia. **Ahorro medido: ~21× en tarea real (§12).**
- **Fase 3 — Semántica.** ✅ Resúmenes cacheados + embeddings (TF-IDF local / model2vec
  neural / API) + ranking híbrido (RRF) + `decisions`. Índice vectorial regenerable.
- **Fase 4 — Conocimiento de dominio.** ✅ `decision`/`convention` desde markdown +
  `entity` desde glosario. Aristas `governs`/`models`/`relates_to`.
- **Fase 5 — Escala y comodidad.** ✅ Multi-lenguaje, `watch` (polling + watchdog),
  reconciliación de símbolos movidos, empaquetado (`pyproject.toml`).

**Regla de oro del roadmap:** Fase 2 debe demostrar ahorro real de tokens antes de
invertir en Fases 3+. Si no ahorra en un proyecto grande, se replantea el enfoque.
→ Cumplida: la Fase 2 demostró ~21× de reducción antes de construir 3–5.

---

## 15. Riesgos y preguntas abiertas

- **Calidad de retrieval:** el valor entero depende de que `search` devuelva lo correcto.
  Mitigación: ranking híbrido y set de evaluación desde temprano.
- **Mantener el grafo fresco:** código que cambia y grafo que no → respuestas obsoletas.
  Mitigación: incremental por hash + (fase 5) modo watch + señalar `updated_at`.
- **Coste de resúmenes LLM:** offline y cacheado; vigilar que no se dispare.
- **Sobre-ingeniería:** el grafo solo aporta sobre RAG puro si se explotan las aristas.
  Si un proyecto no necesita relaciones, un vector store simple basta — no forzar grafo.
- **Break-even:** ¿a partir de qué tamaño de proyecto conviene? Responder con datos en
  Fase 2.
- **Abierto:** ¿límite de profundidad por defecto en `neighbors`? ¿formato exacto de
  salida más económico en tokens (YAML-like vs líneas planas)? Decidir con medición.

---

## 16. Glosario

- **Nodo:** unidad de conocimiento (archivo, símbolo, decisión, entidad…).
- **Arista:** relación dirigida entre dos nodos.
- **Retrieval selectivo:** traer solo el subconjunto relevante a la tarea.
- **Fuente de verdad:** el dato canónico (SQLite/JSON), nunca el índice vectorial.
- **Procedencia:** de dónde salió un dato o arista (parser, heurística, LLM, manual).
- **Presupuesto de tokens:** límite máximo de tokens que una consulta puede devolver.

---

## 17. Estado de implementación — cierre v1.1 (2026-07-22)

Fases 0–5 **completas**. El proyecto cubre todo el roadmap del diseño. Validado
end-to-end sobre dos proyectos reales de un sistema multi-repo, tratados como un
solo sistema.

### Capacidades entregadas

| Área | Estado | Notas |
|------|--------|-------|
| Almacenamiento SQLite + export JSON | ✅ | Fuente de verdad legible y portable |
| Indexado Python (`ast`) | ✅ | Exacto: símbolos, `defines`, `calls` (intra y cross-archivo) |
| Indexado JS/TS/TSX | ✅ | tree-sitter (exacto) con fallback regex; `calls`, `implements` |
| `calls` cross-archivo | ✅ | Resueltos vía bindings de import/require |
| Reconciliación de símbolos movidos | ✅ | Preserva aristas entrantes; se dispara con `calls` cross-archivo |
| Enlace cross-project (endpoints HTTP) | ✅ | Une varios repos como un solo sistema |
| Decisiones + convenciones (markdown) | ✅ | Con `governs` hacia el código |
| Entidades de dominio (`models`) | ✅ | Glosario `memorygraf.entities.json` (curable) |
| Resúmenes | ✅ | Heurístico (default) · Ollama local · API — cacheados por hash |
| Búsqueda híbrida (RRF) | ✅ | Léxico (FTS) + semántico |
| Embeddings | ✅ | TF-IDF local · model2vec neural (cross-idioma) · API — caché regenerable |
| Servidor MCP (6 herramientas) | ✅ | stdio JSON-RPC sin deps; recarga en caliente |
| `sync` / `watch` incremental | ✅ | polling (WSL/`/mnt`) + watchdog (nativo) |
| Suite de pruebas | ✅ | 12 tests (`python -m unittest discover -s tests`) |
| Empaquetado | ✅ | `pyproject.toml`, entry point `memorygraf`, extras `[full]` |

### Revisión de filosofía (v1.1): dependencias opcionales

El núcleo corre **solo con la stdlib** (modo portable). Las dependencias
(`tree-sitter`, `model2vec`, `watchdog`) y los backends externos (Ollama, API de
embeddings/resúmenes) son **opcionales**: aumentan la potencia y, si faltan, el sistema
**degrada con elegancia**. Ninguna es obligatoria. Ver `requirements-full.txt`.

### Métricas del grafo real

- 204 archivos → 1.767 nodos · 3.814 aristas.
- 9 tipos de arista: `calls` (907, de ellas 210 cross-archivo), `defines`, `depends_on`,
  `governs`, `implements`, `imports`, `models` (829), `references`, `relates_to`.
- 18 entidades de dominio; 11 decisiones; 47 convenciones.
- Ahorro medido en una tarea real: **~21× menos tokens** (40.445 → 1.889).

### Pipeline operativo

`sync` = `index` → `cross_link` → `docs` → `entities` → `summarize` → `embed`
(todo incremental; sube `sync_version` para recarga en caliente del MCP).
Deja `memorygraf watch` corriendo para mantener el grafo al día.

### Despliegue (v1.1): portable y agnóstico de IA

- **Instalable** (`pyproject.toml`, entry point `memorygraf`): `pipx install "memorygraf[full]"`
  o `install.sh`/`install.ps1` (pipx con fallback a venv). Extras opcionales `[full]`.
- **Por proyecto, sin rutas hardcodeadas** (`workspace.py`): `memorygraf init` crea
  `.memorygraf/config.json` (roots relativos) y `.memorygraf/graph.db`. La config/BD se
  descubren desde el CWD o vía `MEMORYGRAF_HOME`/`MEMORYGRAF_DB`.
- **Conexión a la IA como cualquier MCP**: `memorygraf install claude` (1 comando) o
  `memorygraf mcp-config` (imprime el JSON `mcpServers` para cualquier cliente MCP). El
  servidor se lanza con `memorygraf mcp`. Agnóstico de IA: MCP estándar + CLI.

### Elecciones conscientes (no deudas técnicas)

- **Resúmenes en prosa no son el default** (privacidad/coste): se activan con Ollama
  (local, privado) o API. El default heurístico es offline y gratuito.
- **`calls` en modo regex** (sin tree-sitter) se omiten para evitar falsos positivos.
- El **glosario de entidades** lo aporta y cura el proyecto; el bootstrap inicial es una
  propuesta, no una verdad absoluta.

---

## 18. Evolución post-v1.1: capas contextuales y mejoras (2026-07-24)

Sobre el núcleo v1.1 (Fases 0–5) se añadieron tres **capas contextuales** y un conjunto de
mejoras, todas fieles a §3 (portable, degradación elegante, procedencia, determinismo,
presupuesto de tokens, trabajo pesado fuera del contexto del LLM). El estado detallado por
mejora vive en `MEJORAS-FUTURAS.md`.

### 18.1 Capa 1 · Temporal / Git (`git_layer.py`)

Señales de historia que el AST no ve, derivadas de `.git` (caché regenerable, **nunca**
fuente de verdad):

- **Co-cambio** (`co_changes_with`, INFERRED): acoplamiento por co-edición. A nivel ARCHIVO
  (historia completa vía `git log --numstat`) y a nivel SÍMBOLO (por blame).
- **Churn, fragilidad (`fix`), autores, edad, top-commits** por nodo → consultas `history`,
  `working-set`, `impact` (blast radius = llamadas ∪ co-cambio).
- **Etiquetas de confianza** (`confidence.py`): EXTRACTED / INFERRED / AMBIGUOUS derivadas al
  vuelo de (tipo, provenance, confidence).
- **Escala (M6):** el `git blame` por símbolo paraleliza la LECTURA (pool de hilos acotado)
  manteniendo la ESCRITURA a SQLite en el hilo principal (WAL a salvo). Determinista.

### 18.2 Capa 2 · Verdad de runtime (`runtime/`)

Parsea artefactos que el proyecto YA produce (no ejecuta nada):

- **Cobertura** (`coverage.xml`) → `covered` / `coverage_ratio` por símbolo y archivo.
- **`tested_by`** código→test: archivo→archivo por imports (INFERRED); y **símbolo→test**
  (M2) desde CONTEXTOS de cobertura (`coverage json --show-contexts`), EXTRACTED.
- **JUnit** → `last_test_status` por símbolo de test.
- **LSP efímero** (`runtime --lsp`): `diagnostics` + `resolved_type` por símbolo y tipos por
  **parámetro** (M4b). **Multi-lenguaje** (M4): Python (pyright/pylsp/jedi) y TS/JS
  (`typescript-language-server`); pyright da la mejor calidad de tipos.

### 18.3 Capa 3 · Compilador de contexto local (`context_compiler.py`)

Un LLM pequeño y local (Ollama efímero) que **DESTILA y PLANIFICA, nunca razona la respuesta
final** (guardarraíl de honestidad). Todo con **fallback determinista** si no hay Ollama:

- **`digest`**: destila un log gigante (test/build) a lo esencial ligado a nodos. Formatos
  lineales (traceback/pytest/mypy) y AGRUPADOS (M5: eslint stylish, jest, go test, tsc).
- **Narrativa de co-cambio** (M3): el "por qué" de cada arista `co_changes_with` (archivo y
  símbolo), cacheado por `content_hash`.
- **Rerank** (M7): determinista por defecto (léxico + estructura + churn); con **LLM local
  opt-in** (`--rerank-llm`): presupuesto de latencia estricto + caché + fallback.

**Motor de LLM configurable (`setup-llm`).** Resúmenes y compilador aceptan tres motores con
degradación: `heuristic` (offline), `ollama` (local; elige/descarga modelo o importa un GGUF)
y `api` (endpoint compatible con OpenAI). `memorygraf setup-llm` los configura de forma
interactiva y escribe la config; la API key vive solo en `MEMORYGRAF_LLM_KEY` (nunca en el
archivo). Así el sistema es agnóstico del LLM también en la generación, no solo en el acceso.

### 18.7 Indexado multi-lenguaje (`extractors/ts_generic.py`)

La extracción estructural dejó de ser solo Python + JS/TS. Un extractor GENÉRICO dirigido por
config sobre tree-sitter cubre **C, C++, Java, C#, Go, Rust, PHP, R, Visual Basic y Assembly**:
emite `symbol` (funciones, clases/tipos, métodos `Clase.m`) + aristas `defines`, con extracción
de nombre por-gramática (campo `name`; casos especiales: declarator en C/C++, `type_spec` en Go,
`impl` en Rust, asignación en R, bloque en VB, `label` en asm). El indexador rutea por extensión:
Python→`python_ast` (exacto, con calls), JS/TS→`ts_treesitter` (exacto, con calls/imports
cross-file), el resto→`ts_generic`. Degrada a nodo `file` sin tree-sitter.

**Alcance honesto:** los lenguajes nuevos aportan **símbolos + `defines`** (potencian `overview`,
`search`, `get`, `neighbors`, `graph`, `report`, y el co-cambio a nivel símbolo de la Capa 1).
Los `calls`/`imports` cross-file de alta fidelidad siguen siendo de Python y JS/TS; extenderlos a
más lenguajes es una ampliación acotada por gramática (aún no hecha).

### 18.4 Co-cambio cross-project por símbolo (M8)

Muy conservador: dos símbolos de proyectos distintos solo se enlazan si (1) comparten repo
git, (2) superan umbrales más estrictos y (3) están CONFIRMADOS por `cross_link` (endpoints
compartidos). Evita falsos positivos entre proyectos.

### 18.5 M1 · Co-cambio por historia completa — prototipo medido y decisión de diseño

**Problema.** El co-cambio por símbolo se deriva del BLAME, que atribuye cada línea a su
ÚLTIMO commit: es un acoplamiento "de superficie". Si dos símbolos se co-editaron en commits
viejos cuyas líneas luego se reescribieron, la señal se pierde (el de archivo sí usa historia
completa).

**Prototipo (`prototype_m1_history_cochange.py`, NO integrado).** Recorre el diff de cada
commit (`git show --unified=0`) y re-extrae los símbolos de esa versión (AST, caché por
(sha,path)) para contar co-ocurrencias reales. Su fin es MEDIR el coste antes de integrar
(§10, §12), no ser código de producción:

- **Beneficio confirmado:** capta el acoplamiento que el blame pierde (test dedicado).
- **Coste medido:** ~14× (20 archivos × 30 commits) y ~23× (50 × 50) frente al `sync` con
  blame. Domina `git show` + re-AST por (commit, archivo): O(commits × archivos).

**Decisión (elección consciente).** En vez de pagar ese coste GLOBAL en cada `sync`, se
integró la ruta **ON-DEMAND y ACOTADA** (`deep_history.py`), fiel a §3.3/§3.4 (el trabajo
pesado fuera del hot path; pagar solo por lo que se consulta):

- **A · `impact <símbolo> --deep`:** historia completa restringida al historial del ARCHIVO
  del símbolo (`git log --follow -- <archivo>`) → coste proporcional a ese archivo, no al
  repo. CLI y MCP (`deep:true`; resuelve las raíces desde el meta `git_roots`). Marca
  `[NUEVO vs blame]` lo que el co-cambio del sync no vio.
- **B · disparador heurístico:** si un símbolo vive en un archivo de churn alto (historia
  completa) pero sin co-cambios registrados, `impact` sugiere `--deep`. Honesto: no "detecta"
  lo que el blame no computó (imposible en global), reconoce la FORMA de una pérdida probable.
- **C · narrativa:** el "por qué" del acoplamiento profundo con el LLM local si ya está
  activo (sin cold-start sorpresa), si no heurístico (Capa 3). Degradación elegante.

El **full-repo siempre-encendido** queda diferido; si alguna vez se integra, el camino es un
acumulador incremental por SHA + tope de profundidad — y el prototipo es la evidencia que lo
justificaría (o no) con datos.

### 18.6 Estado actual

- **Herramientas MCP (10):** `overview`, `search`, `neighbors`, `get`, `decisions`, `stats`,
  `working_set`, `impact` (+ `deep`), `history`, `digest_log`.
- **CLI adicional:** `runtime [--lsp]`, `analyze`, `report`, `compile [--llm]`,
  `digest [--llm]`, `graph [--level symbol]`, `doctor`, `setup-ollama`.
- **`memorygraf doctor`:** diagnostica e instala las dependencias opcionales (parsers,
  neural, watch, lsp, **pyright**) según el entorno (pipx/venv, plataforma/WSL/distro), con
  degradación; espejo "en vivo" del instalador.
- **Indexado multi-lenguaje (§18.7):** Python + JS/TS (completo) y C/C++/Java/C#/Go/Rust/PHP/
  R/VB/Assembly (símbolos + `defines`).
- **Suite:** 121 tests (`python -m unittest discover -s tests`), sin dependencias.
