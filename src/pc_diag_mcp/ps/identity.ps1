# Host identifiers used ONLY by `pc_diag_mcp.record` so the scrubber knows which literal
# strings to replace. Never exposed through an MCP tool.
# Read through .NET, not environment variables: collectors launch PowerShell with a minimal
# environment in which COMPUTERNAME, USERNAME and USERPROFILE are not set.
# Parameters (stdin JSON): {} (none)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false

try {
    $null = [Console]::In.ReadToEnd()
    ConvertTo-Json -Compress -InputObject @{
        ok = $true
        data = @{
            computer_name = [Environment]::MachineName
            user_name = [Environment]::UserName
            user_profile = [Environment]::GetFolderPath('UserProfile')
            user_domain = [Environment]::UserDomainName
        }
    }
} catch {
    ConvertTo-Json -Compress -InputObject @{ ok = $false; kind = 'error'; error = $_.Exception.Message }
}
