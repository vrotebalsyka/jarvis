[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$runtimeTaskName = 'Home Butler WSL Runtime'
$gpuTaskName = 'Home Butler Ollama GPU'
$legacyTaskNames = @('Home Butler Watchdog')
$wslExe = Join-Path $env:SystemRoot 'System32\wsl.exe'
$conhostExe = Join-Path $env:SystemRoot 'System32\conhost.exe'
$distroName = 'Ubuntu'
$serviceUser = 'homebutler'

foreach ($requiredFile in ($wslExe, $conhostExe)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "Required startup file is missing: $requiredFile"
    }
}

$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
if ([string]::IsNullOrWhiteSpace($identity)) {
    throw 'Current Windows identity is unavailable.'
}

$principal = New-ScheduledTaskPrincipal `
    -UserId $identity `
    -LogonType Interactive `
    -RunLevel Limited
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$reportWakeTrigger = New-ScheduledTaskTrigger `
    -Daily `
    -At ([DateTime]::Today.AddHours(12).AddMinutes(58))
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -DontStopOnIdleEnd `
    -StartWhenAvailable `
    -WakeToRun `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -Hidden

$runtimeAction = New-ScheduledTaskAction `
    -Execute $conhostExe `
    -Argument "--headless `"$wslExe`" -d $distroName -u $serviceUser --exec /usr/bin/sleep infinity"
$runtimeTask = New-ScheduledTask `
    -Action $runtimeAction `
    -Trigger @($logonTrigger, $reportWakeTrigger) `
    -Principal $principal `
    -Settings $settings `
    -Description 'Keep the per-user Ubuntu WSL runtime alive so enabled Home Butler systemd units can run.'

$gpuAction = New-ScheduledTaskAction `
    -Execute $conhostExe `
    -Argument "--headless `"$wslExe`" -d $distroName -u $serviceUser --exec /usr/bin/python3 /opt/home-butler/scripts/windows_gpu_supervisor.py"
$gpuTask = New-ScheduledTask `
    -Action $gpuAction `
    -Trigger $logonTrigger `
    -Principal $principal `
    -Settings $settings `
    -Description 'Run the Ubuntu-owned pinned Ollama GPU supervisor without PowerShell.'

Register-ScheduledTask -TaskName $runtimeTaskName -InputObject $runtimeTask -Force | Out-Null
Register-ScheduledTask -TaskName $gpuTaskName -InputObject $gpuTask -Force | Out-Null
foreach ($legacyTaskName in $legacyTaskNames) {
    if (Get-ScheduledTask -TaskName $legacyTaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $legacyTaskName -Confirm:$false
    }
}

$desktop = [Environment]::GetFolderPath('Desktop')
if (-not [string]::IsNullOrWhiteSpace($desktop) -and (Test-Path -LiteralPath $desktop -PathType Container)) {
    $chatShortcut = Join-Path $desktop 'Домашний дворецкий.url'
    $shortcutContent = @(
        '[InternetShortcut]',
        'URL=http://127.0.0.1:8780/',
        "IconFile=$(Join-Path $PSScriptRoot 'app.ico')",
        'IconIndex=0'
    ) -join "`r`n"
    [System.IO.File]::WriteAllText(
        $chatShortcut,
        $shortcutContent + "`r`n",
        [System.Text.UTF8Encoding]::new($false)
    )
}

Write-Output 'Home Butler uses hidden WSL tasks; Ubuntu supervises the GPU model, tunnel, and watchdogs without PowerShell startup tasks.'
