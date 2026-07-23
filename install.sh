#!/usr/bin/env bash
# Instalador de MemoryGraf (Linux/macOS/WSL). Deja el comando `memorygraf` disponible.
# Uso:
#   ./install.sh            # potencia completa ([full]): tree-sitter, model2vec, watchdog, python-lsp-server
#   ./install.sh --core     # solo núcleo (stdlib, sin dependencias opcionales)
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
EXTRAS="[full]"
if [ "${1:-}" = "--core" ]; then EXTRAS=""; fi

echo "==> Instalando MemoryGraf desde: $SRC  (extras: ${EXTRAS:-ninguno})"

# Informa qué activan las dependencias OPCIONALES (degradación elegante si faltan).
if [ -n "$EXTRAS" ]; then
  echo "==> Dependencias opcionales (modo potencia) que se instalarán con [full]:"
else
  echo "==> Modo --core: solo stdlib. Estas capacidades quedan en modo portable:"
fi
cat <<'CAPS'
      tree-sitter (+ language-pack) : símbolos/calls JS/TS exactos (si no: regex aprox.)
      model2vec                     : búsqueda semántica neural cross-idioma (si no: TF-IDF)
      watchdog                      : `watch` por eventos nativos      (si no: polling)
      python-lsp-server             : `runtime --lsp` diagnósticos + tipos (si no: se omite)
    (instala solo lo que quieras:  pip install ".[neural]"  ".[parsers]"  ".[watch]"  ".[lsp]")
    (revisa el estado y ACTIVA lo que falte con:  memorygraf doctor  — interactivo)
CAPS

# Detecta el rc file del shell activo (para persistir el PATH si hace falta)
detect_rc_file() {
  case "$(basename "${SHELL:-bash}")" in
    zsh)  echo "$HOME/.zshrc" ;;
    bash) echo "$HOME/.bashrc" ;;
    *)    echo "" ;;
  esac
}

# Añade una línea a un rc file solo si no está ya presente (evita duplicados
# si corres el instalador varias veces)
persist_path_line() {
  local dir_to_add="$1"
  local rc
  rc="$(detect_rc_file)"
  local line="export PATH=\"$dir_to_add:\$PATH\""

  if [ -z "$rc" ]; then
    echo "==> No se detectó shell soportado (bash/zsh); añade esto a tu perfil manualmente:"
    echo "    $line"
    return
  fi

  if [ -f "$rc" ] && grep -qF "$dir_to_add" "$rc"; then
    echo "==> $dir_to_add ya está en $rc (no se duplica)"
  else
    {
      echo ""
      echo "# Añadido por instalador de MemoryGraf"
      echo "$line"
    } >> "$rc"
    echo "==> Añadido permanentemente al PATH en $rc"
  fi
  echo "==> Para usarlo en esta misma terminal, corre:  source $rc"
}

if command -v pipx >/dev/null 2>&1; then
  echo "==> Usando pipx (entorno aislado, comando global)"
  pipx install --force "${SRC}${EXTRAS}"
  BIN="$(command -v memorygraf || echo "$HOME/.local/bin/memorygraf")"

  # pipx sabe hacer esto de forma nativa y correcta (detecta shell, evita duplicados, etc.)
  if command -v pipx >/dev/null 2>&1; then
    pipx ensurepath || true
  fi
else
  echo "==> pipx no encontrado; creando venv en $SRC/.venv"
  python3 -m venv "$SRC/.venv"
  "$SRC/.venv/bin/pip" install -q --upgrade pip
  "$SRC/.venv/bin/pip" install -q -e "${SRC}${EXTRAS}"
  BIN="$SRC/.venv/bin/memorygraf"

  persist_path_line "$SRC/.venv/bin"
fi

echo ""
echo "==> Instalado. Comando: $BIN"
if "$BIN" --help >/dev/null 2>&1; then echo "==> Verificación OK."; fi
cat <<EOF

Siguiente, en cualquier proyecto:
  cd /ruta/a/tu/proyecto
  memorygraf init
  memorygraf sync
  memorygraf install claude      # o:  memorygraf mcp-config  (para cualquier cliente MCP)

Opcional — resúmenes en prosa con IA 100% local (Ollama):
  memorygraf setup-ollama        # detecta tu plataforma, instala Ollama y el modelo
                                 # (en WSL/Linux se instala sin sudo, en ~/.local)

NOTA: si "memorygraf" no se reconoce en esta terminal, abre una nueva
o corre "source ~/.bashrc" (o ~/.zshrc) para cargar el PATH actualizado.
EOF
