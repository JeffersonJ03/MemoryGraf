# Instalador de MemoryGraf (Windows PowerShell). Deja el comando `memorygraf` disponible.
# Uso:
#   .\install.ps1             # potencia completa ([full]): tree-sitter, model2vec, watchdog, python-lsp-server
#   .\install.ps1 -Core       # solo núcleo (stdlib, sin dependencias opcionales)
param([switch]$Core)

$ErrorActionPreference = "Stop"
$Src = $PSScriptRoot
$Extras = if ($Core) { "" } else { "[full]" }

# Compatibilidad: $IsWindows no existe en Windows PowerShell 5.1 (solo en pwsh/Core)
if (-not (Test-Path variable:IsWindows)) { $IsWindows = $true }

$ExtrasLabel = if ($Extras -eq "") { "ninguno" } else { $Extras }
Write-Host "==> Instalando MemoryGraf desde: $Src (extras: $ExtrasLabel)"

# Informa qué activan las dependencias OPCIONALES (degradación elegante si faltan).
if ($Extras -eq "") {
    Write-Host "==> Modo -Core: solo stdlib. Estas capacidades quedan en modo portable:"
} else {
    Write-Host "==> Dependencias opcionales (modo potencia) que se instalaran con [full]:"
}
Write-Host "      tree-sitter (+ language-pack) : simbolos/calls JS/TS exactos (si no: regex aprox.)"
Write-Host "      model2vec                     : busqueda semantica neural cross-idioma (si no: TF-IDF)"
Write-Host "      watchdog                      : 'watch' por eventos nativos      (si no: polling)"
Write-Host "      python-lsp-server             : 'runtime --lsp' diagnosticos + tipos (si no: se omite)"
Write-Host '    (instala solo lo que quieras:  pip install ".[neural]"  ".[parsers]"  ".[watch]"  ".[lsp]")'

# Añade una carpeta al PATH del usuario de forma PERMANENTE (persiste en el
# registro, sobrevive a reinicios de terminal) sin duplicarla si ya está.
function Add-ToUserPath {
    param([string]$DirToAdd)

    $currentUserPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($null -eq $currentUserPath) { $currentUserPath = "" }

    $alreadyThere = $currentUserPath -split ';' | Where-Object { $_.TrimEnd('\') -eq $DirToAdd.TrimEnd('\') }

    if ($alreadyThere) {
        Write-Host "==> $DirToAdd ya está en el PATH de usuario (no se duplica)"
    } else {
        $newPath = if ($currentUserPath.Trim() -eq "") { $DirToAdd } else { "$currentUserPath;$DirToAdd" }
        [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
        Write-Host "==> Añadido permanentemente al PATH de usuario: $DirToAdd"
    }

    # También lo añadimos a la sesión actual para que funcione sin reabrir la terminal
    if (-not (($env:Path -split ';') -contains $DirToAdd)) {
        $env:Path = "$env:Path;$DirToAdd"
    }
}

if (Get-Command pipx -ErrorAction SilentlyContinue) {
    Write-Host "==> Usando pipx (entorno aislado, comando global)"
    pipx install --force "$Src$Extras"
    # pipx sabe hacer esto de forma nativa y correcta (detecta ubicación, evita duplicados)
    pipx ensurepath | Out-Null
} else {
    $venvPath = Join-Path $Src ".venv"
    Write-Host "==> pipx no encontrado; creando venv en $venvPath"
    python -m venv $venvPath

    if ($IsWindows) {
        $pip = Join-Path $venvPath "Scripts\pip.exe"
        $scriptsDir = Join-Path $venvPath "Scripts"
        $binName = "memorygraf.exe"
    } else {
        $pip = Join-Path $venvPath "bin/pip"
        $scriptsDir = Join-Path $venvPath "bin"
        $binName = "memorygraf"
    }

    & $pip install --upgrade pip
    & $pip install -e "$Src$Extras"

    $BinPath = Join-Path $scriptsDir $binName
    Write-Host "==> Comando en: $BinPath"

    Add-ToUserPath -DirToAdd $scriptsDir
}

Write-Host ""
Write-Host "Siguiente, en cualquier proyecto:"
Write-Host "  cd C:\ruta\a\tu\proyecto"
Write-Host "  memorygraf init"
Write-Host "  memorygraf sync"
Write-Host "  memorygraf install claude   # o: memorygraf mcp-config"
Write-Host ""
Write-Host "Opcional - resumenes en prosa con IA 100% local (Ollama):"
Write-Host "  memorygraf setup-ollama     # instala Ollama (winget) y el modelo, y configura MemoryGraf"
Write-Host ""
Write-Host "NOTA: si 'memorygraf' no se reconoce en una terminal NUEVA que abras,"
Write-Host "cierra y reabre VS Code / la terminal para que recargue el PATH del sistema."
