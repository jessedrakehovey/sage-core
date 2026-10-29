# release.ps1 - publish a release of the engine to GitHub Releases, one command (sage-core, 2026-10-28).
#   powershell -ExecutionPolicy Bypass -File assistant\installer\windows\release.ps1 [-Repo owner/name] [-Notes "..."] [-NoExe] [-Draft]
# Run from an engine checkout (sage-core) with the GitHub CLI signed in (`gh auth status`). It builds the release (build.ps1:
# dist\sage-core-<version>.zip + release.json + .sha256, and SageSetup.exe when Inno Setup is installed), tags the checkout
# `v<version>` (the + of the version becomes -) and uploads the files. Installed organizations find the newest release at
# `releases/latest/download/release.json` (assistant/updater.py), so nothing on their side changes between releases.
param(
    [string]$Repo = "",
    [string]$Notes = "",
    [switch]$NoExe,
    [switch]$Draft
)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoDir = [IO.Path]::GetFullPath((Join-Path $here '..\..\..'))
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { throw "the GitHub CLI (gh) is not installed: https://cli.github.com" }
if (-not $Repo) {
    $origin = (git -C $repoDir remote get-url origin 2>$null)
    if ($origin -match 'github\.com[:/]([^/]+/[^/.]+)') { $Repo = $Matches[1] } else { throw "pass -Repo owner/name (no GitHub origin on this checkout)" }
}
$url = "https://github.com/$Repo/releases/latest/download"
$out = Join-Path $repoDir 'dist'
$buildArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $here 'build.ps1'), '-ReleaseUrl', $url, '-Out', $out)
if ($NoExe) { $buildArgs += '-NoExe' }
& powershell @buildArgs
if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne 2) { throw "build failed ($LASTEXITCODE)" }    # 2 = no Inno Setup: the release still ships without the .exe
$rel = Get-Content (Join-Path $out 'release.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$tag = 'v' + ($rel.version -replace '\+', '-')
$files = @((Join-Path $out $rel.file), (Join-Path $out 'release.json'), (Join-Path $out ($rel.file + '.sha256')))
$exe = Join-Path $out 'SageSetup.exe'
if (Test-Path $exe) { $files += $exe }
if (-not $Notes) { $Notes = ($rel.notes | ForEach-Object { "- $_" }) -join "`n"; if (-not $Notes) { $Notes = "sage-core $($rel.version)" } }
$notesFile = Join-Path $out 'RELEASE_NOTES.md'
Set-Content -Path $notesFile -Value ("sage-core $($rel.version)`n`n$Notes`n`nsha256 $($rel.sha256)  $($rel.file)") -Encoding UTF8
if (-not (git -C $repoDir tag -l $tag)) { git -C $repoDir tag $tag; git -C $repoDir push origin $tag | Out-Null }
$ghArgs = @('release', 'create', $tag) + $files + @('--repo', $Repo, '--title', "sage-core $($rel.version)", '--notes-file', $notesFile)
if ($Draft) { $ghArgs += '--draft' }
& gh @ghArgs
if ($LASTEXITCODE -ne 0) { throw "gh release create failed ($LASTEXITCODE)" }
Write-Host "[release] $tag published to https://github.com/$Repo/releases/tag/$tag"
Write-Host "[release] installed organizations update from $url/release.json on their next nightly run"
