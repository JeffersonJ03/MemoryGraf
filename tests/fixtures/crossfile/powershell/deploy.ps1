. "$PSScriptRoot\lib\common.ps1"
Import-Module "$PSScriptRoot/Mods/Net.psm1"
Import-Module Az.Accounts

function Invoke-Deploy {
    param([string]$Target)
    write-log "deploy $Target"
    Test-Port -Port 443
    [Retry]::Run()
}
