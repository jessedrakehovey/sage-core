# run_claude.ps1 -- shared headless `claude -p` runner with transient-error retry.
#
# WHY: the headless crons (dream_cron) shell out to `claude -p`. OpenRouter (behind
# Cloudflare) intermittently RESETS the connection while waiting for the first upstream
# token, surfacing as "The socket connection was closed unexpectedly". Before this helper
# the crons had NO retry, so a single transient reset silently failed the whole run.
# The local or-proxy (127.0.0.1:8788) fixes the TERMINAL CLI, but desktop-launched /
# cold runs still see resets, so the crons need their own retry.
#
# SAFETY: retry is safe for these jobs. They are idempotent -- dreaming dedups against a
# "Last consolidated" date, and the poll crons run with explicit --allowedTools/
# --disallowedTools that make sending structurally impossible. A retry can at worst create
# a duplicate Gmail DRAFT (harmless, the owner reviews everything); it can never re-send or
# double-act. Retries fire ONLY on a transient signature -- never on a clean (exit 0)
# completion, so a legitimate "NO NEW LOGS" / "NO NEW APPROVALS" no-op is returned
# immediately.
#
# USAGE (dot-source from a cron, using an absolute path so it works under Task Scheduler):
#   . (Join-Path $wv 'scripts\lib\run_claude.ps1')
#   $r = Invoke-ClaudeHeadless -Claude $claude -Prompt $prompt `
#          -ClaudeArgs @('--allowedTools',$allowed,'--disallowedTools',$denied) `
#          -Logger ${function:Log}
#   if ($r.Output) { Log "OUTPUT:`n$($r.Output)" }
#   if (-not $r.Ok) { exit 1 }
#
# Returns a hashtable: @{ Output; ExitCode; Stderr; Attempts; Ok }.
#
# Env-var overrides (WV_* prefix -- never shares state with the property vault's BLP_* vars):
#   WV_CLAUDE_DAILY_MAX   -- max claude invocations per calendar day (0 = no cap)
#   WV_CLAUDE_MAX_TURNS   -- max agentic turns per invocation (0 = no cap)

function Test-TransientClaudeError {
    # True if a nonzero result looks like a retryable transient (network / overload / 5xx).
    param([string]$Text, [int]$ExitCode)
    if ($ExitCode -eq 0) {
        # `claude -p` can exit 0 after printing ONLY an API error: 2026-09-06 a
        # Travel Scout pass ended in 70s with "API Error: Connection closed
        # mid-response" as its entire output and no retry fired. A short output
        # that is nothing but an API error is a transient, whatever the exit code.
        return (("$Text").Trim().Length -lt 800 -and
                $Text -imatch 'API Error:.*(Connection closed|mid-response|closed unexpectedly|ECONNRESET|overloaded|Too Many Requests|\b5\d\d\b)')
    }
    if ([string]::IsNullOrWhiteSpace($Text)) { return $true }  # nonzero + no output == treat as transient
    $patterns = @(
        'socket connection was closed', 'socket hang ?up', 'Connection error',
        'ECONNRESET', 'ETIMEDOUT', 'EPIPE', 'ENOTFOUND', 'EAI_AGAIN',
        'fetch failed', 'network error', 'request timed out', 'timed ?out',
        'overloaded', 'rate.?limit', 'Too Many Requests', 'temporarily',
        '\b5\d\d\b', 'Internal Server Error', 'Bad Gateway', 'Service Unavailable', 'Gateway Time'
    )
    foreach ($p in $patterns) { if ($Text -imatch $p) { return $true } }
    return $false
}

# --- Daily-invocation budget cap ---------------------------------------------------
# Caps claude runs per calendar day to prevent runaway usage during agentic cron loops.
# Counter lives in LOCALAPPDATA under a wedding-specific filename (wv_claude_budget.json)
# so it NEVER shares state with the property vault (blp_claude_budget.json).
# Opt-in via -DailyMax parameter or WV_CLAUDE_DAILY_MAX env var.
function Get-ClaudeBudgetPath {
    $dir = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { $env:TEMP }
    return (Join-Path $dir 'wv_claude_budget.json')
}
function Get-ClaudeDailyCount {
    $today = (Get-Date).ToString('yyyy-MM-dd')
    $path  = Get-ClaudeBudgetPath
    if (Test-Path $path) {
        try { $s = Get-Content $path -Raw | ConvertFrom-Json; if ($s.date -eq $today) { return [int]$s.count } } catch {}
    }
    return 0
}
function Add-ClaudeDailySpend {
    param([int]$N = 1)
    $today = (Get-Date).ToString('yyyy-MM-dd')
    $path  = Get-ClaudeBudgetPath
    $count = (Get-ClaudeDailyCount) + $N
    @{ date = $today; count = $count } | ConvertTo-Json -Compress | Set-Content -Path $path -Encoding UTF8
    return $count
}

function Invoke-ClaudeHeadless {
    param(
        [Parameter(Mandatory)] [string]   $Claude,
        [Parameter(Mandatory)] [string]   $Prompt,
        [string[]]    $ClaudeArgs   = @(),
        [int]         $MaxAttempts  = 2,
        [int]         $BaseDelaySec = 10,
        # Per-run turn ceiling: injects `--max-turns` (bounds cost per invocation).
        [int]         $MaxTurns     = $(if ($env:WV_CLAUDE_MAX_TURNS -as [int]) { [int]$env:WV_CLAUDE_MAX_TURNS } else { 0 }),
        # Daily invocation cap: skip (clean no-op) once N claude runs have happened today.
        [int]         $DailyMax     = $(if ($env:WV_CLAUDE_DAILY_MAX -as [int]) { [int]$env:WV_CLAUDE_DAILY_MAX } else { 0 }),
        [scriptblock] $Logger       = $null
    )
    function _log($m) { if ($Logger) { & $Logger $m } }

    # Daily-budget gate (opt-in). A skipped run returns Ok=$true so the cron treats
    # it as a clean no-op (exit 0), never a failure that retries or alerts.
    if ($DailyMax -gt 0) {
        $count = Get-ClaudeDailyCount
        if ($count -ge $DailyMax) {
            _log "DAILY BUDGET REACHED ($count/$DailyMax runs today) - skipping; raise WV_CLAUDE_DAILY_MAX to override."
            return @{ Output = ''; ExitCode = 0; Stderr = ''; Attempts = 0; Ok = $true; Skipped = $true; BudgetCount = $count }
        }
        $count = Add-ClaudeDailySpend -N 1
        _log "daily budget: run $count/$DailyMax"
    }

    # Per-run turn ceiling (opt-in). Bounds agentic turns -> bounds cost per run.
    if ($MaxTurns -gt 0 -and ($ClaudeArgs -notcontains '--max-turns')) {
        $ClaudeArgs = @('--max-turns', "$MaxTurns") + $ClaudeArgs
        _log "bounded: --max-turns $MaxTurns"
    }

    $attempt = 0
    while ($true) {
        $attempt++
        $errFile = $null; $out = ''; $code = 1; $err = ''
        try {
            $errFile = [System.IO.Path]::GetTempFileName()
            # stdin-fed prompt; stderr -> temp file (NOT 2>&1, which would wrap native
            # warnings as terminating ErrorRecords under ErrorActionPreference=Stop).
            $out  = ($Prompt | & $Claude -p @ClaudeArgs 2>$errFile | Out-String).Trim()
            $code = $LASTEXITCODE
            $err  = (Get-Content $errFile -Raw -ErrorAction SilentlyContinue)
        }
        catch {
            $out = ''; $code = 1; $err = "EXCEPTION: $_"
        }
        finally {
            if ($errFile -and (Test-Path $errFile)) { Remove-Item $errFile -ErrorAction SilentlyContinue }
        }

        $combined = "$out`n$err"
        if (-not (Test-TransientClaudeError -Text $combined -ExitCode $code)) {
            if ($attempt -gt 1) { _log "recovered on attempt $attempt (exit $code)" }
            return @{ Output = $out; ExitCode = $code; Stderr = $err; Attempts = $attempt; Ok = ($code -eq 0) }
        }
        if ($attempt -ge $MaxAttempts) {
            # NOTE: coerce $err to string before Trim -- an empty stderr temp-file makes
            # Get-Content -Raw return $null, and `$null.Trim()` is a terminating error under
            # ErrorActionPreference=Stop. That crash escaped this function and turned a clean
            # double-transient into a hard cron failure. "$err" => "" if null.
            _log "FAILED after $attempt attempts (exit $code). Last error: $(("$err").Trim())"
            return @{ Output = $out; ExitCode = $code; Stderr = $err; Attempts = $attempt; Ok = $false }
        }
        $delay = [int]($BaseDelaySec * [math]::Pow(2, $attempt - 1))   # 10s, 20s, 40s
        $snippet = ($combined.Trim() -split "`r?`n" | Where-Object { $_ } | Select-Object -First 1)
        _log "transient error on attempt $attempt/$MaxAttempts (exit $code); retrying in ${delay}s. snippet: $snippet"
        Start-Sleep -Seconds $delay
    }
}

# Every script that dot-sources this runner is a headless cron: tell the SessionEnd capture hook
# (scripts/session_capture.py) not to log these sessions as if the owner had been working.
$env:WV_HEADLESS = '1'
