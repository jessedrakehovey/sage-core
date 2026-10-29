# operator_cron.ps1 -- the assistant's wake-on-work runner.
#
#   (no args)   SENSE tick: run the token-free sensors; ONLY if the queue has claimable
#               tickets, start one headless Sonnet operator to drain it. Empty queue
#               = zero tokens spent.
#   -Morning    same sensors, then the morning-brief pass (always runs once a day).
#   -SenseOnly  sensors only, never start claude (for testing / manual refresh).
#
# SAFETY: the operator runs with an explicit --allowedTools list, NOT
# --dangerously-skip-permissions, because it reads untrusted email. It can read the
# repo, write only under assistant/out/, and run only tickets.py + mail.py fetch/draft.
# No web, no MCP, no git, no Edit. mail.py has no send path at all (IMAP only).
#   -Comms      a wake from comms_probe.py (a human message just landed): same sensors, same
#               operator, but refuses to overlap a run already in flight and counts against
#               the same daily budget. The .operator_running marker is the in-flight lock.
param([switch]$Morning, [switch]$SenseOnly, [switch]$Comms)

$ErrorActionPreference = 'Stop'
$vault  = "{{ORG_ROOT}}"
$asst   = Join-Path $vault 'assistant'
$claude = "C:\Users\owner1\AppData\Roaming\npm\claude.cmd"
$py     = "C:\Users\owner1\.venvs\wedding-web\Scripts\python.exe"
$log    = Join-Path $env:USERPROFILE ".claude\assistant-operator.log"
if ($env:ASSISTANT_CLAUDE) { $claude = $env:ASSISTANT_CLAUDE }   # test hook

function Log($m) {
    # never fatal: FileShare.ReadWrite + sidecar fallback (Travel Scout outage, 2026-09-06)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $m"
    try {
        $fs = [System.IO.FileStream]::new($log, [System.IO.FileMode]::Append,
              [System.IO.FileAccess]::Write, [System.IO.FileShare]::ReadWrite)
        $sw = [System.IO.StreamWriter]::new($fs, [System.Text.UTF8Encoding]::new($false))
        $sw.WriteLine($line); $sw.Dispose()
    } catch {
        try { Add-Content -Path ($log + '.fallback') -Value $line -ErrorAction SilentlyContinue } catch {}
    }
}
. (Join-Path $vault 'scripts\lib\run_claude.ps1')

function Test-Budget {
    param([int]$Max)
    if ($Max -le 0) { return $true }
    $path  = Join-Path $asst 'data\.budget.json'
    $today = (Get-Date).ToString('yyyy-MM-dd')
    $count = 0
    if (Test-Path $path) {
        try { $s = Get-Content $path -Raw | ConvertFrom-Json; if ($s.date -eq $today) { $count = [int]$s.count } } catch {}
    }
    if ($count -ge $Max) { Log "BUDGET REACHED ($count/$Max operator runs today) - skipping LLM pass"; return $false }
    @{ date = $today; count = $count + 1 } | ConvertTo-Json -Compress | Set-Content -Path $path -Encoding UTF8
    Log "budget: run $($count + 1)/$Max"
    return $true
}

function Build-Dashboard {
    # mission-control page (assistant/web/index.html); the morning run also re-checks HoneyBook
    try {
        $a = @((Join-Path $asst 'scripts\dashboard.py')); if ($Morning) { $a += '--hb-check' }
        Log ((& $py @a 2>&1 | Out-String).Trim())
    } catch { Log "dashboard error: $($_.Exception.Message)" }
}

function Notify($text) {
    try { $text | & $py (Join-Path $asst 'scripts\notify.py') 2>&1 | ForEach-Object { Log "$_" } }
    catch { Log "notify error: $($_.Exception.Message)" }
}

$running = Join-Path $asst 'data\.operator_running'
try {
    Set-Location $vault
    $mode = if ($Morning) { 'morning' } elseif ($SenseOnly) { 'sense-only' } elseif ($Comms) { 'comms-wake' } else { 'tick' }
    # One operator at a time: a comms wake (1-min probe) and the half-hour tick must never
    # both claim the queue. The marker is the lock; a marker older than 25 min is a dead run.
    if (-not $SenseOnly) {
        if ((Test-Path $running) -and ((Get-Date) - (Get-Item $running).LastWriteTime).TotalMinutes -lt 25) {
            Log "=== $mode skipped: an operator run is in flight (.operator_running) ==="; exit 0
        }
        Set-Content -Path $running -Value "$PID $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -Encoding UTF8
    }
    Log "=== assistant $mode start ==="
    $cfg = Get-Content (Join-Path $asst 'config.json') -Raw | ConvertFrom-Json

    # order matters: mail first (followups matches its new tickets), sensors arm SOP steps,
    # then the follow-through engine fires what is due and the sentinels look for stuck work.
    foreach ($s in @(@('mail.py','sense'), @('mail_arm.py'), @('sense_local.py'), @('sense_honeybook.py'), @('sense_editor.py'), @('followups.py','tick'), @('sentinels.py'))) {
        try {
            $a = @((Join-Path $asst "scripts\$($s[0])")) + @($s | Select-Object -Skip 1)
            $ErrorActionPreference = 'Continue'    # a stderr line must not abort the sensor mid-run (see the pending check below)
            $o = & $py @a 2>&1 | Out-String
            $ErrorActionPreference = 'Stop'
            Log "SENSE $($s[0]): $($o.Trim())"
        } catch { $ErrorActionPreference = 'Stop'; Log "SENSE $($s[0]) crashed: $($_.Exception.Message)" }
    }

    # Zero-LLM approval chain, every tick: expire dead cards, hand the owner's answers to the
    # queue (free text / mint errors become tickets), then run approved exec tickets.
    try { Log ("CARDS " + ((& $py (Join-Path $asst 'scripts\cards.py') sweep 2>&1 | Out-String).Trim())) } catch { Log "cards sweep error: $($_.Exception.Message)" }
    try { Log ("DRAIN " + ((& $py (Join-Path $asst 'scripts\drain.py') 2>&1 | Out-String).Trim())) } catch { Log "drain error: $($_.Exception.Message)" }

    if ($Morning) {   # once a day: drop closed tickets older than 30 days (ids stay in the seen-sets)
        try { Log ("PRUNE " + ((& $py (Join-Path $asst 'scripts\tickets.py') prune 2>&1 | Out-String).Trim())) } catch { Log "prune error: $($_.Exception.Message)" }
    }

    # Under $ErrorActionPreference='Stop', PS 5.1 turns ANY stderr line from a native command
    # with 2>&1 into a terminating error. A warning from tickets.py used to land in the catch
    # as "0 pending", i.e. "queue empty", and real tickets waited silently. So: read stdout
    # only, and if the count cannot be read, assume there IS work rather than none.
    $pending = 0
    try {
        $ErrorActionPreference = 'Continue'
        $raw = (& $py (Join-Path $asst 'scripts\tickets.py') pending 2>$null | Out-String).Trim()
        $ErrorActionPreference = 'Stop'
        if ($raw -notmatch '^\d+$') { throw "unreadable count '$raw'" }   # [int]"" is 0 in PowerShell: a crash would read as "empty"
        $pending = [int]$raw
    } catch {
        $ErrorActionPreference = 'Stop'
        Log "pending check FAILED ($($_.Exception.Message)) - assuming work is waiting"
        $pending = 1
    }
    Log "claimable tickets: $pending"

    if ($SenseOnly) { Build-Dashboard; Log "=== sense-only end ==="; exit 0 }
    if (-not $Morning -and $pending -eq 0) { Log "queue empty -- no LLM pass (0 tokens)"; Build-Dashboard; Log "=== tick end ==="; exit 0 }
    if (-not (Test-Path $claude)) { Log "ERROR: claude CLI not found at $claude"; exit 1 }
    if (-not (Test-Budget -Max ([int]$cfg.operator.runs_per_day_max))) { exit 0 }

    $promptFile = if ($Morning) { 'brief.md' } else { 'operator.md' }
    $prompt  = Get-Content (Join-Path $asst "prompts\$promptFile") -Raw
    # The owners' last day of chat with Sage, appended as DATA (ported from the property OS's
    # lane_chat injection, 2026-10-08): the drafter judges a text knowing what the owner said today.
    try {
        $chat = & $py (Join-Path $asst 'scripts\owner_chat.py') 2>$null | Out-String
        if ($chat -and $chat.Trim()) { $prompt = $prompt + "`n`n" + $chat.Trim() + "`n" }
    } catch { Log "owner_chat block skipped: $_" }
    # NOTE: keep these as @(...) -join ' ' arrays: scripts/guardrails/check_gates.py
    # parses that exact shape to freeze/verify the allow/deny lists.
    # File writes: the harness matches file tools only through Edit(path) rules
    # ("Write(...)" rules are ignored and a bare "Edit" deny blocks Write too), so
    # the ONLY writable path is assistant/out/** and every other tree is denied by path.
    $allowed = @('Read','Glob','Grep','Skill','Edit(assistant/out/**)',
                 'Bash(python assistant/scripts/tickets.py:*)',
                 'Bash(python assistant/scripts/mail.py fetch:*)',
                 'Bash(python assistant/scripts/mail.py draft:*)',
                 'Bash(python assistant/scripts/cards.py post:*)',
                 'Bash(python assistant/scripts/cards.py note:*)',
                 'Bash(python assistant/scripts/cards.py say:*)',
                 'Bash(python assistant/scripts/cards.py pending:*)',
                 'Bash(python assistant/scripts/rulings.py find:*)',
                 'Bash(python assistant/scripts/followups.py arm:*)',
                 'Bash(python assistant/scripts/followups.py list:*)',
                 'Bash(python assistant/scripts/followups.py mark:*)',
                 'Bash(python assistant/scripts/client_texts.py:*)',
                 'Bash(python assistant/scripts/learn.py:*)',
                 'Bash(python assistant/scripts/phone_bridge.py thread:*)') -join ' '
    # The operator may ASK (cards.py post) but never ANSWER, mint, drain or send:
    # those are the owner's tap (cards.py answer) and the zero-LLM drain.
    $denied  = @('WebFetch','WebSearch','NotebookEdit','Task','Agent',
                 'Bash(python assistant/scripts/cards.py answer:*)',
                 'Bash(python assistant/scripts/cards.py undo:*)',
                 'Bash(python assistant/scripts/cards.py harvest:*)',
                 'Bash(python assistant/scripts/drain.py:*)',
                 'Bash(python assistant/scripts/email_send.py:*)',
                 'Bash(python assistant/scripts/exec_store.py:*)',
                 'Bash(python assistant/scripts/rulings.py add:*)',
                 'Edit(Wiki/**)','Edit(Memory/**)','Edit(Schema/**)','Edit(scripts/**)',
                 'Edit(.claude/**)','Edit(assistant/scripts/**)','Edit(assistant/prompts/**)',
                 'Edit(assistant/config.json)','Edit(assistant/data/**)',
                 'mcp__gmail__send_email','mcp__gmail__delete_email',
                 'mcp__honeybook__take_action','mcp__playwright__browser_click',
                 'mcp__playwright-cli__browser_click') -join ' '

    $r = Invoke-ClaudeHeadless -Claude $claude -Prompt $prompt `
            -ClaudeArgs @('--model', [string]$cfg.operator.model, '--allowedTools', $allowed, '--disallowedTools', $denied) `
            -MaxTurns ([int]$cfg.operator.max_turns) -Logger ${function:Log}
    if ($r.Output) { Log "OUTPUT:`n$($r.Output)" }
    if (-not $r.Ok) { Log "operator run FAILED (exit $($r.ExitCode)) stderr: $([string]$r.Stderr)"; Notify "Assistant operator run failed - see ~/.claude/assistant-operator.log"; exit 1 }

    $out = "$($r.Output)".Trim()
    if ($Morning -or ($out -match 'NEEDS owner1' -and $out -notmatch 'staged=0')) { Notify $out }
    Build-Dashboard
    Log "=== $mode end ==="
}
catch {
    Log "FATAL: $($_.Exception.Message)"
    exit 1
}
finally {
    if (-not $SenseOnly) { try { Remove-Item -Path $running -Force -ErrorAction SilentlyContinue } catch {} }
}
