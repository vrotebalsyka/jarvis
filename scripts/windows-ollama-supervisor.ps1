[CmdletBinding()]
param(
    [ValidateRange(5, 300)]
    [int]$PollSeconds = 15
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ollamaRoot = Split-Path -Parent $PSCommandPath
$ollamaExe = Join-Path $ollamaRoot 'ollama.exe'
$modelRoot = 'H:\OllamaModels'
$listenPort = 11434
$wslExe = Join-Path $env:SystemRoot 'System32\wsl.exe'
$contextOutput = & $wslExe `
    -d Ubuntu `
    -u homebutler `
    --exec /usr/bin/python3 `
    /opt/home-butler/scripts/model_runtime_policy.py `
    --context-window dialogue
$contextWindow = 0
if ($LASTEXITCODE -ne 0 -or
    -not [int]::TryParse(($contextOutput | Select-Object -Last 1), [ref]$contextWindow) -or
    $contextWindow -lt 8192 -or $contextWindow -gt 65536) {
    throw 'The canonical model context policy is unavailable.'
}

function Test-PrivateWslAddress {
    param([Parameter(Mandatory)][string]$Address)

    $parsed = $null
    if (-not [System.Net.IPAddress]::TryParse($Address, [ref]$parsed)) {
        return $false
    }
    $bytes = $parsed.GetAddressBytes()
    return $bytes.Length -eq 4 -and $bytes[0] -eq 172 -and $bytes[1] -ge 16 -and $bytes[1] -le 31
}

function Get-CurrentWslAddress {
    $candidates = @(
        Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object {
                $_.InterfaceAlias -like 'vEthernet (WSL*' -and
                $_.AddressState -eq 'Preferred' -and
                (Test-PrivateWslAddress -Address $_.IPAddress)
            } |
            Select-Object -ExpandProperty IPAddress -Unique
    )
    if ($candidates.Count -ne 1) {
        return $null
    }
    return $candidates[0]
}

function Get-ManagedOllamaServers {
    return @(
        Get-CimInstance Win32_Process -Filter "Name = 'ollama.exe'" -ErrorAction SilentlyContinue |
            Where-Object {
                $_.ExecutablePath -eq $ollamaExe -and
                $_.CommandLine -match '(?i)(^|\s)serve(\s|$)'
            }
    )
}

function Test-ManagedOllamaServerIdentity {
    param([Parameter(Mandatory)][uint32]$ProcessId)

    $processes = @(
        Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue |
            Where-Object {
                $_.ExecutablePath -eq $ollamaExe -and
                $_.CommandLine -match '(?i)(^|\s)serve(\s|$)'
            }
    )
    return $processes.Count -eq 1
}

function Test-ExactListener {
    param(
        [Parameter(Mandatory)][uint32]$ProcessId,
        [Parameter(Mandatory)][string]$Address
    )

    $listeners = @(
        Get-NetTCPConnection -State Listen -LocalPort $listenPort -ErrorAction SilentlyContinue |
            Where-Object {
                $_.OwningProcess -eq $ProcessId
            }
    )
    return $listeners.Count -eq 1 -and $listeners[0].LocalAddress -eq $Address
}

function Stop-ManagedOllamaServers {
    param([Parameter(Mandatory)][object[]]$Servers)

    foreach ($server in $Servers) {
        if (Test-ManagedOllamaServerIdentity -ProcessId $server.ProcessId) {
            Stop-Process -Id $server.ProcessId -ErrorAction SilentlyContinue
        }
    }
    Start-Sleep -Seconds 2
    foreach ($server in $Servers) {
        if (Test-ManagedOllamaServerIdentity -ProcessId $server.ProcessId) {
            Stop-Process -Id $server.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }
}

if (-not (Test-Path -LiteralPath $ollamaExe -PathType Leaf)) {
    throw 'The pinned Ollama executable is missing.'
}
if (-not (Test-Path -LiteralPath $modelRoot -PathType Container)) {
    throw 'The pinned Ollama model directory is missing.'
}
$signature = Get-AuthenticodeSignature -FilePath $ollamaExe
if ($signature.Status -ne 'Valid') {
    throw 'The pinned Ollama executable has an invalid signature.'
}

while ($true) {
    $address = Get-CurrentWslAddress
    $servers = @(Get-ManagedOllamaServers)

    if ($null -eq $address) {
        if ($servers.Count -gt 0) {
            Stop-ManagedOllamaServers -Servers $servers
        }
        Start-Sleep -Seconds $PollSeconds
        continue
    }

    if ($servers.Count -eq 1 -and (Test-ExactListener -ProcessId $servers[0].ProcessId -Address $address)) {
        Start-Sleep -Seconds $PollSeconds
        continue
    }
    if ($servers.Count -gt 0) {
        Stop-ManagedOllamaServers -Servers $servers
    }

    $env:OLLAMA_HOST = "$address`:$listenPort"
    $env:OLLAMA_MODELS = $modelRoot
    $env:OLLAMA_NO_CLOUD = '1'
    $env:OLLAMA_CONTEXT_LENGTH = [string]$contextWindow
    $env:OLLAMA_FLASH_ATTENTION = '1'
    $env:OLLAMA_KV_CACHE_TYPE = 'q8_0'
    $env:OLLAMA_NUM_PARALLEL = '1'
    $env:OLLAMA_MAX_LOADED_MODELS = '1'
    $env:OLLAMA_KEEP_ALIVE = '5m'

    $started = Start-Process -FilePath $ollamaExe -ArgumentList 'serve' -WindowStyle Hidden -PassThru
    Start-Sleep -Seconds 3
    if ($started.HasExited -or -not (Test-ExactListener -ProcessId $started.Id -Address $address)) {
        if (-not $started.HasExited) {
            Stop-ManagedOllamaServers -Servers @(
                [pscustomobject]@{ ProcessId = [uint32]$started.Id }
            )
        }
        Start-Sleep -Seconds $PollSeconds
    }
}
