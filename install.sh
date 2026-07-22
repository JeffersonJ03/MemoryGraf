#!/usr/bin/env bash
# Instalador de MemoryGraf (Linux/macOS/WSL). Deja el comando `memorygraf` disponible.
# Uso:
#   ./install.sh            # instala con potencia completa ([full]): tree-sitter, neural, watch
#   ./install.sh --core     # solo núcleo (sin dependencias opcionales)
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
EXTRAS="[full]"
if [ "${1:-}" = "--core" ]; then EXTRAS=""; fi

echo "==> Instalando MemoryGraf desde: $SRC  (extras: ${EXTRAS:-ninguno})"

if command -v pipx >/dev/null 2>&1; then
  echo "==> Usando pipx (entorno aislado, comando global)"
  pipx install --force "${SRC}${EXTRAS}"
  BIN="$(command -v memorygraf || echo "$HOME/.local/bin/memorygraf")"
else
  echo "==> pipx no encontrado; creando venv en $SRC/.venv"
  python3 -m venv "$SRC/.venv"
  "$SRC/.venv/bin/pip" install -q --upgrade pip
  "$SRC/.venv/bin/pip" install -q -e "${SRC}${EXTRAS}"
  BIN="$SRC/.venv/bin/memorygraf"
  echo "==> Sugerencia: añade a tu PATH:  export PATH=\"$SRC/.venv/bin:\$PATH\""
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
EOF
