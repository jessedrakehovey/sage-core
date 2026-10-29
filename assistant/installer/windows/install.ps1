# install.ps1 - everything a Sage agent needs on a Windows PC, with no terminal knowledge and no git (sage-core, 2026-10-28).
# Run by SageSetup.exe (Inno Setup, SageSetup.iss) after it has copied the engine release next to this file; also runs by hand:
#   powershell -ExecutionPolicy Bypass -File install.ps1 [-Root "$env:LOCALAPPDATA\Sage"] [-EngineZip <zip>] [-ReleaseJson <json>] [-ReleaseUrl <url>]
#                                                        [-SkipClaude] [-SkipBrowser] [-NoLaunch]
# What it puts under -Root (per user, no admin):
#   python\     the python.org embeddable Python 3.11 + pip + assistant/requirements.txt (private to Sage; the system's Python is never touched)
#   engine\     the engine release (engine_manifest.json, assistant/, scripts/, Schema/, .claude/)
#   install.json  where things are (python, engine, release URL, version), read by assistant/welcome.py
# plus the Playwright Chromium browser (%LOCALAPPDATA%\ms-playwright, shared) and the Claude Code CLI by its native installer
# (%USERPROFILE%\.local\bin\claude.exe; skipped when a `claude` is already on this machine). The organization's own folder is
# made afterwards by the setup page (welcome.py), never here; this script touches no organization data and stores no token.
# Idempotent: every step is skipped when its result is already there; re-run it until it ends with "ready".
param(
    [string]$Root = "",
    [string]$EngineZip = "",
    [string]$ReleaseJson = "",
    [string]$ReleaseUrl = "",
    [switch]$SkipClaude,
    [switch]$SkipBrowser,
    [switch]$NoLaunch
)
# Windows PowerShell 5.1: under 'Stop' a native program's stderr line (pip's progress, a Python traceback we probe for) becomes a
# terminating error, so native calls run under 'Continue' and are judged by their exit code; cmdlets ask for -ErrorAction Stop.
$ErrorActionPreference = 'Continue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$PyVersion = '3.11.9'
$PyUrl = "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-embed-amd64.zip"
$PySha = '009d6bf7e3b2ddca3d784fa09f90fe54336d5b60f0e0f305c37f400bf83cfd3b'      # python.org, pinned 2026-10-28
$GetPip = 'https://bootstrap.pypa.io/get-pip.py'
$ClaudeInstaller = 'https://claude.ai/install.ps1'

function Say($m) { Write-Host ("[sage] " + $m) }
function Sha256($path) { (Get-FileHash -Algorithm SHA256 -Path $path -ErrorAction Stop).Hash.ToLower() }
function PyOk($code) { cmd /c "`"$py`" -c `"$code`" >nul 2>&1"; return ($LASTEXITCODE -eq 0) }   # a quiet probe: no traceback on screen
function Download($url, $dst) {
    Say "downloading $url"
    $tmp = "$dst.part"
    if (Test-Path $tmp) { Remove-Item $tmp -Force }
    Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing -ErrorAction Stop
    Move-Item $tmp $dst -Force -ErrorAction Stop
}

if (-not $Root) { $Root = Join-Path $env:LOCALAPPDATA 'Sage' }
$Root = [IO.Path]::GetFullPath($Root)
New-Item -ItemType Directory -Force -Path $Root -ErrorAction Stop | Out-Null
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $EngineZip) { $EngineZip = (Get-ChildItem -Path $here -Filter 'sage-core-*.zip' -ErrorAction SilentlyContinue | Select-Object -First 1).FullName }
if (-not $ReleaseJson -and $EngineZip) { $cand = Join-Path (Split-Path -Parent $EngineZip) 'release.json'; if (Test-Path $cand) { $ReleaseJson = $cand } }
Say "root $Root"

# ---- 1. Python (embeddable, private) ----------------------------------------------------------------------------
$pyDir = Join-Path $Root 'python'
$py = Join-Path $pyDir 'python.exe'
if (-not (Test-Path $py)) {
    $zip = Join-Path $Root "python-$PyVersion-embed-amd64.zip"
    if (-not (Test-Path $zip) -or (Sha256 $zip) -ne $PySha) { Download $PyUrl $zip }
    if ((Sha256 $zip) -ne $PySha) { throw "the Python download does not match its published checksum; stopping" }
    Say "unpacking Python $PyVersion"
    if (Test-Path $pyDir) { Remove-Item $pyDir -Recurse -Force }
    Expand-Archive -Path $zip -DestinationPath $pyDir -Force -ErrorAction Stop
    Remove-Item $zip -Force
}
# the embeddable build ignores site-packages until its ._pth says so (the standard recipe: list the folder, import site)
$pth = Get-ChildItem -Path $pyDir -Filter 'python*._pth' | Select-Object -First 1
if ($pth) {
    $lines = Get-Content $pth.FullName
    $want = @('python311.zip', '.', 'Lib\site-packages', 'import site')
    $changed = $false
    foreach ($w in $want) { if (-not ($lines -contains $w)) { $lines = @($lines | Where-Object { $_ -ne "#import site" }) + $w; $changed = $true } }
    if ($changed) { Set-Content -Path $pth.FullName -Value $lines -Encoding Ascii; Say "site-packages enabled in $($pth.Name)" }
}
if (-not (PyOk "import pip")) {
    $gp = Join-Path $Root 'get-pip.py'
    Download $GetPip $gp
    Say "installing pip"
    & $py $gp --no-warn-script-location --quiet
    if ($LASTEXITCODE -ne 0) { throw "pip did not install" }
    Remove-Item $gp -Force
}
Say ("python: " + (& $py --version))

# ---- 2. The engine release ---------------------------------------------------------------------------------------
$engineDir = Join-Path $Root 'engine'
$manifest = Join-Path $engineDir 'engine_manifest.json'
$rel = $null
if ($ReleaseJson -and (Test-Path $ReleaseJson)) { $rel = Get-Content $ReleaseJson -Raw -Encoding UTF8 | ConvertFrom-Json }
if (-not $EngineZip -and $ReleaseUrl) {
    $relUrl = if ($ReleaseUrl -match '\.json$') { $ReleaseUrl } else { $ReleaseUrl.TrimEnd('/') + '/release.json' }
    $ReleaseJson = Join-Path $Root 'release.json'
    Download $relUrl $ReleaseJson
    $rel = Get-Content $ReleaseJson -Raw -Encoding UTF8 | ConvertFrom-Json
    $zipUrl = if ($rel.url) { $rel.url } else { ($relUrl -replace 'release\.json$', '') + $rel.file }
    $EngineZip = Join-Path $Root $rel.file
    Download $zipUrl $EngineZip
}
if (-not $EngineZip -or -not (Test-Path $EngineZip)) { throw "no engine release: pass -EngineZip <sage-core-*.zip> or -ReleaseUrl <feed>" }
if ($rel -and $rel.sha256 -and ((Sha256 $EngineZip) -ne $rel.sha256.ToLower())) { throw "the engine archive does not match release.json's sha256; stopping" }
$installedVersion = ''
if (Test-Path $manifest) { try { $installedVersion = (Get-Content $manifest -Raw -Encoding UTF8 | ConvertFrom-Json).engine.version } catch {} }
$relVersion = if ($rel) { $rel.version } else { '' }
if (-not (Test-Path $manifest) -or ($relVersion -and $relVersion -ne $installedVersion)) {
    Say ("unpacking the engine" + $(if ($relVersion) { " $relVersion" } else { '' }))
    if (Test-Path $engineDir) { Remove-Item $engineDir -Recurse -Force }
    Expand-Archive -Path $EngineZip -DestinationPath $engineDir -Force -ErrorAction Stop
} else { Say "engine $installedVersion already unpacked" }
if (-not (Test-Path (Join-Path $engineDir 'assistant\sage.py'))) { throw "the archive is not a sage-core release (assistant/sage.py missing)" }

# ---- 3. Python packages + the browser ---------------------------------------------------------------------------
$req = Join-Path $engineDir 'assistant\requirements.txt'
if (-not (PyOk "import keyring, playwright, pykeepass, google.auth, requests, cryptography")) {
    Say "installing the Python packages (a minute or two)"
    & $py -m pip install --quiet --no-warn-script-location --disable-pip-version-check -r $req
    if ($LASTEXITCODE -ne 0) { throw "pip install -r requirements.txt failed" }
} else { Say "python packages present" }
if (-not $SkipBrowser) {
    Say "installing the Playwright Chromium browser (one-time download; skipped when present)"
    & $py -m playwright install chromium
    if ($LASTEXITCODE -ne 0) { Say "WARN: the browser did not install; the setup page's checks will say so (re-run later)" }
}

# ---- 4. The Claude Code CLI (native build; no Node) ----------------------------------------------------------------
function HaveClaude {
    if (Get-Command claude -ErrorAction SilentlyContinue) { return $true }
    foreach ($c in @((Join-Path $env:USERPROFILE '.local\bin\claude.exe'), (Join-Path $env:APPDATA 'npm\claude.cmd'))) { if (Test-Path $c) { return $true } }
    return $false
}
if (-not $SkipClaude) {
    if (HaveClaude) { Say "Claude Code CLI present" }
    else {
        Say "installing the Claude Code CLI (its native installer)"
        try { powershell -NoProfile -ExecutionPolicy Bypass -Command "irm $ClaudeInstaller | iex" } catch { Say "WARN: the Claude CLI installer failed: $($_.Exception.Message)" }
        if (-not (HaveClaude)) { Say "WARN: the Claude CLI is still missing; the setup page's checks carry the fix line" }
    }
}

# ---- 5. Where things are ----------------------------------------------------------------------------------------
$info = [ordered]@{ python = $py; engine = $engineDir; release_url = $ReleaseUrl; version = $relVersion; installed = (Get-Date -Format 'yyyy-MM-ddTHH:mm:ss'); installer = 'SageSetup' }
$info | ConvertTo-Json | Set-Content -Path (Join-Path $Root 'install.json') -Encoding UTF8 -ErrorAction Stop
Say "ready: $Root"
if (-not $NoLaunch) {
    $pyw = Join-Path $pyDir 'pythonw.exe'
    $env:SAGE_HOME = $Root
    Start-Process -FilePath $(if (Test-Path $pyw) { $pyw } else { $py }) -ArgumentList @("`"$engineDir\assistant\welcome.py`"", '--open') -WorkingDirectory $engineDir
    Say "the setup page is opening in your browser"
}
