# rotate_logs.ps1 -- rotate oversized *.log files under ~/.claude/.
# For every *.log file directly under $env:USERPROFILE\.claude that exceeds the
# SIZE_LIMIT threshold, rename it to <name>.log.1 (overwriting any existing .log.1).
# Called near the start of dream_cron.ps1 so logs never grow unboundedly.
# PS 5.1 safe, ASCII-only comments, no Write-Host (runs headless).

$SIZE_LIMIT = 2MB   # rotate once the file exceeds 2 megabytes

$logDir = Join-Path $env:USERPROFILE '.claude'
if (-not (Test-Path $logDir)) { return }

Get-ChildItem -Path $logDir -Filter '*.log' -File | Where-Object { $_.Length -gt $SIZE_LIMIT } | ForEach-Object {
    $rotated = $_.FullName + '.1'
    Move-Item -Path $_.FullName -Destination $rotated -Force
}
