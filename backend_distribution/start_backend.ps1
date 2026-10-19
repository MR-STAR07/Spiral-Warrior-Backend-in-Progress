param(
    [string]$LanIp = ""
)

$ErrorActionPreference = "Stop"

if (-not $LanIp) {
    $configPath = Join-Path $PSScriptRoot "backend.env"
    if (Test-Path -LiteralPath $configPath) {
        $line = Get-Content -LiteralPath $configPath | Where-Object { $_ -match '^\s*BACKEND_LAN_IP\s*=' } | Select-Object -First 1
        if ($line -match '^\s*BACKEND_LAN_IP\s*=\s*(.*?)\s*$') {
            $LanIp = $Matches[1].Trim().Trim('"').Trim("'")
        }
    }
}

if ($LanIp -eq "<LAN_IP_OF_HOST_MACHINE>") {
    $LanIp = ""
}

$python = Get-Command python -ErrorAction Stop
$launcher = Join-Path $PSScriptRoot "run_local_server.py"
if ($LanIp) {
    & $python.Source $launcher --lan --lan-ip $LanIp
} else {
    & $python.Source $launcher --lan
}
exit $LASTEXITCODE
