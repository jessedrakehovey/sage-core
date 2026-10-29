# bootstrap.ps1 - a Sage agent from a bare Windows machine (sage-setup skill). Idempotent: re-run until doctor is clean.
#   powershell -ExecutionPolicy Bypass -File bootstrap.ps1 -Org "Acme Bakery" -Agent Pip -Owner "Ana Ruiz:ana@acme.test" -Engine <folder|git url> -Out "C:\Users\me\Documents\Acme OS"
# Installs (winget) what is missing: Git, Python 3.11, Node LTS, Google Cloud CLI; then the Claude Code CLI (npm),
# a venv with assistant/requirements.txt, the Playwright Chromium browser, the engine, the pack. Never touches tokens:
# the owner mints the Claude token themselves (SKILL.md step 2).
param(
    [Parameter(Mandatory = $true)][string]$Org,
    [Parameter(Mandatory = $true)][string]$Agent,     # the owner names their agent; no default
    [Parameter(Mandatory = $true)][string[]]$Owner,
    [Parameter(Mandatory = $true)][string]$Engine,
    [string]$Out = "",
    [string]$What = "",
    [switch]$SkipInstalls
)
$ErrorActionPreference = 'Stop'
function Say($m) { Write-Host ("[sage-setup] " + $m) }
function Have($cmd) { return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
function Winget($id, $name) {
    if (-not (Have winget)) { Say "winget missing: install $name by hand, then re-run"; return }
    Say "installing $name ..."
    winget install --id $id -e --silent --accept-package-agreements --accept-source-agreements | Out-Null
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
}

$slug = ($Org.ToLower() -replace '[^a-z0-9]+', '-').Trim('-')
if (-not $Out) { $Out = Join-Path ([Environment]::GetFolderPath('MyDocuments')) "$Org OS" }

if (-not $SkipInstalls) {
    if (-not (Have git)) { Winget 'Git.Git' 'Git' }
    if (-not (Have python)) { Winget 'Python.Python.3.11' 'Python 3.11' }
    if (-not (Have node)) { Winget 'OpenJS.NodeJS.LTS' 'Node LTS' }
    if (-not (Have gcloud)) { Winget 'Google.CloudSDK' 'Google Cloud CLI' }
    if (-not (Have claude)) { Say "installing the Claude Code CLI ..."; npm install -g @anthropic-ai/claude-code | Out-Null }
}
foreach ($c in @('git', 'python', 'node', 'npm')) { if (-not (Have $c)) { throw "$c is still not on PATH; open a new terminal and re-run" } }

# the engine: a folder (copied as-is) or a git URL (cloned) into ~/sage-core
$engineDir = $Engine
if ($Engine -match '^(https?://|git@)') {
    $engineDir = Join-Path $HOME 'sage-core'
    if (Test-Path (Join-Path $engineDir 'engine_manifest.json')) { Say "engine present at $engineDir (git pull)"; git -C $engineDir pull --ff-only | Out-Null }
    else { Say "cloning the engine to $engineDir"; git clone --depth 1 $Engine $engineDir | Out-Null }
}
if (-not (Test-Path (Join-Path $engineDir 'assistant\new_org.py'))) { throw "no engine at $engineDir (assistant/new_org.py missing)" }

# the venv the jobs run in
$venv = Join-Path $HOME ".venvs\$slug-sage"
$py = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path $py)) { Say "creating venv $venv"; python -m venv $venv }
Say "installing Python packages"
& $py -m pip install --quiet --upgrade pip
& $py -m pip install --quiet -r (Join-Path $engineDir 'assistant\requirements.txt')
Say "installing the Playwright Chromium browser (one-time download)"
& $py -m playwright install chromium | Out-Null

# the pack
if (-not (Test-Path (Join-Path $Out 'assistant\org.json'))) {
    $args = @((Join-Path $engineDir 'assistant\new_org.py'), $Org, '--agent', $Agent, '--out', $Out)
    foreach ($o in $Owner) { $args += @('--owner', $o) }
    if ($What) { $args += @('--what', $What) }
    Say "creating the pack at $Out"
    & $py @args
} else { Say "pack already at $Out" }

# the pack knows its interpreter
$orgJson = Join-Path $Out 'assistant\org.json'
$org = Get-Content $orgJson -Raw -Encoding UTF8 | ConvertFrom-Json
$org.python = $py
$org | ConvertTo-Json -Depth 8 | Out-File -FilePath $orgJson -Encoding utf8

Say "doctor:"
& $py (Join-Path $Out 'assistant\sage.py') doctor --no-probe --fix
Say "next: SKILL.md step 2 (the owner's Claude token, in a fresh terminal), then `"$py`" `"$Out\assistant\sage.py`" serve"
