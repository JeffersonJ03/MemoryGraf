# Onboarding — MemoryGraf

**Qué es:** un grafo de conocimiento local de tu proyecto que le da contexto a tu
asistente de IA (vía MCP) recuperando **solo lo relevante** para cada tarea, en vez de
volcar archivos completos. Resultado medido: **~20× menos tokens** para ponerse en
contexto, y respuestas mejor dirigidas.

Corre 100% local. El código nunca sale de tu máquina (salvo que actives, a propósito,
un backend de API). Funciona con Claude y con cualquier cliente que hable MCP.

---

## 1. Requisitos

- **Python 3.10+**
- (Opcional) **pipx** para instalación aislada y global.
- (Opcional, para potencia) las dependencias de `requirements-full.txt` (tree-sitter,
  model2vec, watchdog) — el instalador las pone por ti.
- (Opcional, resúmenes en prosa privados) **Ollama** con un modelo de código.

## 2. Instalar (una vez por equipo)

```bash
git clone <url-del-repo> memorygraf && cd memorygraf
./install.sh                 # Linux/macOS/WSL   ·   .\install.ps1 en Windows
#   --core  para instalar solo el núcleo (sin dependencias opcionales)
```

Esto deja disponible el comando **`memorygraf`** (vía pipx global, o en `./.venv/bin`).

## 3. Activar en tu proyecto

```bash
cd /ruta/a/tu/proyecto
memorygraf init          # crea .memorygraf/ (config + BD del grafo)
memorygraf sync          # construye el grafo (incremental; repite cuando cambie el código)
```

Varios repos como un solo sistema:
`memorygraf init --name sistema --project . --project ../otro-repo`

## 4. Conectarlo a tu IA

```bash
memorygraf install claude    # Claude Code: registra el MCP en 1 comando
#   … o para CUALQUIER cliente MCP (Claude Desktop, etc.):
memorygraf mcp-config        # imprime el JSON de mcpServers para pegar
```

Reinicia la sesión del cliente para que cargue el servidor. Listo: al pedirle algo al
asistente, este consultará MemoryGraf (`overview`, `search`, `neighbors`, `decisions`)
antes de abrir archivos.

## 5. Uso diario

```bash
memorygraf watch     # déjalo corriendo: mantiene el grafo al día automáticamente
                     # (el servidor MCP se recarga en caliente al reindexar)
```

Ver lo que ve la IA:
```bash
memorygraf graph     # genera graph.html interactivo (ábrelo en el navegador)
```

Explorar por consola (mismo motor que el MCP):
```bash
memorygraf overview
memorygraf search "autenticacion de usuarios"
memorygraf neighbors "<node_id>"
memorygraf decisions "base de datos"
```

## 6. Potencia opcional (todo con degradación elegante)

- **Parsers exactos JS/TS y semántica cross-idioma**: ya vienen con `install.sh` (sin
  `--core`). Si faltan, cae a regex + TF-IDF sin romperse.
- **Entidades de dominio**: copia `memorygraf.entities.example.json` a tu proyecto como
  `memorygraf.entities.json` (o `.memorygraf/entities.json`), edítalo con tus entidades
  de negocio y `memorygraf sync`.
- **Resúmenes en prosa (privados, con Ollama)**:
  ```bash
  ollama pull qwen2.5-coder:3b
  MEMORYGRAF_SUMMARY_BACKEND=ollama memorygraf summarize --all
  ```

## 7. Notas y resolución de problemas

- **WSL / proyectos en `/mnt/c`**: `watch` usa *polling* a propósito (inotify no ve
  cambios hechos desde Windows). Es automático; no requiere nada.
- **Cambié código y la IA no lo ve**: corre `memorygraf sync` (o ten `watch` activo) y
  reinicia la sesión del cliente si hiciera falta.
- **El grafo (`*.db`, `graph.html`, `.memorygraf/`) NO se versiona** — es regenerable y
  está en `.gitignore`. Sí se versiona el código de la herramienta y los `.example`.
- **Privacidad**: por defecto todo es local. Los backends de API (embeddings/resúmenes)
  son opt-in explícito y envían texto fuera; úsalos solo si tu política lo permite.

## Cheatsheet

```
instalar     ./install.sh
por proyecto memorygraf init && memorygraf sync
conectar     memorygraf install claude   |   memorygraf mcp-config
al día       memorygraf watch
ver grafo    memorygraf graph
consultar    memorygraf overview | search | neighbors | decisions
```
