<#
.SYNOPSIS
    Sets up tally-mcp on this PC: installs the server, configures Claude Desktop
    and Claude Code, and (for write tools) loads the bridge TDL in TallyPrime.

.DESCRIPTION
    Safe to run again: it updates the server to the latest version and rewrites
    only its own settings. Every file it changes is backed up first, next to
    the original (or in C:\TallyMCP), as <name>.bak-<date-time>.

.PARAMETER WriteCompanies
    Which companies the AI may change: "none" (read only), "*" (any open
    company), or names separated by ";". Asked for when not given.

.PARAMETER TallyIni
    Path to TallyPrime's tally.ini, when it isn't found automatically.

.EXAMPLE
    .\install.ps1
    .\install.ps1 -WriteCompanies "My Company (Test)"
#>
param(
    [string]$WriteCompanies,
    [string]$TallyIni
)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Repo       = 'svk-vijaykumar/tally-mcp'
$Package    = "https://github.com/$Repo/archive/refs/heads/main.zip"   # no git needed
$BridgeUrl  = "https://raw.githubusercontent.com/$Repo/main/tally/TallyMCPBridge.tdl"
$McpDir     = 'C:\TallyMCP'                     # the bridge TDL has C:\TallyMCP\queue hard-coded
$BridgePath = Join-Path $McpDir 'TallyMCPBridge.tdl'
$TallyUrl   = 'http://localhost:9000'
$Stamp      = Get-Date -Format 'yyyyMMdd-HHmmss'

function Write-Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Write-Ok($text)   { Write-Host "   $text" -ForegroundColor Green }
function Write-Note($text) { Write-Host "   $text" -ForegroundColor Yellow }

# Runs a native command without its stderr stopping the script (PowerShell 5.1
# turns redirected stderr into errors). Returns the exit code.
function Invoke-Native([scriptblock]$Command) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $Command 2>&1 | ForEach-Object { "   $_" } | Write-Host } finally { $ErrorActionPreference = $old }
    return $LASTEXITCODE
}

function Test-TallyPort {
    $client = New-Object Net.Sockets.TcpClient
    try { return $client.ConnectAsync('127.0.0.1', 9000).Wait(1500) -and $client.Connected }
    catch { return $false }
    finally { $client.Dispose() }
}

# Writes via a temp file; asks for admin rights only when the folder needs them.
function Save-Text($path, [string]$text, $encoding) {
    $tmp = Join-Path $env:TEMP ("tally-mcp-" + [IO.Path]::GetFileName($path))
    [IO.File]::WriteAllText($tmp, $text, $encoding)
    try {
        Copy-Item -LiteralPath $tmp -Destination $path -Force
    } catch [UnauthorizedAccessException] {
        Write-Note "Windows will ask for permission to change $path."
        $cmd = "Copy-Item -LiteralPath '$tmp' -Destination '$path' -Force"
        $p = Start-Process powershell -Verb RunAs -Wait -PassThru -ArgumentList '-NoProfile', '-Command', $cmd
        if ($p.ExitCode -ne 0) { throw "Could not write $path" }
    }
    Remove-Item -LiteralPath $tmp -ErrorAction SilentlyContinue
}

function Backup($path) {
    $bak = "$path.bak-$Stamp"
    try { Copy-Item -LiteralPath $path -Destination $bak }
    catch [UnauthorizedAccessException] {
        $bak = Join-Path $McpDir ([IO.Path]::GetFileName($path) + ".bak-$Stamp")
        Copy-Item -LiteralPath $path -Destination $bak
    }
    Write-Ok "Backup: $bak"
}

# Makes tally.ini load the bridge: one TDL= line for it (an older one is
# repointed) and User TDL=Yes. Other lines are left as they are.
function Add-BridgeToIni($ini, $bridge) {
    $enc = [Text.Encoding]::Default
    $lines = [Collections.Generic.List[string]]([IO.File]::ReadAllLines($ini, $enc))
    $tdlLine = "TDL=$bridge"
    $find = { param($pattern) @(0..($lines.Count - 1) | Where-Object { $lines[$_] -match $pattern }) }

    $existing = & $find '^\s*TDL\s*=.*TallyMCPBridge\.tdl\s*$'
    $userTdl = & $find '^\s*User TDL\s*='
    if ($existing.Count -and $lines[$existing[0]].Trim() -eq $tdlLine -and
        $userTdl.Count -and $lines[$userTdl[0]] -match '=\s*Yes\s*$') {
        Write-Ok "Already loaded in $ini"
        return
    }

    Backup $ini
    if ($existing.Count) {
        $lines[$existing[0]] = $tdlLine
    } else {
        $last = & $find '^\s*(User )?TDL\s*=' | Select-Object -Last 1
        if ($null -ne $last) { $lines.Insert($last + 1, $tdlLine) } else { $lines.Add($tdlLine) }
    }
    $userTdl = & $find '^\s*User TDL\s*='
    if ($userTdl.Count) { $lines[$userTdl[0]] = 'User TDL=Yes' }
    else { $lines.Insert($lines.IndexOf($tdlLine), 'User TDL=Yes') }
    Save-Text $ini (($lines -join "`r`n") + "`r`n") $enc
    Write-Ok "Added the bridge to $ini"
}

# Adds or replaces the "tally" server in a Claude Desktop config, keeping
# everything else in it.
function Set-DesktopConfig($path, $exe, $envVars) {
    $cfg = New-Object PSObject
    if (Test-Path $path) {
        $raw = [IO.File]::ReadAllText($path)
        if ($raw.Trim()) {
            try { $cfg = $raw | ConvertFrom-Json }
            catch { Write-Note "$path is not valid JSON; left alone."; return $false }
        }
        Backup $path
    }
    if (-not $cfg.PSObject.Properties['mcpServers']) { $cfg | Add-Member mcpServers (New-Object PSObject) }
    $entry = [pscustomobject]@{ command = $exe; args = @(); env = [pscustomobject]$envVars }
    $cfg.mcpServers | Add-Member -Force tally $entry
    Save-Text $path ($cfg | ConvertTo-Json -Depth 32) (New-Object Text.UTF8Encoding $false)
    Write-Ok "Claude Desktop: $path (restart Claude Desktop to load it)"
    return $true
}

function Find-TallyIni {
    $dirs = @()
    $dirs += Get-Process tally -ErrorAction SilentlyContinue | Where-Object Path | ForEach-Object { Split-Path $_.Path }
    foreach ($root in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, 'C:\')) {
        if ($root) { $dirs += Get-ChildItem $root -Directory -ErrorAction SilentlyContinue | ForEach-Object FullName }
    }
    $found = @($dirs | Select-Object -Unique |
        Where-Object { (Test-Path (Join-Path $_ 'tally.exe')) -and (Test-Path (Join-Path $_ 'tally.ini')) } |
        ForEach-Object { Join-Path $_ 'tally.ini' })
    if ($found.Count -eq 1) { return $found[0] }
    if ($found.Count -gt 1) {
        for ($i = 0; $i -lt $found.Count; $i++) { Write-Host "     $($i + 1)) $($found[$i])" }
        return $found[[int](Read-Host '   More than one TallyPrime found. Which one') - 1]
    }
    return (Read-Host '   TallyPrime not found. Full path of its tally.ini (Enter to skip)').Trim('" ')
}

function Main {
    Write-Host 'tally-mcp setup' -ForegroundColor White
    New-Item -ItemType Directory -Force (Join-Path $McpDir 'queue') | Out-Null

    # --- 1. Write permission ---------------------------------------------------
    Write-Step 'Write tools'
    if (-not $WriteCompanies) {
        Write-Host '   Write tools let the AI change vouchers in Tally (every change is logged).'
        Write-Host '     1) Off - read only (recommended to start)'
        Write-Host '     2) Only the companies I name'
        Write-Host '     3) Any open company'
        switch (Read-Host '   Choose 1, 2 or 3 [1]') {
            '2'     { $script:WriteCompanies = (Read-Host '   Company names exactly as in Tally, separated by ;').Trim() }
            '3'     { $script:WriteCompanies = '*' }
            default { $script:WriteCompanies = 'none' }
        }
        if (-not $WriteCompanies) { $script:WriteCompanies = 'none' }
    }
    $writes = $WriteCompanies.Trim() -ne 'none'
    if ($writes) { Write-Ok "Writes allowed for: $WriteCompanies" } else { Write-Ok 'Read only' }

    # --- 2. uv (fetches Python and the server) ---------------------------------
    Write-Step 'Installing uv'
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $uv) {
        $code = Invoke-Native { powershell -NoProfile -ExecutionPolicy Bypass -Command 'irm https://astral.sh/uv/install.ps1 | iex' }
        if ($code -ne 0) { throw 'Installing uv failed (see above).' }
        $uv = Get-Command uv -ErrorAction SilentlyContinue
        if (-not $uv) { throw 'uv was installed but cannot be found. Open a new window and run this again.' }
    }
    $uv = $uv.Source
    Write-Ok "uv: $uv"

    # --- 3. The server ----------------------------------------------------------
    Write-Step 'Installing the tally-mcp server (latest version)'
    $code = Invoke-Native { & $uv tool install --force --reinstall $Package }
    if ($code -ne 0) { throw 'Installing tally-mcp failed (see above).' }
    $exe = Join-Path (& $uv tool dir --bin).Trim() 'tally-mcp.exe'
    if (-not (Test-Path $exe)) { throw "Installed, but $exe is missing." }
    Write-Ok "Server: $exe"

    # --- 4. Bridge TDL in tally.ini (write tools only) -------------------------
    $tallyExe = $null
    if ($writes) {
        Write-Step 'Loading the bridge TDL in TallyPrime'
        Invoke-WebRequest -UseBasicParsing $BridgeUrl -OutFile $BridgePath
        Write-Ok "Bridge TDL: $BridgePath"

        $ini = if ($TallyIni) { $TallyIni } else { Find-TallyIni }
        if ($ini -and (Test-Path $ini)) {
            $tallyExe = Join-Path (Split-Path $ini) 'tally.exe'
            while (Get-Process tally -ErrorAction SilentlyContinue) {
                Read-Host '   Close TallyPrime (it rewrites tally.ini when it exits), then press Enter' | Out-Null
            }
            Add-BridgeToIni $ini $BridgePath
        } else {
            Write-Note 'Skipped. Add the bridge yourself: F1 > TDLs & Add-ons > Manage Local TDLs,'
            Write-Note "add $BridgePath, then restart TallyPrime."
        }
    }

    # --- 5. MCP clients ---------------------------------------------------------
    Write-Step 'Connecting Claude'
    $envVars = [ordered]@{ TALLY_URL = $TallyUrl; TALLY_MCP_WRITE_COMPANIES = $WriteCompanies.Trim() }
    $configured = 0

    $desktopConfigs = @()
    if (Test-Path "$env:APPDATA\Claude") { $desktopConfigs += "$env:APPDATA\Claude\claude_desktop_config.json" }
    Get-ChildItem "$env:LOCALAPPDATA\Packages" -Directory -Filter 'Claude_*' -ErrorAction SilentlyContinue | ForEach-Object {
        $d = Join-Path $_.FullName 'LocalCache\Roaming\Claude'
        if (Test-Path $d) { $desktopConfigs += Join-Path $d 'claude_desktop_config.json' }
    }
    foreach ($path in $desktopConfigs) {
        if (Set-DesktopConfig $path $exe $envVars) { $configured++ }
    }

    if (Get-Command claude -ErrorAction SilentlyContinue) {
        Invoke-Native { claude mcp remove tally --scope user } | Out-Null
        $code = Invoke-Native {
            claude mcp add tally --scope user -e "TALLY_URL=$TallyUrl" -e "TALLY_MCP_WRITE_COMPANIES=$($WriteCompanies.Trim())" -- $exe
        }
        if ($code -eq 0) { Write-Ok 'Claude Code: added for all your projects'; $configured++ }
        else { Write-Note 'Claude Code: adding the server failed (see above).' }
    }

    if (-not $configured) {
        Write-Note 'Neither Claude Desktop nor Claude Code was found. In your MCP client, run:'
        Write-Note "  $exe"
        Write-Note "  with TALLY_URL=$TallyUrl and TALLY_MCP_WRITE_COMPANIES=$($WriteCompanies.Trim())"
    }

    # --- 6. Check Tally ---------------------------------------------------------
    Write-Step 'Checking TallyPrime'
    if (-not (Get-Process tally -ErrorAction SilentlyContinue) -and $tallyExe -and (Test-Path $tallyExe)) {
        if ((Read-Host '   Start TallyPrime now? [Y/n]') -notmatch '^[nN]') {
            Start-Process $tallyExe -WorkingDirectory (Split-Path $tallyExe)
            Write-Host '   Waiting for TallyPrime (open a company if it asks)...'
            for ($i = 0; $i -lt 60 -and -not (Test-TallyPort); $i++) { Start-Sleep 2 }
        }
    }
    if (Test-TallyPort) {
        Write-Ok "TallyPrime is answering on $TallyUrl"
    } else {
        if (Get-Process tally -ErrorAction SilentlyContinue) {
            Write-Note 'TallyPrime is running but not answering on port 9000. Turn its server on:'
        } else {
            Write-Note 'TallyPrime is not running. When you start it, make sure its server is on:'
        }
        Write-Note '  F1 (Help) > Settings > Connectivity > Client/Server configuration'
        Write-Note '  TallyPrime acts as: Both    Port: 9000    (restart TallyPrime after changing it)'
    }

    Write-Host "`nDone. In Claude, ask: `"check Tally status`"." -ForegroundColor Green
    if ($writes) { Write-Host "Every write is logged in $McpDir\audit.jsonl." }
}

if ($MyInvocation.InvocationName -ne '.') { Main }
