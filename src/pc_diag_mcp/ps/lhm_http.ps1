# GET http://127.0.0.1:8085/data.json from LibreHardwareMonitor's web server, on the Windows
# side (used under WSL, where 127.0.0.1 is the Linux loopback). Fixed URL, GET only, no proxy,
# no redirects: the same server's /Sensor?action=Set endpoint writes values and is never called.
# Parameters (stdin JSON): {} (none)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false

function Write-Result($Data) {
    ConvertTo-Json -InputObject @{ ok = $true; data = $Data } -Depth 6 -Compress
}

try {
    $null = [Console]::In.ReadToEnd()
    $request = [System.Net.HttpWebRequest]::Create('http://127.0.0.1:8085/data.json')
    $request.Method = 'GET'
    $request.Proxy = $null
    $request.AllowAutoRedirect = $false
    $request.Timeout = 3000
    $request.ReadWriteTimeout = 3000
    $response = $request.GetResponse()
    try {
        if ([int]$response.StatusCode -ne 200) { throw "unexpected HTTP status $([int]$response.StatusCode)" }
        # Bounded read: never buffer more than 16 MB, whatever the server sends.
        $limit = 16MB
        $stream = $response.GetResponseStream()
        $buffer = New-Object System.IO.MemoryStream
        $chunk = New-Object byte[] 65536
        while (($read = $stream.Read($chunk, 0, $chunk.Length)) -gt 0) {
            if ($buffer.Length + $read -gt $limit) { throw 'data.json is larger than 16 MB' }
            $buffer.Write($chunk, 0, $read)
        }
        Write-Result @{ body = [System.Text.Encoding]::UTF8.GetString($buffer.ToArray()) }
    } finally {
        $response.Close()
    }
} catch {
    # Not running, web server disabled, or refused: all mean "not available".
    ConvertTo-Json -InputObject @{ ok = $false; kind = 'unavailable'; error = $_.Exception.Message } -Compress
}
