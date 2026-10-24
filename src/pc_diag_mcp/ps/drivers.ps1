# Signed PnP drivers for an allowlisted set of device classes.
# Parameters (stdin JSON): {"device_classes": ["DISPLAY", ...]}
# Classes are compared as data with -contains; nothing from the input is built into WQL.
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
    $params = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $classes = @($params.device_classes | ForEach-Object { ([string]$_).ToUpperInvariant() })
    $drivers = @(Get-CimInstance -ClassName Win32_PnPSignedDriver |
        Where-Object { $_.DeviceClass -and ($classes -contains $_.DeviceClass.ToUpperInvariant()) } |
        ForEach-Object {
            $date = $null
            if ($_.DriverDate) { $date = ([datetime]$_.DriverDate).ToUniversalTime().ToString('yyyy-MM-dd') }
            @{
                device_name = [string]$_.DeviceName
                device_class = [string]$_.DeviceClass
                provider = [string]$_.DriverProviderName
                version = [string]$_.DriverVersion
                date = $date
                inf_name = $_.InfName
                signed = $_.IsSigned
            }
        })
    Write-Result $drivers
} catch {
    Write-Failure $_
}
