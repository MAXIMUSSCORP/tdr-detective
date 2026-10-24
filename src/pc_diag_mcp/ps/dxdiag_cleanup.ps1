# Stops a dxdiag.exe that dxdiag.ps1 started with the given token and deletes its temp file.
# Run only after dxdiag.ps1 failed or was killed (e.g. the caller's timeout), which can leave
# dxdiag running. It matches our own process by the token in its command line, nothing else.
# Parameters (stdin JSON): {"token": "<16 hex chars>"}
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false

try {
    $params = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $token = [string]$params.token
    if ($token -notmatch '^[0-9a-f]{16}$') { throw 'invalid token' }
    $marker = 'pcdiag-dxdiag-' + $token + '.xml'

    $stopped = 0
    foreach ($process in @(Get-CimInstance -ClassName Win32_Process -Filter "Name = 'dxdiag.exe'")) {
        if ($process.CommandLine -and $process.CommandLine.Contains($marker)) {
            Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
            $stopped++
        }
    }
    $path = Join-Path ([System.IO.Path]::GetTempPath()) $marker
    if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force }
    ConvertTo-Json -Compress -InputObject @{ ok = $true; data = @{ stopped = $stopped } }
} catch {
    ConvertTo-Json -Compress -InputObject @{ ok = $false; kind = 'error'; error = $_.Exception.Message }
}
