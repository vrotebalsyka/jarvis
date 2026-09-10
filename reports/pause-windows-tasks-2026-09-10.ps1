# One-off owner-authorized operation. Run as Windows administrator.
$ErrorActionPreference = 'Stop'
$taskNames = @(
 'Home Butler LAN Forwarding', 'Home Butler Ollama GPU',
 'Home Butler Scheduler Wake', 'Home Butler Scheduler Wake Sync',
 'Home Butler WSL Runtime'
)
foreach ($taskName in $taskNames) {
 Disable-ScheduledTask -TaskName $taskName -TaskPath '\' | Out-Null
 Stop-ScheduledTask -TaskName $taskName -TaskPath '\'
}
foreach ($taskName in $taskNames) {
 if ((Get-ScheduledTask -TaskName $taskName -TaskPath '\').State -ne 'Disabled') {
  throw 'Jarvis task is not disabled'
 }
}
