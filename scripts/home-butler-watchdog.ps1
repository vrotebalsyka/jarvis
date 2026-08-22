[CmdletBinding()]
param(
    [ValidateRange(15, 300)]
    [int]$PollSeconds = 30,
    [switch]$Once
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$runtimeTaskName = 'Home Butler WSL Runtime'
$gpuTaskName = 'Home Butler Ollama GPU'
$distroName = 'Ubuntu'
$wslExe = Join-Path $env:SystemRoot 'System32\wsl.exe'
$stateRoot = 'H:\WSL\Ubuntu\windows-runtime'
$statusPath = Join-Path $stateRoot 'watchdog-status.json'
$proofHistoryPath = Join-Path $stateRoot 'startup-proof-history.json'
$requiredUnits = @(
    'home-butler.service',
    'home-butler-heartbeat.timer',
    'home-butler-startup-ha-check.timer',
    'home-butler-startup-self-check.timer',
    'home-butler-dialogue-qualification.timer',
    'home-butler-incident-monitor.service',
    'home-butler-incident-notifier.timer',
    'home-butler-inventory.timer',
    'home-butler-daily-report.timer',
    'home-butler-automation-diagnostics.timer',
    'home-butler-system-log-diagnostics.timer',
    'home-butler-device-health.timer',
    'home-butler-entity-freshness.timer',
    'home-butler-alice-skill.service',
    'home-butler-local-chat.service',
    'home-butler-alice-tunnel.service',
    'home-butler-alice-health.timer',
    'home-butler-alice-finalize.path',
    'home-butler-alice-rotation-finalize.path'
)

foreach ($unit in $requiredUnits) {
    if ($unit -notmatch '^home-butler(?:-[a-z0-9@_.-]+)?\.(?:service|timer|path)$') {
        throw "Unsafe Home Butler unit in watchdog allowlist: $unit"
    }
}
if (-not (Test-Path -LiteralPath $wslExe -PathType Leaf)) {
    throw 'wsl.exe is unavailable.'
}

function Start-PinnedTaskIfNeeded {
    param([Parameter(Mandatory)][string]$TaskName)

    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        return $false
    }
    if ($task.State -ne 'Running') {
        Start-ScheduledTask -TaskName $TaskName
    }
    return $true
}

function Invoke-WslFixed {
    param([Parameter(Mandatory)][string[]]$Arguments)

    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $wslExe @Arguments 1> $null 2> $null
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
    return $exitCode
}

function Test-GpuEndpoint {
    $addresses = @(
        Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object {
                $_.InterfaceAlias -like 'vEthernet (WSL*' -and
                $_.AddressState -eq 'Preferred' -and
                $_.IPAddress -match '^172\.(1[6-9]|2[0-9]|3[01])\.'
            } |
            Select-Object -ExpandProperty IPAddress -Unique
    )
    if ($addresses.Count -ne 1) {
        return $false
    }
    try {
        $request = [System.Net.HttpWebRequest]::Create(
            "http://$($addresses[0]):11434/api/version"
        )
        $request.Method = 'GET'
        $request.Timeout = 3000
        $request.ReadWriteTimeout = 3000
        $response = $request.GetResponse()
        try {
            return [int]$response.StatusCode -eq 200
        }
        finally {
            $response.Close()
        }
    }
    catch {
        return $false
    }
}

function Get-WslModelMode {
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $lines = @(& $wslExe @(
            '-d', $distroName, '-u', 'homebutler', '--exec', '/usr/bin/python3',
            '/opt/home-butler/scripts/ollama_endpoint.py'
        ) 2> $null)
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($exitCode -ne 0 -or $lines.Count -ne 1) {
        return 'unavailable'
    }
    $endpoint = ([string]$lines[0]).Trim()
    if ($endpoint -eq 'http://127.0.0.1:11434') {
        return 'cpu_fallback'
    }
    if ($endpoint -match '^http://172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}:11434$') {
        return 'gpu'
    }
    return 'unavailable'
}

function Test-StartupSelfCheck {
    return (
        (Invoke-WslFixed -Arguments @(
            '-d', $distroName, '-u', 'homebutler', '--exec', '/usr/bin/python3',
            '/opt/home-butler/scripts/startup_self_check.py', '--check-status'
        )) -eq 0
    )
}

function Test-DialogueQualification {
    return (
        (Invoke-WslFixed -Arguments @(
            '-d', $distroName, '-u', 'homebutler', '--exec', '/usr/bin/python3',
            '/opt/home-butler/scripts/dialogue_qualification.py', '--check-status'
        )) -eq 0
    )
}

function Test-AliceSkillHealth {
    return (
        (Invoke-WslFixed -Arguments @(
            '-d', $distroName, '-u', 'root', '--exec', '/usr/bin/python3',
            '/opt/home-butler/scripts/alice_skill_health.py', '--check-status'
        )) -eq 0
    )
}

function Get-WslBootId {
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $lines = @(& $wslExe @(
            '-d', $distroName, '-u', 'homebutler', '--exec', '/usr/bin/cat',
            '/proc/sys/kernel/random/boot_id'
        ) 2> $null)
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($exitCode -ne 0 -or $lines.Count -ne 1) {
        return $null
    }
    $bootId = ([string]$lines[0]).Trim().ToLowerInvariant()
    if ($bootId -notmatch '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') {
        return $null
    }
    return $bootId
}

function Update-StartupProofHistory {
    param(
        [Parameter(Mandatory)][string]$WindowsBootId,
        [Parameter(Mandatory)][string]$WslBootId,
        [Parameter(Mandatory)][string]$Accelerator,
        [Parameter(Mandatory)][bool]$DialogueQualificationReady
    )

    $entries = @()
    if (Test-Path -LiteralPath $proofHistoryPath -PathType Leaf) {
        try {
            $item = Get-Item -LiteralPath $proofHistoryPath
            if ($item.Length -le 1048576) {
                $existing = Get-Content -LiteralPath $proofHistoryPath -Raw | ConvertFrom-Json
                if ($existing.schema_version -eq 2 -and $null -ne $existing.entries) {
                    $entries = @($existing.entries)
                }
            }
        }
        catch {
            $entries = @()
        }
    }
    $alreadyRecorded = @($entries | Where-Object {
        $_.windows_boot_id -eq $WindowsBootId
    }).Count -gt 0
    if (-not $alreadyRecorded) {
        $entries += [pscustomobject]@{
            windows_boot_id = $WindowsBootId
            wsl_boot_id = $WslBootId
            verified_at = [DateTime]::UtcNow.ToString('o')
            accelerator = $Accelerator
            startup_self_check_ready = $true
            alice_public_ready = $true
            dialogue_qualification_ready = $DialogueQualificationReady
        }
        if ($entries.Count -gt 20) {
            $entries = @($entries | Select-Object -Last 20)
        }
    }
    $document = [ordered]@{
        schema_version = 2
        baseline_boot_id = if ($entries.Count -gt 0) { $entries[0].windows_boot_id } else { $null }
        verified_reboot_count = [Math]::Max(0, $entries.Count - 1)
        entries = @($entries)
    }
    $temporary = "$proofHistoryPath.tmp"
    [System.IO.File]::WriteAllText(
        $temporary,
        ($document | ConvertTo-Json -Depth 5 -Compress) + [Environment]::NewLine,
        [System.Text.UTF8Encoding]::new($false)
    )
    Move-Item -LiteralPath $temporary -Destination $proofHistoryPath -Force
    return [int]$document.verified_reboot_count
}

function Write-WatchdogStatus {
    param([Parameter(Mandatory)][System.Collections.IDictionary]$Status)

    if (-not (Test-Path -LiteralPath $stateRoot -PathType Container)) {
        New-Item -ItemType Directory -Path $stateRoot | Out-Null
    }
    $temporary = "$statusPath.tmp"
    $json = $Status | ConvertTo-Json -Depth 5 -Compress
    [System.IO.File]::WriteAllText(
        $temporary,
        $json + [Environment]::NewLine,
        [System.Text.UTF8Encoding]::new($false)
    )
    Move-Item -LiteralPath $temporary -Destination $statusPath -Force
}

$aliceProbeFailures = 0
$windowsBootId = try {
    (Get-CimInstance -ClassName Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString('o')
}
catch {
    'unavailable'
}
while ($true) {
    $repairs = [System.Collections.Generic.List[string]]::new()
    $inactiveUnits = [System.Collections.Generic.List[string]]::new()
    $runtimeTaskPresent = Start-PinnedTaskIfNeeded -TaskName $runtimeTaskName
    $gpuTaskPresent = Start-PinnedTaskIfNeeded -TaskName $gpuTaskName

    if ($runtimeTaskPresent) {
        Start-Sleep -Milliseconds 500
    }
    $wslReady = (
        (Invoke-WslFixed -Arguments @(
            '-d', $distroName, '-u', 'homebutler', '--exec', '/usr/bin/test',
            '-x', '/usr/bin/systemctl'
        )) -eq 0
    )

    if ($wslReady) {
        foreach ($unit in $requiredUnits) {
            $active = (
                (Invoke-WslFixed -Arguments @(
                    '-d', $distroName, '-u', 'root', '--exec',
                    '/usr/bin/systemctl', 'is-active', '--quiet', '--', $unit
                )) -eq 0
            )
            if (-not $active) {
                $inactiveUnits.Add($unit)
                $started = (
                    (Invoke-WslFixed -Arguments @(
                        '-d', $distroName, '-u', 'root', '--exec',
                        '/usr/bin/systemctl', 'start', '--', $unit
                    )) -eq 0
                )
                if ($started) {
                    $repairs.Add("started:$unit")
                }
            }
        }
    }

    $alicePublicReady = $false
    if ($wslReady) {
        $alicePublicReady = (
            (Invoke-WslFixed -Arguments @(
                '-d', $distroName, '-u', 'root', '--exec', '/usr/bin/python3',
                '/opt/home-butler/scripts/alice_tailscale_funnel.py', '--public-probe'
            )) -eq 0
        )
        if ($alicePublicReady) {
            $aliceProbeFailures = 0
        }
        else {
            $aliceProbeFailures++
            if ($aliceProbeFailures -ge 2) {
                $restarted = (
                    (Invoke-WslFixed -Arguments @(
                        '-d', $distroName, '-u', 'root', '--exec',
                        '/usr/bin/systemctl', 'restart', '--',
                        'home-butler-alice-tunnel.service'
                    )) -eq 0
                )
                if ($restarted) {
                    $repairs.Add('restarted:home-butler-alice-tunnel.service')
                }
                $aliceProbeFailures = 0
            }
        }
    }

    if ($gpuTaskPresent -and -not (Test-GpuEndpoint)) {
        Start-PinnedTaskIfNeeded -TaskName $gpuTaskName | Out-Null
    }
    $gpuReady = Test-GpuEndpoint
    $modelMode = if ($wslReady) { Get-WslModelMode } else { 'unavailable' }
    $modelReady = $modelMode -in @('gpu', 'cpu_fallback')
    $startupSelfCheckReady = $wslReady -and (Test-StartupSelfCheck)
    $dialogueQualificationReady = (
        $wslReady -and (Test-DialogueQualification)
    )
    $aliceWebhookReady = $wslReady -and (Test-AliceSkillHealth)
    $wslBootId = if ($wslReady) { Get-WslBootId } else { $null }
    $healthy = (
        $runtimeTaskPresent -and $gpuTaskPresent -and $wslReady -and
        $inactiveUnits.Count -eq 0 -and $modelReady -and
        $startupSelfCheckReady -and $dialogueQualificationReady -and
        $alicePublicReady -and $aliceWebhookReady
    )
    $verifiedRebootCount = 0
    if (
        $healthy -and $windowsBootId -ne 'unavailable' -and
        -not [string]::IsNullOrWhiteSpace($wslBootId)
    ) {
        $verifiedRebootCount = Update-StartupProofHistory `
            -WindowsBootId $windowsBootId `
            -WslBootId $wslBootId `
            -Accelerator $modelMode `
            -DialogueQualificationReady $dialogueQualificationReady
    }
    Write-WatchdogStatus -Status ([ordered]@{
        schema_version = 3
        observed_at = [DateTime]::UtcNow.ToString('o')
        healthy = $healthy
        wsl_ready = $wslReady
        gpu_ready = $gpuReady
        model_ready = $modelReady
        accelerator = $modelMode
        startup_self_check_ready = $startupSelfCheckReady
        dialogue_qualification_ready = $dialogueQualificationReady
        alice_public_ready = $alicePublicReady
        alice_webhook_ready = $aliceWebhookReady
        verified_reboot_count = $verifiedRebootCount
        runtime_task_present = $runtimeTaskPresent
        gpu_task_present = $gpuTaskPresent
        inactive_units = @($inactiveUnits)
        repairs = @($repairs)
    })

    if ($Once) {
        break
    }
    Start-Sleep -Seconds $PollSeconds
}
