# MemoryGraf

**Un grafo de conocimiento local y portable que le da a un asistente de IA el contexto
de tu proyecto — recuperando solo lo relevante para cada tarea, en vez de volcar
archivos enteros.** Se conecta vía **MCP** (Model Context Protocol), así que funciona con
Claude y con cualquier cliente que hable MCP. También tiene CLI, por lo que sirve sin
ninguna IA.

> La idea: no gastar tokens re-leyendo el proyecto en cada sesión. MemoryGraf indexa el
> código en un grafo (símbolos, llamadas, imports, decisiones, entidades de dominio) y
> el asistente lo **consulta** (`overview`, `search`, `neighbors`, `decisions`) trayendo
> a su contexto únicamente el subgrafo que necesita. En una tarea real medida, la
> orientación pasó de ~40.000 tokens (leer archivos a ciegas) a ~1.900 (**~21× menos**),
> llegando además al punto exacto del cambio.

Autor: **Jefferson J. Patiño Ortega** · Licencia: **MIT**

---

## Instalación (una vez)

```bash
git clone <este-repo> memorygraf && cd memorygraf
./install.sh            # Linux/macOS/WSL   ·   .\install.ps1 en Windows
#   --core  para instalar solo el núcleo (sin dependencias opcionales)
```

Deja disponible el comando **`memorygraf`** (vía `pipx` si está, o un venv local).

> **Estado de plataformas (honesto).** Validado end-to-end en **Linux / WSL**. El núcleo es
> cross-platform por diseño (solo stdlib) y hay soporte específico para Windows/macOS
> (instaladores, rutas, `setup-ollama` con winget/brew), pero **Windows nativo y macOS aún no
> están probados end-to-end**. Ver `E2E-INTEGRATION-TEST.md` y `MEJORAS-FUTURAS.md` §9.

## Uso en cualquier proyecto

```bash
cd /ruta/a/tu/proyecto
memorygraf init            # crea .memorygraf/ (config + grafo)
memorygraf configure       # (opcional) asistente: activa capacidades por potencia y valida deps
memorygraf sync            # construye el grafo (incremental)
memorygraf install claude  # registra el MCP en Claude Code (1 comando)
#   … o para cualquier otro cliente MCP:
memorygraf mcp-config      # imprime el JSON de mcpServers para pegar
memorygraf watch           # (opcional) mantiene el grafo al día automáticamente
```

Varios repos como un solo sistema:
`memorygraf init --name sistema --project . --project ../otro-repo`.

## Ver lo que "ve la IA"

```bash
memorygraf graph           # genera graph.html interactivo (autocontenido, sin CDN)
```

Un diagrama del grafo: nodos por proyecto/tipo, aristas por relación
(calls/imports/references/models), zoom, arrastre, tooltips y búsqueda.

## Dos modos: portable y potencia

El **núcleo corre solo con la stdlib** (cero dependencias, offline, cualquier plataforma).
Instalando dependencias **opcionales** se activa el **modo potencia**; si falta alguna,
hay **degradación elegante** al modo portable.

| Capacidad | Portable (sin extra) | Potencia (con extra) | Extra |
|---|---|---|---|
| Símbolos JS/TS (`calls`/`implements`) | regex (aprox.) | **tree-sitter** (exacto) | `parsers` |
| Símbolos C/C++/Java/C#/Go/Rust/PHP/R/VB/asm | — (se omite) | **tree-sitter** (símbolos + `defines` + `calls` intra-archivo) | `parsers` |
| Búsqueda semántica | TF-IDF | **model2vec** neural (cross-idioma) | `neural` |
| `watch` | polling | **watchdog** (eventos nativos) | `watch` |
| Diagnósticos + tipos por símbolo (`runtime --lsp`) | — (se omite) | **python-lsp-server** (o **pyright**, mejor calidad — `memorygraf doctor` lo instala); **TS/JS** con `typescript-language-server` | `lsp` |
| Python (`ast`), grafo, MCP, decisiones, entidades | exacto siempre | igual | — |

Instala todo con `pip install ".[full]"`, o solo lo que quieras: `pip install ".[neural]"`,
`".[parsers]"`, `".[watch]"`, `".[lsp]"`. Si un extra falta, esa capacidad **degrada** al
modo portable en vez de fallar. Corre **`memorygraf doctor`** para ver qué está activo y
**activar lo que falte de forma interactiva**: instala en el entorno correcto (pipx o venv,
detecta plataforma/WSL/distro) y te dice el paso siguiente. También no interactivo:
`memorygraf doctor --install neural,lsp` (o `--install all`), y `--json` para solo reporte.

**Resúmenes de nodos** (controlados por la variable `MEMORYGRAF_SUMMARY_BACKEND`):

| Backend | Qué hace | Cuándo |
|---|---|---|
| `heuristic` *(por defecto)* | Rápido, offline, sin IA | El `sync` diario; nunca sorprende |
| `auto` | Usa Ollama **si está instalado**; si no, heurístico | opt-in; ⚠️ con Ollama en CPU y miles de nodos un `sync` puede tardar **horas** |
| `ollama` | Prosa real con LLM **local y privado** | opt-in (`memorygraf setup-ollama`) |
| `api` | Prosa vía API compatible OpenAI | opt-in (clave en `MEMORYGRAF_LLM_KEY`) |

> El default es `heuristic`: Ollama es **opt-in** (por env, config o `backend=auto`), nunca
> implícito solo por estar instalado — así un `sync` no se va a horas de CPU sin que lo pidas.

Patrón recomendado — `sync` rápido y, si quieres prosa, en un paso aparte (con progreso `i/total`):
```bash
MEMORYGRAF_SUMMARY_BACKEND=heuristic memorygraf sync          # rápido, offline
MEMORYGRAF_SUMMARY_BACKEND=ollama    memorygraf summarize --all   # prosa (lento en CPU)
```

Con Ollama, dos opt-in con **LLM local** (privado, fallback determinista si no está):
narrativa más rica del co-cambio on-demand (`memorygraf compile --llm`, o `compiler.backend=ollama`
en la config para cada `sync`), y rerank de búsqueda con presupuesto de latencia acotado y
caché (`memorygraf search "<consulta>" --rerank-llm`; `--rerank` es la versión determinista).

## Cómo funciona

MemoryGraf construye un grafo **por capas**; cada una degrada con elegancia si le falta una
dependencia, y todas dejan **procedencia** (`archivo:línea`) y respetan un presupuesto de tokens.

1. **Grafo base.** Nodos `file`/`symbol`/`entity`/`decision`/`convention` y aristas
   `defines`/`imports`/`calls` (intra y cross-archivo)/`implements`/`references`/`models`.
   **Multi-lenguaje** vía tree-sitter: **Python** (`ast`), **JS/TS/TSX** y ahora también
   **C, C++, Java, C#, Go, Rust, PHP, R, Visual Basic y Assembly** con símbolos + `defines` +
   `calls` intra-archivo **y `calls`/`imports` cross-file** (M9, determinista y precisión-primero:
   paquete/ruta/`#include`/`source`/`mod`/`go.mod`/PSR-4/namespaces según el lenguaje; capa LLM
   opcional para desempatar ambiguos). Incremental por hash, con reconciliación de símbolos
   movidos y enlace cross-project por endpoints HTTP.
2. **Conocimiento de dominio.** Decisiones y convenciones desde markdown (`governs`);
   entidades desde un glosario del proyecto (`models`); resúmenes (heurístico/Ollama/API) y
   embeddings (TF-IDF/model2vec/API) para búsqueda híbrida (RRF).
3. **Capa temporal (Git).** Co-cambio (acoplamiento oculto que el call-graph no ve), churn,
   fragilidad, autores y el "por qué" de cada cambio → `history`, `working-set`, `impact`.
4. **Verdad de runtime.** Cobertura por símbolo, `tested_by` código↔test (hasta **símbolo→test**
   por contextos de cobertura), estado del último test, y diagnósticos + tipos vía **LSP**
   (`runtime --lsp`, multi-lenguaje Python + TS/JS).
5. **Compilador de contexto local.** Un LLM pequeño y local (Ollama, opcional) que **destila**
   logs gigantes (`digest`), **narra** el co-cambio y **reordena** búsquedas — siempre con
   fallback determinista.

La fuente de verdad es un SQLite legible + export JSON; los índices (vectorial, Git, runtime)
son caché regenerable. Todo es portable y agnóstico del LLM. Ver [`DESIGN.md`](DESIGN.md)
(§18 para las capas contextuales) y [`ONBOARDING.md`](ONBOARDING.md) para el arranque.

## Herramientas MCP

`overview` · `search` · `neighbors` · `get` · `decisions` · `stats` · `working_set` ·
`impact` · `history` · `digest_log`

`impact` predice el "blast radius" (llamadas ∪ co-cambio). Con `--deep` (CLI) o `deep:true`
(MCP) añade el co-cambio por **historia completa** acotado al archivo, que capta acoplamiento
que el blame pierde; si un símbolo está muy tocado pero sin co-cambios, `impact` lo sugiere.

## CLI (mismas capacidades, también sin IA)

- **Consulta:** `overview` · `search [--rerank | --rerank-llm]` · `neighbors` · `get` ·
  `decisions` · `stats` · `working-set` · `impact [--deep]` · `history` · `graph [--level symbol]`.
- **Mantenimiento:** `init` · `sync` · `index` · `summarize` · `embed` · `runtime [--lsp]` ·
  `compile [--llm]` · `digest [--llm]` · `analyze` · `report` · `watch` · `export`.
- **Setup:** `install claude` / `mcp-config` · **`configure`** (asistente de capacidades
  opcionales) · **`bootstrap-entities`** (propone entidades de dominio desde el código para
  curar) · `doctor` (dependencias opcionales) · `setup-ollama` (instala Ollama) ·
  **`setup-llm`** (elige motor+modelo de LLM, interactivo).

**`memorygraf configure`** es un asistente interactivo que activa las capacidades opcionales
del grafo (LLM local para resúmenes/compilador/desempate M9, co-cambio por **historia completa**
M1, **tipos LSP en el `sync`** y variables locales M4b) mediante **paquetes por potencia**
(portable / estándar / potencia) o **modo avanzado** opción por opción, **validando las
dependencias** y orientando a `doctor`/`setup-ollama` si falta alguna. Escribe la config sola.

`memorygraf setup-llm` configura el LLM de resúmenes y compilador de forma interactiva y
escribe la config sola. Motores: **heuristic** (offline), **ollama** (local — elige/descarga
un modelo, o importa un `.gguf` propio) o **api** (endpoint compatible con OpenAI: LM Studio,
vLLM, llama.cpp server o nube; la API key va en `MEMORYGRAF_LLM_KEY`, nunca en el archivo).

## Desarrollo

```bash
python -m unittest discover -s tests     # suite de pruebas (sin dependencias)
```

## Privacidad

Todo corre en local por defecto; el código nunca sale de tu máquina. Los backends de API
(embeddings/resúmenes) son **opt-in** explícito y envían texto a un servicio externo —
úsalos solo si tu política lo permite.
