$IcarusDir = Split-Path -Parent $MyInvocation.MyCommand.Path

$FunctionBlock = @"

function icarus {
    param([string]`$cmd = "help")
    `$dir = "$IcarusDir"
    switch (`$cmd) {
        "wake" {
            Write-Host "[Icarus] Starting Docker services..."
            docker compose -f "`$dir\docker-compose.yml" up -d
            Write-Host "[Icarus] Starting Councilor..."
            try {
                python "`$dir\councilor.py"
            } finally {
                Write-Host ""
                Write-Host "[Icarus] Shutting down Docker services..."
                docker compose -f "`$dir\docker-compose.yml" down
            }
        }
        "stop" {
            docker compose -f "`$dir\docker-compose.yml" down
        }
        "logs" {
            docker compose -f "`$dir\docker-compose.yml" logs -f icarus-api
        }
        default {
            Write-Host "Usage: icarus <command>"
            Write-Host "  wake   Start Docker services + Councilor"
            Write-Host "  stop   Stop Docker services"
            Write-Host "  logs   Tail icarus-api logs"
        }
    }
}
"@

if (!(Test-Path $PROFILE)) {
    New-Item -ItemType File -Path $PROFILE -Force | Out-Null
}

Add-Content -Path $PROFILE -Value $FunctionBlock
Write-Host "Done. Run: . `$PROFILE"
