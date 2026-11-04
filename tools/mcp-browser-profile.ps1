<#
Launch Edge with a dedicated profile for gemini-web-mcp and expose CDP on 9333.

Nothing else should sign in to this profile: the whole point is that only the
MCP rotates the session cookie, so a browser elsewhere cannot invalidate it.

    .\mcp-browser-profile.ps1            # start (or reuse) the profile
    .\mcp-browser-profile.ps1 -Dump      # start, then export cookies to the MCP
#>
[CmdletBinding()]
param(
    [switch]$Dump,
    [int]$Port = 9333,
    [string]$Edge = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
)

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
$profile = Join-Path $repo ".cache\edge-mcp-profile"
New-Item -ItemType Directory -Force -Path $profile | Out-Null

$running = $false
try { $null = Invoke-WebRequest "http://127.0.0.1:$Port/json/version" -UseBasicParsing -TimeoutSec 2; $running = $true } catch {}

if (-not $running) {
    $args = @(
        "--remote-debugging-port=$Port",
        "--user-data-dir=$profile",
        "--no-first-run",
        "--no-default-browser-check",
        "https://gemini.google.com/app"
    )
    Start-Process -FilePath $Edge -ArgumentList $args
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 500
        try { $null = Invoke-WebRequest "http://127.0.0.1:$Port/json/version" -UseBasicParsing -TimeoutSec 2; $running = $true; break } catch {}
    }
}

if (-not $running) {
    Write-Error "CDP did not come up on port $Port (Edge policy may block remote debugging)."
    exit 1
}
Write-Host "CDP ready on http://127.0.0.1:$Port  (profile: $profile)"
Write-Host "Sign in to gemini.google.com in that window if you have not already."

if ($Dump) {
    $staging = Join-Path $repo ".cache\cookies-cdp.json"
    & node (Join-Path $PSScriptRoot "dump_cookies_cdp.mjs") $Port $staging
    if ($LASTEXITCODE -ne 0) { Write-Error "cookie dump failed (code $LASTEXITCODE)"; exit $LASTEXITCODE }
    $target = Join-Path $HOME ".config\gemini-web-mcp\cookies.json"
    New-Item -ItemType Directory -Force -Path (Split-Path $target) | Out-Null
    if (Test-Path $target) {
        Copy-Item $target "$target.bak-$(Get-Date -Format yyyyMMdd-HHmmss)" -Force
    }
    [System.IO.File]::WriteAllText($target, (Get-Content $staging -Raw), (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "wrote $target"
}