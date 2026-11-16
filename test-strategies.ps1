# Host-side runner for Windows PowerShell 5.1+ / PowerShell 7. No .env changes.
[CmdletBinding()]
param(
    [string]$Image = 'ghcr.io/burakku1xd/warp-zapret-container:latest',
    [ValidateSet('missing', 'always', 'never')][string]$Pull = 'missing',
    [string[]]$Strategies = @(),
    [ValidateRange(1, 99999)][int]$Rounds = 3,
    [ValidateRange(1, 99999)][int]$StartupTimeout = 150,
    [ValidateRange(1, 99999)][int]$RequestTimeout = 10,
    [ValidateRange(1, 99999)][int]$Connections = 2,
    [string]$DownloadUrl = '',
    [ValidateRange(0, 999999999)][long]$DownloadBytes = 0,
    [string]$DownloadSha256 = '',
    [string]$UploadUrl = '',
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
  [-RequestTimeout 10] [-TargetsFile FILE] [-OutputDirectory DIR] [-Connections 2]
  [-DownloadUrl URL -DownloadBytes N -DownloadSha256 HASH] [-UploadUrl URL] [-Help]
Order: direct host reference, WARP without zapret, selected strategies.
Download checks exact size/SHA256; upload endpoint must echo 65536 zero bytes.
Targets file: one public HTTPS URL per line; # comments allowed.
Requires Docker Desktop (Linux/amd64 containers), curl.exe and /dev/net/tun.
Tests accept Cloudflare's terms and create a temporary WARP registration.
Only runner-owned containers and the temporary volume are removed.
Exit codes: 0 = at least one AVAILABLE strategy, 1 = none available, 2 = runner error.
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
    param([string]$Proxy, [string]$Url, [string]$Type, [string]$Protocol, [string]$Kind)
    # Feed the literal bypass rule through stdin: Unix PowerShell expands '*'
    # in native argv, while Windows PowerShell 5.1 drops empty native arguments.
    $routeArgs = if ($Kind -eq 'DIRECT') { @('--proxy', $Proxy, '--config', '-') } else { @('--proxy', $Proxy, '--noproxy', ',') }
    $curlArgs = @('--disable', '--silent') + $routeArgs + @(
        '--connect-timeout', "$RequestTimeout", '--max-time', "$RequestTimeout",
        '--proto', '=https', '--proto-redir', '=https', '--location', '--max-redirs', '3',
        '--write-out', "`n%{http_code}`t%{time_total}`t%{size_download}`t%{time_starttransfer}")
    switch ($Protocol) {
        'HTTP1.1' { $curlArgs += '--http1.1' }
        'TLS1.2' { $curlArgs += @('--tlsv1.2', '--tls-max', '1.2') }
        'TLS1.3' { $curlArgs += @('--tlsv1.3', '--tls-max', '1.3') }
    }
    $bodyFile = Join-Path $report 'body.tmp'
    switch ($Type) {
        'trace' { $curlArgs += @('--max-filesize', '4096') }
        'download' { $curlArgs += @('--max-filesize', "$DownloadBytes", '--output', $bodyFile) }
        'upload' { $curlArgs += @('--max-filesize', '65536', '--data-binary', "@$uploadFile", '--output', $bodyFile) }
        default { $curlArgs += @('--output', 'NUL') }
    }
    $curlArgs += $Url
    $ErrorActionPreference = 'Continue'
    if ($Kind -eq 'DIRECT') { $raw = @('noproxy = "*"' | & curl.exe @curlArgs 2>$null) }
    else { $raw = @(& curl.exe @curlArgs 2>$null) }
    $exit = $LASTEXITCODE
    $text = $raw -join "`n"
    $http = '000'; $seconds = '0'; $bytes = '0'; $ttfb = '0'
    if ($text -match '(?m)^([0-9]{3})\t([0-9]+(?:\.[0-9]+)?)\t([0-9]+)\t([0-9]+(?:\.[0-9]+)?)\r?$') {
        $http = $Matches[1]; $seconds = $Matches[2]; $bytes = $Matches[3]; $ttfb = $Matches[4]
    }
    $status = 'FAIL'
    if ($exit -eq 0 -and $http -match '^2[0-9]{2}$') {
        $status = 'PASS'
        if ($Type -eq 'trace') {
            if ($Kind -eq 'DIRECT') {
                if ($text -match '(?m)^warp=on\r?$') { $script:directClean = $false; $status = 'CONTAMINATED' }
                elseif ($text -notmatch '(?m)^warp=off\r?$') { $status = 'FAIL' }
            } elseif ($text -notmatch '(?m)^warp=on\r?$') { $status = 'FAIL' }
            if ($http -ne '200') { $status = 'FAIL' }
        } elseif ($Type -eq 'download' -or $Type -eq 'upload') {
            $hash = if (Test-Path -LiteralPath $bodyFile) { (Get-FileHash -LiteralPath $bodyFile -Algorithm SHA256).Hash } else { '' }
            if ($Type -eq 'download') {
                if ([long]$bytes -ne $DownloadBytes -or $hash -ne $DownloadSha256) { $status = 'FAIL' }
            } elseif ([long]$bytes -ne 65536 -or $hash -ne $uploadHash) { $status = 'FAIL' }
        }
    } elseif ($exit -eq 4) { $status = 'UNSUPPORTED' }
    elseif ($exit -eq 35 -or $exit -eq 60) { $status = 'TLS_ERROR' }
    elseif ($exit -eq 28) { $status = 'TIMEOUT' }
    elseif ($http -match '^[45][0-9]{2}$') { $status = 'HTTP_ERROR' }
    if ($Kind -eq 'DIRECT' -and $Type -eq 'trace' -and $status -ne 'PASS') { $script:directClean = $false }
    return [pscustomobject]@{ Status=$status; Http=$http; Seconds=$seconds; Exit=$exit; Bytes=$bytes; Ttfb=$ttfb }
}
function Save-Check {
    param($Check, [string]$Target, [string]$Protocol)
    if ($Target -eq 'GUARD' -or $Target -eq 'STOPPED_PROXY') {
        $Check.Status = if ($Check.Http -eq '000' -and $Check.Exit -eq 7) { 'EXPECTED_REFUSAL' } else { 'GUARD_FAIL' }
    }
    Add-Content -LiteralPath $details -Encoding UTF8 -Value "$strategy`t$connection`t$round`t$kind`t$Target`t$Protocol`t$($Check.Status)`t$($Check.Http)`t$($Check.Seconds)`t$($Check.Exit)`t$($Check.Bytes)`t$($Check.Ttfb)"
    if ($Check.Status -eq 'PASS') { $script:passed++; $script:connectionPassed++ }
    if ($Target -eq 'GUARD' -or $Target -eq 'STOPPED_PROXY') { return }
    $key = "$kind|$Target|$Protocol"
    if ($strategy -eq 'warp-control') {
        if ($Check.Status -eq 'PASS') { $controlGood[$key] = $true }
        elseif ($Check.Status -ne 'UNSUPPORTED') { $controlBad[$key] = $true }
    } elseif ($strategy -ne 'direct') {
        if ($Check.Status -eq 'PASS') { $currentGood[$key] = $true } else { $currentBad[$key] = $true }
    }
}
function Invoke-Requests {
    param([string]$Proxy)
    for ($round = 1; $round -le $Rounds; $round++) {
        Write-Host "  $strategy connection=$connection round=$round proxy=$kind"
        Save-Check (Test-Probe $Proxy 'https://www.cloudflare.com/cdn-cgi/trace' trace DEFAULT $kind) WARP DEFAULT
        foreach ($target in $targets) {
            foreach ($protocol in @('HTTP1.1', 'TLS1.2', 'TLS1.3')) {
                Save-Check (Test-Probe $Proxy $target site $protocol $kind) $target $protocol
            }
        }
        if ($DownloadUrl) { Save-Check (Test-Probe $Proxy $DownloadUrl download HTTP1.1 $kind) DOWNLOAD HTTP1.1 }
        if ($UploadUrl) { Save-Check (Test-Probe $Proxy $UploadUrl upload HTTP1.1 $kind) UPLOAD HTTP1.1 }
        if ($round -lt $Rounds) { Start-Sleep -Seconds 1 }
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
        'https://discord.com/', 'https://gateway.discord.gg/', 'https://cdn.discordapp.com/',
        'https://updates.discord.com/', 'https://www.youtube.com/', 'https://youtu.be/',
        'https://i.ytimg.com/', 'https://redirector.googlevideo.com/',
        'https://www.google.com/', 'https://www.gstatic.com/',
        'https://www.cloudflare.com/', 'https://cdnjs.cloudflare.com/'
    )
    if ($TargetsFile) {
        $targets = @(Get-Content -LiteralPath $TargetsFile | Where-Object { $_ -and -not $_.StartsWith('#') })
    }
    if (-not $targets.Count) { throw 'Targets file is empty.' }
    if ($DownloadUrl -or $DownloadBytes -or $DownloadSha256) {
        if (-not $DownloadUrl -or $DownloadBytes -lt 1 -or $DownloadSha256 -notmatch '^[0-9a-fA-F]{64}$') {
            throw 'Download requires URL, positive byte count, and SHA256.'
        }
    }
    foreach ($url in @($targets) + @($DownloadUrl, $UploadUrl | Where-Object { $_ })) {
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
    $imageHelp = (Invoke-Docker -DockerArgs @('run', '--rm', '--network', 'none', $imageId, '--help')) -join "`n"
    if (-not $imageHelp.Contains('--without-zapret') -or -not $imageHelp.Contains('--test-diagnostics')) {
        throw 'Image lacks comparison support; pull a newer image or build this revision.'
    }
    $runId = 'warp-zapret-test-' + (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmss') + '-' + [guid]::NewGuid().ToString('N')
    $report = Join-Path $OutputDirectory $runId
    New-Item -ItemType Directory -Path $report | Out-Null
    $summary = Join-Path $report 'summary.tsv'
    $details = Join-Path $report 'checks.tsv'
    $attempts = Join-Path $report 'connections.tsv'
    $header = "strategy`tstatus`tstartup_seconds`tpassed`ttotal`tstability`teffect`trecovered_checks`tqueue_packets`ttransport"
    Set-Content -LiteralPath $summary -Value $header -Encoding UTF8
    Set-Content -LiteralPath $details -Value "strategy`tconnection`tround`tproxy`ttarget`tprotocol`tstatus`thttp_code`tseconds`tcurl_exit`tbytes`tttfb" -Encoding UTF8
    Set-Content -LiteralPath $attempts -Value "strategy`tconnection`tstartup`tstartup_seconds`tpassed`ttotal`tnfqws_alive`ttcp_packets`tudp_packets`ttransport" -Encoding UTF8
    $metadata = Join-Path $report 'run.txt'
    Set-Content -LiteralPath $metadata -Encoding UTF8 -Value @(
        "report_version=2", "image=$Image", "image_id=$imageId", "rounds=$Rounds", "connections=$Connections",
        "startup_timeout=$StartupTimeout", "request_timeout=$RequestTimeout",
        "download=$(if ($DownloadUrl) { $DownloadUrl } else { 'NOT_RUN' })",
        "download_bytes=$DownloadBytes", "download_sha256=$DownloadSha256",
        "upload=$(if ($UploadUrl) { $UploadUrl } else { 'NOT_RUN' })", 'profile_evidence=NOT_ESTABLISHED', 'configured_transport=MASQUE_h3-with-h2-fallback'
    )
    $uploadFile = Join-Path $report 'upload.tmp'
    [IO.File]::WriteAllBytes($uploadFile, (New-Object byte[] 65536))
    $uploadHash = (Get-FileHash -LiteralPath $uploadFile -Algorithm SHA256).Hash
    $controlGood = @{}; $controlBad = @{}; $currentGood = @{}; $currentBad = @{}
    $directClean = $true
    $strategy = 'proxy-guard'; $connection = 0; $round = 0; $passed = 0; $connectionPassed = 0
    foreach ($kind in @('HTTP', 'SOCKS')) {
        $proxy = if ($kind -eq 'HTTP') { 'http://127.0.0.1:1' } else { 'socks5h://127.0.0.1:1' }
        $check = Test-Probe $proxy 'https://example.invalid/' site HTTP1.1 $kind
        Save-Check $check GUARD HTTP1.1
        if ($check.Http -ne '000' -or $check.Exit -ne 7) { throw "Closed proxy guard failed for $kind" }
    }
    Write-Host $header
    $perRoute = $Rounds * (1 + 3 * $targets.Count + [int][bool]$DownloadUrl + [int][bool]$UploadUrl)
    $strategy = 'direct'; $connection = 1; $kind = 'DIRECT'; $passed = 0; $connectionPassed = 0
    Invoke-Requests 'http://127.0.0.1:1'
    $row = "direct`tREFERENCE`t0`t$passed`t$perRoute`tNOT_APPLICABLE`tNOT_APPLICABLE`t0`t0`tNOT_APPLICABLE"
    Add-Content -LiteralPath $summary -Encoding UTF8 -Value $row
    Write-Host $row
    Add-Content -LiteralPath $metadata -Encoding UTF8 -Value @("direct_reference_clean=$([int]$directClean)", 'host_route_isolation=NOT_ESTABLISHED')
    Invoke-Docker -DockerArgs @('volume', 'create', '--label', 'warp-zapret.strategy-test=true', $runId) | Out-Null
    $volume = $runId
    $available = 0; $controlPassed = 0
    foreach ($strategy in @('warp-control') + @($selected)) {
        Write-Host "Testing: $strategy ($Connections cold connections)"
        $passed = 0; $startup = 0; $readyCount = 0; $queuePackets = 0L; $queueKnown = $true; $aliveAll = $true
        $currentGood = @{}; $currentBad = @{}
        for ($connection = 1; $connection -le $Connections; $connection++) {
            $status = 'STARTUP_FAIL'; $connectionPassed = 0; $alive = '0'; $tcp = '0'; $udp = '0'; $transport = 'UNKNOWN'
            $timer = [Diagnostics.Stopwatch]::StartNew()
            $selection = if ($strategy -eq 'warp-control') { @('--strategy', 'general', '--without-zapret') } else { @('--strategy', $strategy) }
            try {
                $active = Invoke-Docker -DockerArgs (@(
                    'create', '--name', "$runId-$strategy-$connection", '--init', '--restart', 'no',
                    '--memory', '256m', '--memory-swap', '256m',
                    '--env', 'MALLOC_TRIM_THRESHOLD_=1048576', '--env', 'MALLOC_MMAP_THRESHOLD_=1048576',
                    '--env', 'HTTP_PROXY=', '--env', 'HTTPS_PROXY=', '--env', 'ALL_PROXY=', '--env', 'FTP_PROXY=', '--env', 'NO_PROXY=',
                    '--env', 'http_proxy=', '--env', 'https_proxy=', '--env', 'all_proxy=', '--env', 'ftp_proxy=', '--env', 'no_proxy=',
                    '--cap-add', 'NET_ADMIN', '--cap-add', 'NET_RAW', '--device', '/dev/net/tun:/dev/net/tun',
                    '--publish', '127.0.0.1::8888', '--publish', '127.0.0.1::1080',
                    '--mount', "type=volume,src=$volume,dst=/var/lib/cloudflare-warp",
                    '--tmpfs', '/tmp', '--tmpfs', '/run', '--no-healthcheck', $imageId
                ) + $selection)
                Invoke-Docker -DockerArgs @('start', $active) | Out-Null
                $readyCommand = "test -f /run/warp-zapret.ready && ss -lnt | grep -q ':8888 ' && ss -lnt | grep -q ':1080 '"
                while ($timer.Elapsed.TotalSeconds -lt $StartupTimeout) {
                    if ((Invoke-Docker -DockerArgs @('inspect', '--format', '{{.State.Running}}', $active)) -ne 'true') { break }
                    & docker exec $active /usr/bin/bash -c $readyCommand *> $null
                    if ($LASTEXITCODE -eq 0) { $status = 'READY'; break }
                    Start-Sleep -Seconds 2
                }
            } catch { Write-Warning "Strategy $strategy failed to start." }
            $connectionStartup = [int][math]::Floor($timer.Elapsed.TotalSeconds)
            $startup += $connectionStartup
            if ($status -eq 'READY') {
                $readyCount++
                $http = Invoke-Docker -DockerArgs @('port', $active, '8888/tcp')
                $socks = Invoke-Docker -DockerArgs @('port', $active, '1080/tcp')
                if ($http -notmatch '^127\.0\.0\.1:[0-9]+$' -or $socks -notmatch '^127\.0\.0\.1:[0-9]+$') { throw 'Unexpected port bindings.' }
                foreach ($kind in @('HTTP', 'SOCKS')) {
                    $proxy = if ($kind -eq 'HTTP') { "http://$http" } else { "socks5h://$socks" }
                    Invoke-Requests $proxy
                }
                try {
                    $diagnostic = (Invoke-Docker -DockerArgs @('exec', $active, '/entrypoint.sh', '--test-diagnostics')) -join "`n"
                    if ($diagnostic -notmatch '^([01])\t([0-9]+)\t([0-9]+)\tUNKNOWN$') { throw 'Invalid evidence' }
                    $alive = $Matches[1]; $tcp = $Matches[2]; $udp = $Matches[3]
                    $queuePackets += [long]$tcp + [long]$udp
                } catch { $queueKnown = $false }
                if ($strategy -eq 'warp-control' -and ($alive -ne '0' -or ([long]$tcp + [long]$udp) -ne 0)) {
                    throw 'No-zapret control unexpectedly has nfqws or queued packets.'
                }
            }
            if ($alive -ne '1') { $aliveAll = $false }
            if ($active) {
                Invoke-Docker -DockerArgs @('stop', '--time', '20', $active) | Out-Null
                if ($status -eq 'READY') {
                    foreach ($kind in @('HTTP', 'SOCKS')) {
                        $round = 0
                        $proxy = if ($kind -eq 'HTTP') { "http://$http" } else { "socks5h://$socks" }
                        $check = Test-Probe $proxy 'https://example.invalid/' site HTTP1.1 $kind
                        $previousPassed = $passed; $previousConnectionPassed = $connectionPassed
                        Save-Check $check STOPPED_PROXY HTTP1.1
                        $passed = $previousPassed; $connectionPassed = $previousConnectionPassed
                        if ($check.Http -ne '000' -or $check.Exit -ne 7) { throw "Stopped container proxy guard failed for $kind" }
                    }
                }
                Invoke-Docker -DockerArgs @('rm', $active) | Out-Null
                $active = ''
            }
            Add-Content -LiteralPath $attempts -Encoding UTF8 -Value "$strategy`t$connection`t$status`t$connectionStartup`t$connectionPassed`t$($perRoute * 2)`t$alive`t$tcp`t$udp`t$transport"
        }
        $total = $Connections * 2 * $perRoute
        $status = if ($readyCount -eq 0) { 'STARTUP_FAIL' } else { 'PARTIAL' }
        $stability = 'FAIL'; $effect = 'NOT_ESTABLISHED'; $recovered = 0
        if ($passed -eq $total) {
            $status = 'AVAILABLE'
            $stability = if ($Connections -gt 1 -and $Rounds -gt 1) { 'PASS' } else { 'INSUFFICIENT_SAMPLES' }
        }
        if ($strategy -eq 'warp-control') {
            $controlPassed = $passed; $effect = 'CONTROL'
            $controlValid = $readyCount -eq $Connections -and $queueKnown -and $queuePackets -eq 0 -and -not $aliveAll
        }
        else {
            foreach ($key in $currentGood.Keys) {
                if ($controlBad.ContainsKey($key) -and -not $controlGood.ContainsKey($key) -and -not $currentBad.ContainsKey($key)) { $recovered++ }
            }
            if ($controlPassed -eq $total) { $effect = 'WARP_WORKS_WITHOUT_ZAPRET' }
            elseif ($recovered -gt 0 -and $queuePackets -gt 0 -and $queueKnown -and $aliveAll -and $directClean -and $controlValid -and $readyCount -eq $Connections) { $effect = 'OBSERVED_GAIN' }
            if ($status -eq 'AVAILABLE') { $available++ }
        }
        if (-not $queueKnown) { $queuePackets = 'UNKNOWN' }
        $row = "$strategy`t$status`t$startup`t$passed`t$total`t$stability`t$effect`t$recovered`t$queuePackets`tUNKNOWN"
        Add-Content -LiteralPath $summary -Encoding UTF8 -Value $row
        Write-Host $row
    }
    Write-Host 'AVAILABLE measures connectivity. OBSERVED_GAIN is a comparison, not proof of a particular desync profile.'
    $exitCode = if ($available -gt 0) { 0 } else { 1 }
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
    if ($report) {
        foreach ($temporaryName in @('body.tmp', 'upload.tmp')) {
            Remove-Item -LiteralPath (Join-Path $report $temporaryName) -ErrorAction SilentlyContinue
        }
        Write-Host "Reports: $report"
    }
}
exit $exitCode
