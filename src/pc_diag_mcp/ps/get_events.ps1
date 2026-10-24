# Reads Event Log records with Get-WinEvent -FilterXml.
# Parameters (stdin JSON): {"filters": [<QueryList XML>, ...], "max_events": int}
# The QueryList documents are built and escaped by pc_diag_mcp; this script only runs them.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false

function Write-Result($Data) {
    ConvertTo-Json -InputObject @{ ok = $true; data = $Data } -Depth 6 -Compress
}

function Write-Failure($ErrorRecord) {
    $message = $ErrorRecord.Exception.Message
    $kind = 'error'
    if ($ErrorRecord.Exception -is [System.UnauthorizedAccessException] -or $message -match 'Access is denied|Access denied|unauthorized') {
        $kind = 'unavailable'
    }
    ConvertTo-Json -InputObject @{ ok = $false; kind = $kind; error = $message } -Compress
}

try {
    $params = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $max = [int]$params.max_events
    if ($max -lt 1 -or $max -gt 5000) { throw 'max_events out of range' }

    $records = New-Object System.Collections.ArrayList
    foreach ($filter in @($params.filters)) {
        try {
            $events = Get-WinEvent -FilterXml ([xml][string]$filter) -MaxEvents $max -ErrorAction Stop
        } catch {
            if ($_.FullyQualifiedErrorId -like 'NoMatchingEventsFound*' -or
                $_.Exception.Message -like 'No events were found that match the specified selection criteria*') {
                continue
            }
            throw
        }
        foreach ($evt in $events) {
            $message = $null
            try { $message = $evt.Message } catch { $message = $null }
            [void]$records.Add(@{ xml = $evt.ToXml(); message = $message })
        }
    }
    Write-Result @($records.ToArray())
} catch {
    Write-Failure $_
}
