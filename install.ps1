# Instalador de MemoryGraf (Windows PowerShell). Deja el comando `memorygraf` disponible.
# Uso:
#   .\install.ps1             # potencia completa ([full])
#   .\install.ps1 -Core       # solo núcleo
param([switch]$Core)

$ErrorActionPreference = "Stop"
$Src = $PSScriptRoot
$Extras = if ($Core) { "" } else { "[full]" }

Write-Host "==> Instalando MemoryGraf desde: $Src (extras: $($Extras -eq '' ? 'ninguno' : $Extras))"

if (Get-Command pipx -ErrorAction SilentlyContinue) {
    Write-Host "==> Usando pipx (entorno aislado, comando global)"
    pipx install --force "$Src$Extras"
} else {
    Write-Host "==> pipx no encontrado; creando venv en $Src\.venv"
    python -m venv "$Src\.venv"
    & "$Src\.venv\Scripts\pip.exe" install -q --upgrade pip
    & "$Src\.venv\Scripts\pip.exe" install -q -e "$Src$Extras"
    Write-Host "==> Comando en: $Src\.venv\Scripts\memorygraf.exe"
    Write-Host "==> Sugerencia: añade $Src\.venv\Scripts a tu PATH"
}

Write-Host ""
Write-Host "Siguiente, en cualquier proyecto:"
Write-Host "  cd C:\ruta\a\tu\proyecto"
Write-Host "  memorygraf init"
Write-Host "  memorygraf sync"
Write-Host "  memorygraf install claude   # o: memorygraf mcp-config"
