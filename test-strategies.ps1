# Host-side runner for Windows PowerShell 5.1+ / PowerShell 7. No .env changes.
[CmdletBinding()]
param(
    [string]$Image = 'ghcr.io/burakku1xd/warp-zapret-container:latest',
    [ValidateSet('missing', 'always', 'never')][string]$Pull = 'missing',
    [string[]]$Strategies = @(),
    [ValidateRange(1, 99999)][int]$Rounds = 3,
    [ValidateRange(1, 99999)][int]$StartupTimeout = 150,
    [ValidateRange(1, 99999)][int]$RequestTimeout = 10,
    [string]$TargetsFile = '',
    [string]$OutputDirectory = (Join-Path $PSScriptRoot 'test-results'),
    [switch]$Help
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($Help) {
    Write-Host @'
Usage: .\test-strategies.ps1 [-Image IMAGE] [-Pull missing|always|never]
  [-Strategies general,alt2] [-Rounds 3] [-StartupTimeout 150]
  [-RequestTimeout 10] [-TargetsFile FILE] [-OutputDirectory DIR] [-Help]
Targets file: one public HTTPS URL per line; # comments allowed.
Requires Docker Desktop (Linux/amd64 containers), curl.exe and /dev/net/tun.
Tests accept Cloudflare's terms and create a temporary WARP registration.
Only runner-owned containers and the temporary volume are removed.
Exit codes: 0 = at least one full PASS, 1 = no full PASS, 2 = runner error.
Ctrl+C runs cleanup and leaves partial reports.
'@
    exit 0
}

function Invoke-Docker {
    param([string[]]$DockerArgs)
    # Windows PowerShell treats native stderr differently from PowerShell 7.
    # Check the native exit code explicitly, not PowerShell's error stream.
    $ErrorActionPreference = 'Continue'
    $result = @(& docker @DockerArgs)
    $code = $LASTEXITCODE
    if ($code -ne 0) { throw "Docker command '$($DockerArgs[0])' failed (exit $code)." }
    return $result
}

function Test-Probe {
    param([string]$Proxy, [string]$Url, [bool]$Trace)
    # A nonempty list of empty entries disables ambient NO_PROXY without
    # Windows PowerShell 5.1's loss of empty native arguments.
    $curlArgs = @(
        '--disable', '--silent', '--proxy', $Proxy, '--noproxy', ',',
        '--connect-timeout', "$RequestTimeout", '--max-time', "$RequestTimeout",
        '--proto', '=https', '--write-out', "`n%{http_code}`t%{time_total}"
    )
    if ($Trace) { $curlArgs += @('--max-filesize', '4096') }
    else { $curlArgs += @('--head', '--output', 'NUL') }
    $curlArgs += $Url
    $ErrorActionPreference = 'Continue'
    # The trace body contains an IP: keep it in memory, never in reports/logs.
    $raw = @(& curl.exe @curlArgs 2>$null)
    $code = $LASTEXITCODE
    $text = $raw -join "`n"
    $http = '000'
    $seconds = '0'
    if ($text -match '(?m)^([0-9]{3})\t([0-9]+(?:\.[0-9]+)?)\r?$') {
        $http = $Matches[1]
        $seconds = $Matches[2]
    }
    $ok = $false
    if ($code -eq 0) {
        if ($Trace) { $ok = ($http -eq '200' -and $text -match '(?m)^warp=on\r?$') }
        else { $ok = ($http -match '^[23][0-9]{2}$') }
    }
    return [pscustomobject]@{
        Status = $(if ($ok) { 'PASS' } else { 'FAIL' })
        Http = $http
        Seconds = $seconds
        Exit = $code
    }
}

$active = ''
$volume = ''
$report = ''
$exitCode = 2
try {
    if ($Image.StartsWith('-') -or $Image -match '[\r\n\t]') { throw 'Invalid image name.' }
    foreach ($command in @('docker', 'curl.exe')) {
        if (-not (Get-Command $command -ErrorAction SilentlyContinue)) { throw "$command is required." }
    }
    $targets = @(
        'https://discord.com/',
        'https://www.youtube.com/',
        'https://www.google.com/generate_204',
        'https://www.gstatic.com/generate_204'
    )
    if ($TargetsFile) {
        $targets = @(Get-Content -LiteralPath $TargetsFile | Where-Object { $_ -and -not $_.StartsWith('#') })
    }
    if (-not $targets.Count) { throw 'Targets file is empty.' }
    foreach ($url in $targets) {
        if ($url -cnotmatch '^https://[a-zA-Z0-9.-]+(:[0-9]+)?(/[a-zA-Z0-9/._~-]*)?$') {
            throw 'Targets must be public HTTPS URLs without credentials or query strings.'
        }
    }
    if ((Invoke-Docker -DockerArgs @('info', '--format', '{{.OSType}}')) -ne 'linux') {
        throw 'A Linux Docker daemon is required.'
    }
    if ($Pull -eq 'always') {
        Invoke-Docker -DockerArgs @('pull', $Image) | Out-Host
    } else {
        $imagePresent = $true
        try { Invoke-Docker -DockerArgs @('image', 'inspect', $Image) 2>$null | Out-Null }
        catch { $imagePresent = $false }
        if (-not $imagePresent) {
            if ($Pull -eq 'never') { throw 'Image is not available locally.' }
            Invoke-Docker -DockerArgs @('pull', $Image) | Out-Host
        }
    }
    if ((Invoke-Docker -DockerArgs @('image', 'inspect', '--format', '{{.Os}}/{{.Architecture}}', $Image)) -ne 'linux/amd64') {
        throw 'The image must be linux/amd64.'
    }
    $imageId = Invoke-Docker -DockerArgs @('image', 'inspect', '--format', '{{.Id}}', $Image)
    $catalog = @(Invoke-Docker -DockerArgs @('run', '--rm', '--network', 'none', $imageId, '--list-strategies'))
    if (-not $catalog.Count) { throw 'Empty strategy catalog.' }
    foreach ($name in $catalog) {
        if ($name -cnotmatch '^[a-z0-9]+(-[a-z0-9]+)*$') { throw 'Invalid strategy catalog.' }
    }
    $selected = $catalog
    if ($Strategies.Count) {
        $selected = @()
        foreach ($name in $Strategies) {
            if ($catalog -cnotcontains $name) { throw "Unknown strategy: $name" }
            if ($selected -cnotcontains $name) { $selected += $name }
        }
    }
    $runId = 'warp-zapret-test-' + (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmss') + '-' + [guid]::NewGuid().ToString('N')
    $report = Join-Path $OutputDirectory $runId
    New-Item -ItemType Directory -Path $report | Out-Null
    $summary = Join-Path $report 'summary.tsv'
    $details = Join-Path $report 'checks.tsv'
    $header = "strategy`tstatus`tstartup_seconds`tpassed`ttotal"
    Set-Content -LiteralPath $summary -Value $header -Encoding UTF8
    Set-Content -LiteralPath $details -Value "strategy`tround`tproxy`ttarget`tstatus`thttp_code`tseconds`tcurl_exit" -Encoding UTF8
    Set-Content -LiteralPath (Join-Path $report 'run.txt') -Encoding UTF8 -Value @(
        "image=$Image", "image_id=$imageId", "rounds=$Rounds",
        "startup_timeout=$StartupTimeout", "request_timeout=$RequestTimeout"
    )
    Invoke-Docker -DockerArgs @('volume', 'create', '--label', 'warp-zapret.strategy-test=true', $runId) | Out-Null
    $volume = $runId
    $winners = @()
    $total = $Rounds * 2 * ($targets.Count + 1)
    Write-Host $header
    foreach ($strategy in $selected) {
        Write-Host "Testing: $strategy"
        $passed = 0
        $status = 'STARTUP_FAIL'
        $timer = [Diagnostics.Stopwatch]::StartNew()
        try {
            $active = Invoke-Docker -DockerArgs @(
                'create', '--name', "$runId-$strategy", '--init', '--restart', 'no',
                '--memory', '256m', '--memory-swap', '256m',
                '--env', 'MALLOC_TRIM_THRESHOLD_=1048576', '--env', 'MALLOC_MMAP_THRESHOLD_=1048576',
                '--cap-add', 'NET_ADMIN', '--cap-add', 'NET_RAW',
                '--device', '/dev/net/tun:/dev/net/tun',
                '--publish', '127.0.0.1::8888', '--publish', '127.0.0.1::1080',
                '--mount', "type=volume,src=$volume,dst=/var/lib/cloudflare-warp",
                '--tmpfs', '/tmp', '--tmpfs', '/run', '--no-healthcheck',
                $imageId, '--strategy', $strategy
            )
            Invoke-Docker -DockerArgs @('start', $active) | Out-Null
            $readyCommand = "ss -lnt | grep -q ':8888 ' && ss -lnt | grep -q ':1080 '"
            while ($timer.Elapsed.TotalSeconds -lt $StartupTimeout) {
                if ((Invoke-Docker -DockerArgs @('inspect', '--format', '{{.State.Running}}', $active)) -ne 'true') { break }
                & docker exec $active /usr/bin/bash -c $readyCommand *> $null
                if ($LASTEXITCODE -eq 0) { $status = 'READY'; break }
                Start-Sleep -Seconds 2
            }
        } catch {
            # Record the failed launch and continue with the remaining strategies.
            Write-Warning "Strategy $strategy failed to start."
        }
        $startup = [int][math]::Floor($timer.Elapsed.TotalSeconds)
        if ($status -eq 'READY') {
            $http = Invoke-Docker -DockerArgs @('port', $active, '8888/tcp')
            $socks = Invoke-Docker -DockerArgs @('port', $active, '1080/tcp')
            if ($http -notmatch '^127\.0\.0\.1:[0-9]+$' -or $socks -notmatch '^127\.0\.0\.1:[0-9]+$') {
                throw 'Unexpected port bindings.'
            }
            for ($round = 1; $round -le $Rounds; $round++) {
                foreach ($kind in @('HTTP', 'SOCKS')) {
                    $proxy = if ($kind -eq 'HTTP') { "http://$http" } else { "socks5h://$socks" }
                    $check = Test-Probe $proxy 'https://www.cloudflare.com/cdn-cgi/trace' $true
                    Add-Content -LiteralPath $details -Encoding UTF8 -Value "$strategy`t$round`t$kind`tWARP`t$($check.Status)`t$($check.Http)`t$($check.Seconds)`t$($check.Exit)"
                    if ($check.Status -eq 'PASS') {
                        $passed++
                        foreach ($target in $targets) {
                            $check = Test-Probe $proxy $target $false
                            Add-Content -LiteralPath $details -Encoding UTF8 -Value "$strategy`t$round`t$kind`t$target`t$($check.Status)`t$($check.Http)`t$($check.Seconds)`t$($check.Exit)"
                            if ($check.Status -eq 'PASS') { $passed++ }
                        }
                    } else {
                        foreach ($target in $targets) {
                            Add-Content -LiteralPath $details -Encoding UTF8 -Value "$strategy`t$round`t$kind`t$target`tSKIP`t-`t-`t-"
                        }
                    }
                }
                if ($round -lt $Rounds) { Start-Sleep -Seconds 1 }
            }
            $status = 'PARTIAL'
            if ($passed -eq $total) { $status = 'PASS'; $winners += $strategy }
        }
        $row = "$strategy`t$status`t$startup`t$passed`t$total"
        Add-Content -LiteralPath $summary -Value $row -Encoding UTF8
        Write-Host $row
        if ($active) {
            Invoke-Docker -DockerArgs @('stop', '--time', '20', $active) | Out-Null
            Invoke-Docker -DockerArgs @('rm', $active) | Out-Null
            $active = ''
        }
    }
    if ($winners.Count) {
        Write-Host "Fully passing strategies: $($winners -join ', ')"
        $exitCode = 0
    } else {
        Write-Host 'No strategy passed all checks.'
        $exitCode = 1
    }
} catch {
    Write-Host "Runner failed: $($_.Exception.Message)" -ForegroundColor Red
    $exitCode = 2
} finally {
    if ($active) {
        try { Invoke-Docker -DockerArgs @('rm', '--force', $active) | Out-Null }
        catch { Write-Warning "Cleanup failed: container $active"; $exitCode = 2 }
    }
    if ($volume) {
        try { Invoke-Docker -DockerArgs @('volume', 'rm', $volume) | Out-Null }
        catch { Write-Warning "Cleanup failed: volume $volume"; $exitCode = 2 }
    }
    if ($report) { Write-Host "Reports: $report" }
}
exit $exitCode
