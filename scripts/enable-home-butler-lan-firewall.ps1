#Requires -RunAsAdministrator
$ErrorActionPreference = 'Stop'

$standardName = 'Home Butler LAN 8780'
$standard = Get-NetFirewallRule -DisplayName $standardName -ErrorAction SilentlyContinue
if ($null -eq $standard) {
    New-NetFirewallRule `
        -DisplayName $standardName `
        -Direction Inbound `
        -Action Allow `
        -Protocol TCP `
        -LocalPort 8780 `
        -RemoteAddress 192.168.1.0/24 `
        -Profile Any | Out-Null
}

$hyperVName = 'HomeButlerLAN8780'
$wslCreatorId = '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}'
$hyperV = Get-NetFirewallHyperVRule -Name $hyperVName -ErrorAction SilentlyContinue
if ($null -eq $hyperV) {
    New-NetFirewallHyperVRule `
        -Name $hyperVName `
        -DisplayName $standardName `
        -Direction Inbound `
        -VMCreatorId $wslCreatorId `
        -Protocol TCP `
        -LocalPorts 8780 `
        -RemoteAddresses 192.168.1.0/24 `
        -Action Allow `
        -Enabled True | Out-Null
}

Write-Output 'Home Butler LAN firewall configured.'

& netsh interface portproxy delete v4tov4 `
    listenaddress=192.168.1.175 `
    listenport=8780 2>$null | Out-Null
$wslAddresses = (& wsl.exe -d Ubuntu -u root -- hostname -I) -split '\s+'
$wslAddress = $wslAddresses | Where-Object {
    $_ -match '^172\.(1[6-9]|2[0-9]|3[01])\.([0-9]{1,3})\.([0-9]{1,3})$'
} | Select-Object -First 1
if ([string]::IsNullOrWhiteSpace($wslAddress)) {
    throw 'Could not resolve the private Ubuntu address.'
}
& netsh interface portproxy add v4tov4 `
    listenaddress=192.168.1.175 `
    listenport=8780 `
    connectaddress=$wslAddress `
    connectport=8781 `
    protocol=tcp | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'Could not configure the stable Home Butler LAN forwarding rule.'
}

Write-Output 'Home Butler stable LAN forwarding configured.'

$taskName = 'Home Butler LAN Forwarding'
$taskUser = "$env:USERDOMAIN\$env:USERNAME"
$taskAction = New-ScheduledTaskAction `
    -Execute "$env:SystemRoot\System32\wsl.exe" `
    -Argument '-d Ubuntu -u root -- /opt/home-butler/scripts/update-home-butler-lan-forward.sh'
$taskTrigger = New-ScheduledTaskTrigger -AtLogOn -User $taskUser
$taskSettings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 2) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)
$taskSettings.Hidden = $true
Register-ScheduledTask `
    -TaskName $taskName `
    -Action $taskAction `
    -Trigger $taskTrigger `
    -Settings $taskSettings `
    -User $taskUser `
    -RunLevel Highest `
    -Force | Out-Null

Write-Output 'Home Butler Ubuntu-only startup forwarding task configured.'
