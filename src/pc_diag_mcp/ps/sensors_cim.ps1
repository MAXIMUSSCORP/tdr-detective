# ACPI thermal zones and GPU engine utilization from non-localized CIM perf classes.
# Each backend reports its own failure so one missing class does not hide the other.
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

function Get-Backend([string]$ClassName, [scriptblock]$Project) {
    try {
        $null = Get-CimInstance -ClassName $ClassName
        Start-Sleep -Milliseconds 500
        $rows = @(Get-CimInstance -ClassName $ClassName | ForEach-Object $Project)
        return @{ ok = $true; rows = $rows }
    } catch {
        return @{ ok = $false; error = $_.Exception.Message }
    }
}

try {
    $null = [Console]::In.ReadToEnd()
    $thermal = Get-Backend 'Win32_PerfFormattedData_Counters_ThermalZoneInformation' {
        @{
            name = [string]$_.Name
            kelvin = [double]$_.Temperature
            kelvin_tenths = [double]$_.HighPrecisionTemperature
        }
    }
    $engines = Get-Backend 'Win32_PerfFormattedData_GPUPerformanceCounters_GPUEngine' {
        @{ name = [string]$_.Name; utilization = [double]$_.UtilizationPercentage }
    }
    Write-Result @{
        taken_at = (Get-Date).ToUniversalTime().ToString('o')
        thermal = $thermal
        gpu_engine = $engines
    }
} catch {
    Write-Failure $_
}
