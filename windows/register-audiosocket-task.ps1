#requires -Version 5.1
[CmdletBinding(SupportsShouldProcess=$true)]
param(
    [Parameter(Mandatory=$true)][ValidatePattern('^[\w .-]+$')][string]$Distro,
    [string]$RelayPath = (Join-Path $PSScriptRoot 'start-audiosocket-proxy.ps1'),
    [string]$TaskName = 'PhoneSys-AudioSocket',
    [ValidateScript({[bool][System.Net.IPAddress]::Parse($_)})][string]$ListenAddress = '0.0.0.0',
    [ValidateRange(1,65535)][int]$ListenPort = 19010,
    [ValidateRange(1,65535)][int]$TargetPort = 9010
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$RelayPath = (Resolve-Path -LiteralPath $RelayPath).ProviderPath
if ($RelayPath.Contains('"')) { throw 'Unsupported quote in relay path.' }
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    throw 'Task already exists. Review/export/remove the exact old task before replacing it.'
}
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$exe = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
$arguments = '-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -File "{0}" -Distro "{1}" -ListenAddress "{2}" -ListenPort {3} -TargetPort {4}' -f $RelayPath,$Distro,$ListenAddress,$ListenPort,$TargetPort
$action = New-ScheduledTaskAction -Execute $exe -Argument $arguments -WorkingDirectory (Split-Path $RelayPath)
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
if ($PSCmdlet.ShouldProcess($TaskName, 'Register current-user logon relay task (does not start it)')) {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings
    Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName,State,Actions,Principal
}
