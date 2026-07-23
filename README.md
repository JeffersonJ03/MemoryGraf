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

## Uso en cualquier proyecto

```bash
cd /ruta/a/tu/proyecto
memorygraf init            # crea .memorygraf/ (config + grafo)
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
| Símbolos/`calls`/`implements` JS/TS | regex (aprox.) | **tree-sitter** (exacto) | `parsers` |
| Búsqueda semántica | TF-IDF | **model2vec** neural (cross-idioma) | `neural` |
| `watch` | polling | **watchdog** (eventos nativos) | `watch` |
| Diagnósticos + tipos por símbolo (`runtime --lsp`) | — (se omite) | **python-lsp-server** (o pyright) | `lsp` |
| Python (`ast`), grafo, MCP, decisiones, entidades | exacto siempre | igual | — |

Instala todo con `pip install ".[full]"`, o solo lo que quieras: `pip install ".[neural]"`,
`".[parsers]"`, `".[watch]"`, `".[lsp]"`. Si un extra falta, esa capacidad **degrada** al
modo portable en vez de fallar. Corre **`memorygraf doctor`** para ver qué está activo y
**activar lo que falte de forma interactiva**: instala en el entorno correcto (pipx o venv,
detecta plataforma/WSL/distro) y te dice el paso siguiente. También no interactivo:
`memorygraf doctor --install neural,lsp` (o `--install all`), y `--json` para solo reporte.

Resúmenes de nodos: heurístico por defecto (offline); prosa real opcional vía **Ollama**
local (privado, `memorygraf setup-ollama`) o una API compatible OpenAI.

## Cómo funciona

1. **Indexa** el código a un grafo: nodos `file`/`symbol`/`entity`/`decision`/…, aristas
   `defines`/`imports`/`calls` (intra y cross-archivo)/`implements`/`references`/`models`.
   Python vía `ast` (exacto); JS/TS/TSX vía tree-sitter. Incremental por hash, con
   reconciliación de símbolos que cambian de archivo.
2. **Enriquece**: decisiones y convenciones desde la documentación markdown; entidades de
   dominio desde un glosario que aporta el proyecto; resúmenes; embeddings.
3. **Expone** un servidor MCP (y CLI) con 6 herramientas que devuelven texto compacto
   **con procedencia** (`archivo:línea`) y presupuesto de tokens.

La fuente de verdad es un SQLite legible + export JSON; el índice vectorial es caché
regenerable. Todo es portable y agnóstico del LLM. Ver [`DESIGN.md`](DESIGN.md) para la
arquitectura completa y [`ONBOARDING.md`](ONBOARDING.md) para la guía de arranque.

## Herramientas MCP

`overview` · `search` · `neighbors` · `get` · `decisions` · `stats`

## Desarrollo

```bash
python -m unittest discover -s tests     # suite de pruebas (sin dependencias)
```

## Privacidad

Todo corre en local por defecto; el código nunca sale de tu máquina. Los backends de API
(embeddings/resúmenes) son **opt-in** explícito y envían texto a un servicio externo —
úsalos solo si tu política lo permite.
