function Write-Log {
    param([string]$Message)
    Write-Host "[log] $Message"
}

class Retry {
    static [void] Run() { }
}
