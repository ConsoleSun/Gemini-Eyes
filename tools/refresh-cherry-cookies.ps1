<#
Refresh the Gemini web cookies used by gemini-web-mcp.

Cherry Studio keeps the live gemini.google.com session in its webview profile, and
its webview rotates __Secure-1PSIDTS on its own -- which invalidates the copy the
MCP holds. So a refresh means: close Cherry Studio, copy the session, reopen it.

Usage:
    .\refresh-cherry-cookies.ps1                # refresh, then reopen Cherry Studio
    .\refresh-cherry-cookies.ps1 -NoRelaunch    # leave Cherry Studio closed
#>
[CmdletBinding()]
param(
    [switch]$NoRelaunch,
    [string]$CherryExe = "C:\Users\sir_c\AppData\Local\Programs\cherrystudio\Cherry Studio.exe"
)

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo ".venv\Scripts\python.exe"

Get-Process -ErrorAction SilentlyContinue |
    Where-Object { $_.ProcessName -match "(?i)cherry" } |
    ForEach-Object { & taskkill /PID $_.Id /F *>&1 | Out-Null }

for ($i = 0; $i -lt 10; $i++) {
    if (-not (Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match "(?i)cherry" })) { break }
    Start-Sleep -Seconds 1
}

& $python (Join-Path $PSScriptRoot "refresh_cookies.py") --write

if (-not $NoRelaunch -and (Test-Path $CherryExe)) {
    Start-Process -FilePath $CherryExe
    Write-Host "Cherry Studio reopened (its webview will rotate cookies again on use)."
}