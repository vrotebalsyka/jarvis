[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$runtimeTaskName = 'Home Butler WSL Runtime'
$gpuTaskName = 'Home Butler Ollama GPU'
$wakeTaskName = 'Home Butler Scheduler Wake'
$wakeSyncTaskName = 'Home Butler Scheduler Wake Sync'
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
$runtimeTriggers = @($logonTrigger)
$wakeEpoch = $null
try {
    $wakeOutput = & $wslExe `
        -d $distroName `
        -u $serviceUser `
        --exec /usr/bin/python3 `
        /opt/home-butler/scripts/persistent_scheduler.py `
        --wake-json 2>$null
    if ($LASTEXITCODE -eq 0) {
        $wakeDocument = ($wakeOutput | Select-Object -Last 1) | ConvertFrom-Json
        if ($null -ne $wakeDocument.wake_epoch) {
            $wakeEpoch = [long]$wakeDocument.wake_epoch
        }
    }
} catch {
    # Logon remains a safe fallback; no schedule payload or secret is exported.
}
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
    -Trigger $runtimeTriggers `
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

$wakeSyncTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5)
$wakeSyncSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -DontStopOnIdleEnd `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -Hidden
$wakeSyncAction = New-ScheduledTaskAction `
    -Execute $conhostExe `
    -Argument "--headless `"$wslExe`" -d $distroName -u $serviceUser --exec /usr/bin/python3 /opt/home-butler/scripts/windows_wake_sync.py"
$wakeSyncTask = New-ScheduledTask `
    -Action $wakeSyncAction `
    -Trigger @($logonTrigger, $wakeSyncTrigger) `
    -Principal $principal `
    -Settings $wakeSyncSettings `
    -Description 'Mirror the nearest Ubuntu scheduler wake epoch into one bounded Windows wake task.'

function Register-BoundedHomeButlerTask {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][object]$Definition
    )

    $existing = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    $wasRunning = $null -ne $existing -and $existing.State -eq 'Running'
    if ($wasRunning) {
        Stop-ScheduledTask -TaskName $Name -ErrorAction Stop
    }
    try {
        Register-ScheduledTask `
            -TaskName $Name `
            -InputObject $Definition `
            -Force `
            -ErrorAction Stop | Out-Null
    } finally {
        if ($wasRunning -and
            $null -ne (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue)) {
            Start-ScheduledTask -TaskName $Name -ErrorAction Stop
        }
    }
}

Register-BoundedHomeButlerTask -Name $runtimeTaskName -Definition $runtimeTask
Register-BoundedHomeButlerTask -Name $gpuTaskName -Definition $gpuTask
Register-BoundedHomeButlerTask -Name $wakeSyncTaskName -Definition $wakeSyncTask

$wakeSource = Join-Path $PSScriptRoot 'windows-wake-sync.cs'
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$helperDirectory = Join-Path $env:LOCALAPPDATA 'HomeButler'
$wakeHelper = Join-Path $helperDirectory 'HomeButlerWakeSync.exe'
foreach ($requiredWakeFile in ($wakeSource, $compiler)) {
    if (-not (Test-Path -LiteralPath $requiredWakeFile -PathType Leaf)) {
        throw "Required wake helper source or compiler is missing."
    }
}
New-Item -ItemType Directory -Path $helperDirectory -Force | Out-Null
& $compiler /nologo /target:exe "/out:$wakeHelper" /reference:Microsoft.CSharp.dll $wakeSource
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $wakeHelper -PathType Leaf)) {
    throw 'The bounded Windows wake helper could not be built.'
}
if ($null -ne $wakeEpoch) {
    & $wakeHelper ([string]$wakeEpoch)
    if ($LASTEXITCODE -ne 0) {
        throw 'The bounded Windows wake task could not be synchronized.'
    }
    $wakeTask = Get-ScheduledTask -TaskName $wakeTaskName -ErrorAction Stop
    if (-not $wakeTask.Settings.WakeToRun -or
        $wakeTask.Actions.Count -ne 1 -or
        $wakeTask.Actions[0].Execute -ne (Join-Path $env:SystemRoot 'System32\schtasks.exe')) {
        throw 'The bounded Windows wake task failed readback verification.'
    }
}
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

Write-Output 'Home Butler uses hidden WSL tasks; Ubuntu supervises the GPU model, tunnel, scheduler wake, and watchdogs without PowerShell startup tasks.'
