# Host identifiers used ONLY by `pc_diag_mcp.record` so the scrubber knows which literal
# strings to replace. Never exposed through an MCP tool.
# Parameters (stdin JSON): {} (none)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false

try {
    $null = [Console]::In.ReadToEnd()
    ConvertTo-Json -Compress -InputObject @{
        ok = $true
        data = @{
            computer_name = $env:COMPUTERNAME
            user_name = $env:USERNAME
            user_profile = $env:USERPROFILE
            user_domain = $env:USERDOMAIN
        }
    }
} catch {
    ConvertTo-Json -Compress -InputObject @{ ok = $false; kind = 'error'; error = $_.Exception.Message }
}
