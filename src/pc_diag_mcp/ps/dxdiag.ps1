# Runs `dxdiag /x` into a temporary file, returns the XML text and deletes the file.
# Parameters (stdin JSON): {"wait_seconds": int}
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false

function Write-Result($Data) {
    ConvertTo-Json -InputObject @{ ok = $true; data = $Data } -Depth 6 -Compress
}

function Write-Failure($ErrorRecord) {
    $message = $ErrorRecord.Exception.Message
    $kind = 'error'
    if ($ErrorRecord.Exception -is [System.UnauthorizedAccessException] -or $message -match 'Access is denied|Access denied') {
        $kind = 'unavailable'
    }
    ConvertTo-Json -InputObject @{ ok = $false; kind = $kind; error = $message } -Compress
}

$path = $null
try {
    $params = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $wait = [int]$params.wait_seconds
    if ($wait -lt 1 -or $wait -gt 300) { throw 'wait_seconds out of range' }

    $path = Join-Path ([System.IO.Path]::GetTempPath()) ('pcdiag-dxdiag-' + [guid]::NewGuid().ToString('N') + '.xml')
    $proc = Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\dxdiag.exe') -ArgumentList @('/x', $path) -PassThru -WindowStyle Hidden
    $deadline = (Get-Date).AddSeconds($wait)
    while (-not $proc.HasExited -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }

    # dxdiag can exit before the XML is flushed; wait for a stable, non-empty file.
    $lastSize = -1
    while ((Get-Date) -lt $deadline) {
        if (Test-Path -LiteralPath $path) {
            $size = (Get-Item -LiteralPath $path).Length
            if ($size -gt 0 -and $size -eq $lastSize -and $proc.HasExited) { break }
            $lastSize = $size
        }
        Start-Sleep -Milliseconds 500
    }
    if (-not (Test-Path -LiteralPath $path)) {
        if (-not $proc.HasExited) { $proc.Kill() }
        throw "dxdiag did not produce output within $wait seconds"
    }
    $xml = [System.IO.File]::ReadAllText($path)
    Write-Result @{ xml = $xml }
} catch {
    Write-Failure $_
} finally {
    if ($path -and (Test-Path -LiteralPath $path)) { Remove-Item -LiteralPath $path -Force }
}
