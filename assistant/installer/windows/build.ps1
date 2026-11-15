# build.ps1 - one command: the engine release (assistant/release.py) + SageSetup.exe (Inno Setup) into dist\ (sage-core, 2026-10-28).
#   powershell -ExecutionPolicy Bypass -File assistant\installer\windows\build.ps1 [-ReleaseUrl https://.../releases/latest/download] [-Out <dist folder>] [-NoExe] [-Bundle]
# Needs Inno Setup 6 for the .exe (user install, no admin): winget install JRSoftware.InnoSetup --scope user
# -ReleaseUrl is written into release.json and into the installer, so the organizations it installs know where to look for
# updates (org.json engine_release_url). Default: this checkout's GitHub origin as `.../releases/latest/download` (no account
# name lives in the engine itself); with neither, organizations update by hand (`sage update --engine <folder>`).
# -Bundle (2026-11-14): the installer carries Python, pip, every package and the browser (dist\bundle\), so an install
# downloads none of them; the Claude Code CLI alone still comes from its maker. The bundle is proven before it ships: this
# script installs from it into a scratch folder with the package index switched off, and the browser in it is fetched by
# that very install, so it is the build the bundled Playwright expects. Costs about 200 MB of downloads the first time.
param(
    [string]$ReleaseUrl = "",
    [string]$Out = "",
    [string]$Python = "python",
    [switch]$NoExe,
    [switch]$Bundle
)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = [IO.Path]::GetFullPath((Join-Path $here '..\..\..'))
if (-not $Out) { $Out = Join-Path $repo 'dist' }
if (-not $ReleaseUrl) {
    # through cmd: under 'Stop', Windows PowerShell 5.1 turns git's "No such remote" line into a terminating error
    $origin = (cmd /c "git -C `"$repo`" remote get-url origin 2>nul")
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
$bundleDir = ''
if ($Bundle) {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $bundleDir = Join-Path $Out 'bundle'
    $wheels = Join-Path $bundleDir 'wheels'
    New-Item -ItemType Directory -Force -Path $wheels | Out-Null
    # the Python pin lives in install.ps1 alone; read it from there
    $inst = Get-Content (Join-Path $here 'install.ps1') -Raw
    $pyVer = [regex]::Match($inst, "\`$PyVersion = '([^']+)'").Groups[1].Value
    $pySha = [regex]::Match($inst, "\`$PySha = '([0-9a-f]{64})'").Groups[1].Value
    if (-not $pyVer -or -not $pySha) { throw "could not read the Python pin from install.ps1" }
    $pyZip = Join-Path $bundleDir "python-$pyVer-embed-amd64.zip"
    if (-not (Test-Path $pyZip) -or (Get-FileHash -Algorithm SHA256 $pyZip).Hash.ToLower() -ne $pySha) {
        Write-Host "[build] bundle: Python $pyVer"
        Invoke-WebRequest -Uri "https://www.python.org/ftp/python/$pyVer/python-$pyVer-embed-amd64.zip" -OutFile $pyZip -UseBasicParsing
    }
    if ((Get-FileHash -Algorithm SHA256 $pyZip).Hash.ToLower() -ne $pySha) { throw "the Python download does not match the checksum pinned in install.ps1" }
    $gp = Join-Path $bundleDir 'get-pip.py'
    if (-not (Test-Path $gp)) { Write-Host "[build] bundle: get-pip.py"; Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile $gp -UseBasicParsing }
    # pip's own wheels first (pure Python, any interpreter can fetch them), so the bundled Python gets pip with no index
    & $Python -m pip download --quiet --disable-pip-version-check -d $wheels --only-binary=:all: pip setuptools wheel
    if ($LASTEXITCODE -ne 0) { throw "pip download (pip, setuptools, wheel) failed" }
    $scratch = Join-Path ([IO.Path]::GetTempPath()) ("sage-bundle-" + [Guid]::NewGuid().ToString('N').Substring(0, 8))
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $here 'install.ps1') -Root $scratch -Bundle $bundleDir -PythonOnly
    $spy = Join-Path $scratch 'python\python.exe'
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $spy)) { throw "the bundled Python did not unpack" }
    # the packages are picked BY the bundled Python: a different interpreter (this machine's) reads the "only on Python
    # older than 3.12" conditions for itself and leaves out wheels the bundled one then asks for (2026-11-14)
    Write-Host "[build] bundle: the packages, as wheels picked by the bundled Python $pyVer"
    & $spy -m pip download --quiet --disable-pip-version-check -d $wheels --only-binary=:all: -r (Join-Path $repo 'assistant\requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw "pip download failed (a package without a Windows wheel?)" }
    # prove it: a scratch install from the bundle alone (pip runs with --no-index there), then that install fetches its browser
    Write-Host "[build] bundle: proving an install from it in $scratch"
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $here 'install.ps1') -Root $scratch -Bundle $bundleDir -EngineZip (Join-Path $Out $rel.file) `
        -ReleaseJson (Join-Path $Out 'release.json') -SkipClaude -SkipBrowser -NoLaunch
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path (Join-Path $scratch 'install.json'))) { throw "an install from the bundle failed" }
    $browsers = Join-Path $bundleDir 'ms-playwright'
    $old = $env:PLAYWRIGHT_BROWSERS_PATH
    $env:PLAYWRIGHT_BROWSERS_PATH = $browsers
    Write-Host "[build] bundle: the browser the bundled Playwright expects"
    & $spy -m playwright install chromium --no-shell
    if ($LASTEXITCODE -ne 0) { & $spy -m playwright install chromium }
    $code = $LASTEXITCODE
    $env:PLAYWRIGHT_BROWSERS_PATH = $old
    Remove-Item $scratch -Recurse -Force -ErrorAction SilentlyContinue
    if ($code -ne 0 -or -not (Get-ChildItem -Path $browsers -Directory -Filter 'chromium-*' -ErrorAction SilentlyContinue)) { throw "the browser did not download into the bundle" }
    # Playwright's own bookkeeping names the scratch folder, which is gone: it does not ship
    Remove-Item (Join-Path $browsers '.links') -Recurse -Force -ErrorAction SilentlyContinue
    # a newer browser replaced an older one: keep only what the bundled Playwright just asked for
    $keep = Get-ChildItem -Path $browsers -Directory | Sort-Object LastWriteTime -Descending | Group-Object { $_.Name -replace '-\d+$', '' } | ForEach-Object { $_.Group[0].FullName }
    Get-ChildItem -Path $browsers -Directory | Where-Object { $keep -notcontains $_.FullName } | Remove-Item -Recurse -Force
    $mb = [math]::Round(((Get-ChildItem -Path $bundleDir -Recurse -File | Measure-Object Length -Sum).Sum) / 1MB)
    Write-Host "[build] bundle: $bundleDir ($mb MB before compression)"
}
if ($NoExe) { exit 0 }
$iscc = @((Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'), (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'), (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe')) |
    Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $iscc) { $cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue; if ($cmd) { $iscc = $cmd.Source } }
if (-not $iscc) { Write-Host "[build] Inno Setup not found: winget install JRSoftware.InnoSetup --scope user   (then re-run)"; exit 2 }
Write-Host "[build] $iscc"
$isccArgs = @('/Qp', "/DVersion=$($rel.version)", "/DZip=$($rel.file)", "/DDist=$Out", "/DReleaseUrl=$ReleaseUrl")
if ($bundleDir) { $isccArgs += "/DBundle=$bundleDir" }
& $iscc @isccArgs (Join-Path $here 'SageSetup.iss')
if ($LASTEXITCODE -ne 0) { throw "ISCC failed ($LASTEXITCODE)" }
$exe = Join-Path $Out 'SageSetup.exe'
Write-Host "[build] $exe ($([math]::Round((Get-Item $exe).Length / 1MB, 1)) MB)"
