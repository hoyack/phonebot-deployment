#requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][ValidatePattern('^[\w .-]+$')][string]$Distro,
    [ValidateScript({[bool][System.Net.IPAddress]::Parse($_)})][string]$ListenAddress = '0.0.0.0',
    [ValidateRange(1,65535)][int]$ListenPort = 19010,
    [ValidateRange(1,65535)][int]$TargetPort = 9010,
    [ValidateRange(1,60)][int]$ConnectTimeoutSeconds = 5,
    [ValidateRange(1,120)][int]$DiscoveryTimeoutSeconds = 15
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$wsl = Join-Path $env:WINDIR 'System32\wsl.exe'

function Get-DistroAddress {
    # Bounded discovery: a stopped/unhealthy distro must not hang the relay.
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $wsl
    $info.Arguments = '-d "' + $Distro + '" -- hostname -I'
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $info
    try {
        [void]$process.Start()
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($DiscoveryTimeoutSeconds * 1000)) {
            $process.Kill()
            throw 'WSL address discovery timed out.'
        }
        if ($process.ExitCode -ne 0) { throw 'WSL address discovery failed; check distro and user.' }
        $addresses = @($stdout.Result.Trim() -split '\s+' | Where-Object {
            $ip = $null
            [System.Net.IPAddress]::TryParse($_, [ref]$ip) -and
            $ip.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork -and
            -not [System.Net.IPAddress]::IsLoopback($ip)
        })
        if ($addresses.Count -ne 1) {
            throw 'Expected one WSL IPv4 address; review networking rather than guessing an interface.'
        }
        return $addresses[0]
    }
    finally { $process.Dispose() }
}

# No ReuseAddress: a duplicate task must fail instead of sharing a listener.
# AudioSocket has no authentication/TLS. Restrict Windows firewall sources.
$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Parse($ListenAddress), $ListenPort)
$listener.Server.ExclusiveAddressUse = $true
$listener.Start(1)
Write-Information "AudioSocket relay listening on ${ListenAddress}:$ListenPort (single call)" -InformationAction Continue
try {
    while ($true) {
        $caller = $listener.AcceptTcpClient()
        $bridge = $null
        try {
            $target = Get-DistroAddress
            $caller.NoDelay = $true
            $bridge = [System.Net.Sockets.TcpClient]::new()
            $bridge.NoDelay = $true
            $connect = $bridge.ConnectAsync($target, $TargetPort)
            if (-not $connect.Wait($ConnectTimeoutSeconds * 1000)) { throw 'Bridge connection timed out.' }
            $callerStream = $caller.GetStream()
            $bridgeStream = $bridge.GetStream()
            $up = $callerStream.CopyToAsync($bridgeStream, 4096)
            $down = $bridgeStream.CopyToAsync($callerStream, 4096)
            [void][System.Threading.Tasks.Task]::WaitAny([System.Threading.Tasks.Task[]]@($up, $down))
        }
        catch {
            # Do not log audio, endpoints, call identifiers or exception payloads.
            Write-Warning 'Relay connection ended or target unavailable; inspect distro/bridge health.'
        }
        finally {
            if ($null -ne $bridge) { $bridge.Close() }
            $caller.Close()
        }
    }
}
finally { $listener.Stop() }
