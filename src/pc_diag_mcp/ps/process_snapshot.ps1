# Memory, physical-disk and per-process state from non-localized CIM classes.
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

function ConvertTo-UtcText($Value) {
    if ($null -eq $Value) { return $null }
    return ([datetime]$Value).ToUniversalTime().ToString('o')
}

try {
    $null = [Console]::In.ReadToEnd()
    $os = Get-CimInstance -ClassName Win32_OperatingSystem

    # Formatted perf counters report rates; the first read after enumeration is often zero.
    $null = Get-CimInstance -ClassName Win32_PerfFormattedData_PerfDisk_PhysicalDisk
    Start-Sleep -Milliseconds 1000
    $disks = @(Get-CimInstance -ClassName Win32_PerfFormattedData_PerfDisk_PhysicalDisk |
        Where-Object { $_.Name -ne '_Total' } |
        ForEach-Object {
            @{
                name = [string]$_.Name
                queue_length = [double]$_.CurrentDiskQueueLength
                percent_busy = [double]$_.PercentDiskTime
                read_bytes_per_sec = [double]$_.DiskReadBytesPersec
                write_bytes_per_sec = [double]$_.DiskWriteBytesPersec
            }
        })

    $processes = @(Get-CimInstance -ClassName Win32_Process | ForEach-Object {
        @{
            pid = [int64]$_.ProcessId
            name = [string]$_.Name
            working_set_bytes = [int64]$_.WorkingSetSize
            private_bytes = [int64]$_.PrivatePageCount
            page_faults = [int64]$_.PageFaults
            read_bytes = [int64]$_.ReadTransferCount
            write_bytes = [int64]$_.WriteTransferCount
            cpu_seconds = ([double]$_.UserModeTime + [double]$_.KernelModeTime) / 1e7
            started = ConvertTo-UtcText $_.CreationDate
        }
    })

    Write-Result @{
        taken_at = (Get-Date).ToUniversalTime().ToString('o')
        memory = @{
            total_bytes = [int64]$os.TotalVisibleMemorySize * 1024
            available_bytes = [int64]$os.FreePhysicalMemory * 1024
            commit_used_bytes = ([int64]$os.TotalVirtualMemorySize - [int64]$os.FreeVirtualMemory) * 1024
            commit_limit_bytes = [int64]$os.TotalVirtualMemorySize * 1024
        }
        disks = $disks
        processes = $processes
    }
} catch {
    Write-Failure $_
}
