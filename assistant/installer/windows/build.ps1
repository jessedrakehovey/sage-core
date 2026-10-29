# build.ps1 - one command: the engine release (assistant/release.py) + SageSetup.exe (Inno Setup) into dist\ (sage-core, 2026-10-28).
#   powershell -ExecutionPolicy Bypass -File assistant\installer\windows\build.ps1 [-ReleaseUrl https://.../releases/latest/download] [-Out <dist folder>] [-NoExe]
# Needs Inno Setup 6 for the .exe (user install, no admin): winget install JRSoftware.InnoSetup --scope user
# -ReleaseUrl is written into release.json and into the installer, so the organizations it installs know where to look for
# updates (org.json engine_release_url). Default: this checkout's GitHub origin as `.../releases/latest/download` (no account
# name lives in the engine itself); with neither, organizations update by hand (`sage update --engine <folder>`).
param(
    [string]$ReleaseUrl = "",
    [string]$Out = "",
    [string]$Python = "python",
    [switch]$NoExe
)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = [IO.Path]::GetFullPath((Join-Path $here '..\..\..'))
if (-not $Out) { $Out = Join-Path $repo 'dist' }
if (-not $ReleaseUrl) {
    $origin = (git -C $repo remote get-url origin 2>$null)
    if ($origin -match 'github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$') { $ReleaseUrl = "https://github.com/$($Matches[1])/$($Matches[2])/releases/latest/download" }
}
Write-Host "[build] release feed: $(if ($ReleaseUrl) { $ReleaseUrl } else { '(none: installs update by hand)' })"
New-Item -ItemType Directory -Force -Path $Out | Out-Null
Write-Host "[build] release from $repo"
$args = @((Join-Path $repo 'assistant\release.py'), 'build', '--out', $Out)
if ($ReleaseUrl) { $args += @('--url', $ReleaseUrl) }
& $Python @args
if ($LASTEXITCODE -ne 0) { throw "release.py failed" }
$rel = Get-Content (Join-Path $Out 'release.json') -Raw -Encoding UTF8 | ConvertFrom-Json
Write-Host "[build] $($rel.name) $($rel.version): $($rel.file) ($([math]::Round($rel.size / 1MB, 1)) MB, sha256 $($rel.sha256.Substring(0, 12))...)"
if ($NoExe) { exit 0 }
$iscc = @((Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'), (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'), (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe')) |
    Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $iscc) { $cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue; if ($cmd) { $iscc = $cmd.Source } }
if (-not $iscc) { Write-Host "[build] Inno Setup not found: winget install JRSoftware.InnoSetup --scope user   (then re-run)"; exit 2 }
Write-Host "[build] $iscc"
& $iscc /Qp "/DVersion=$($rel.version)" "/DZip=$($rel.file)" "/DDist=$Out" "/DReleaseUrl=$ReleaseUrl" (Join-Path $here 'SageSetup.iss')
if ($LASTEXITCODE -ne 0) { throw "ISCC failed ($LASTEXITCODE)" }
$exe = Join-Path $Out 'SageSetup.exe'
Write-Host "[build] $exe ($([math]::Round((Get-Item $exe).Length / 1MB, 1)) MB)"
