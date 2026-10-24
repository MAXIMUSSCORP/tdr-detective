# OS caption/build, GPU adapters and the local UTC offset.
# Parameters (stdin JSON): {} (none)
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

try {
    $null = [Console]::In.ReadToEnd()
    $os = Get-CimInstance -ClassName Win32_OperatingSystem
    # Read-only lookup of the update build revision (e.g. 26200.6899) for bug reports.
    $ubr = $null
    try {
        $ubr = (Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' -Name UBR).UBR
    } catch { $ubr = $null }

    $gpus = @(Get-CimInstance -ClassName Win32_VideoController | ForEach-Object {
        $vendorId = $null
        if ($_.PNPDeviceID -match 'VEN_([0-9A-Fa-f]{4})') { $vendorId = $Matches[1].ToUpperInvariant() }
        $date = $null
        if ($_.DriverDate) { $date = ([datetime]$_.DriverDate).ToUniversalTime().ToString('yyyy-MM-dd') }
        @{
            name = [string]$_.Name
            compatibility = [string]$_.AdapterCompatibility
            pci_vendor_id = $vendorId
            driver_version = [string]$_.DriverVersion
            driver_date = $date
        }
    })

    Write-Result @{
        os_caption = [string]$os.Caption
        os_build = [string]$os.BuildNumber
        ubr = $ubr
        gpus = $gpus
        utc_offset_minutes = [int][System.TimeZoneInfo]::Local.GetUtcOffset([datetime]::UtcNow).TotalMinutes
    }
} catch {
    Write-Failure $_
}
