# dream_cron.ps1 — daily memory consolidation ("Dreaming") for this folder.
# Runs the Claude CLI headless in this folder to invoke the `dreaming` skill, which
# reads new Memory/Daily_Logs/ entries and promotes durable facts into
# Memory/memory.md (and cross-promotes to the Wiki/ profiles of people, clients and vendors),
# runs a self-improvement scan, then ingests up to 3 pending Raw/ sources into draft
# wiki notes, and commits. Unlike deterministic syncs this NEEDS an LLM, so it shells
# out to `claude -p`. Registered as a Windows Scheduled Task (6:30 AM daily).
# Logs to ~/.claude/dreaming-wv.log. Counts against the subscription rate limit.

$ErrorActionPreference = 'Stop'
$vault  = Split-Path -Parent $PSScriptRoot          # this script lives in <vault>\scripts
$claude = $env:ASSISTANT_CLAUDE                     # else resolved below the way assistant/sage.py claude_cli() does
$log    = Join-Path $env:USERPROFILE ".claude\dreaming-wv.log"

function Log($m) {
    # Shared-write append with sidecar fallback — an orphaned `tail -F` on the
    # travel-scout log killed every Travel Scout run 2026-09-04..06 because
    # Add-Content threw before the try block. Logging must never be fatal.
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
. (Join-Path $vault 'scripts\lib\run_claude.ps1')   # shared headless runner w/ transient-error retry


function Invoke-Py([string[]]$PyArgs) {
    # Run python and return its combined output as text; exit code in $script:PyExit.
    # Under $ErrorActionPreference='Stop', PS 5.1 turns the FIRST stderr line of a native
    # command redirected with 2>&1 into a terminating error. unittest prints its progress dots
    # on stderr, so the nightly self-test died on its first suite every night and never
    # recorded one result (2026-10-01). Stderr is tolerated here, never fatal.
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $o = (& python @PyArgs 2>&1 | ForEach-Object { "$_" }) -join "`n"
        $script:PyExit = $LASTEXITCODE
        return $o
    } finally { $ErrorActionPreference = $old }
}

function Notify-MissionControl($text) {
    # The live channel since Telegram was retired on this side: a note on Mission Control
    # (assistant/data/cards.jsonl, shown under Notifications). Never throws.
    try {
        $tmp = [IO.Path]::GetTempFileName()
        [IO.File]::WriteAllText($tmp, [string]$text, (New-Object System.Text.UTF8Encoding($false)))
        $r = Invoke-Py @((Join-Path $vault 'assistant\scripts\notify.py'), '--file', $tmp)
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
        Log "MISSION-CONTROL: $($r.Trim())"
    } catch { Log "mission-control notify failed -- $($_.Exception.Message)" }
}


$prompt = @'
Use the dreaming skill now. Run ALL THREE phases.

Phase 1 (memory): Read Memory/memory.md to find the "Last consolidated" date, then
consolidate every Memory/Daily_Logs/ entry newer than that date into Memory/memory.md
per the skill (durable facts only; cross-promote facts about people, clients and vendors
to their Wiki/ profiles; update the Last consolidated date).

Phase 2 (self-improvement): Re-read those same logs for skill/mistake signals
(corrections, frustration, "remember this", wrong/incomplete skills, un-logged
mistakes) and for recurring manual asks. Apply the dreaming skill's GRADUATED
auto-vs-gated policy: AUTO-APPLY low-risk fixes — append un-logged mistakes to
Schema/mistakes.md and patch a SKILL.md's steps/recipes/pitfalls directly — but keep
GATED (propose only) anything touching the protected core (footage-handling safety,
send-gating, client-facing turnaround/pricing promises, or these guardrails) and any
new scheduled automation.
Protected-core skill/mistakes proposals -> Memory/skill-suggestions.md; new automations
-> Memory/automation-suggestions.md (dedupe against every section incl. latched
Dismissed; respect the Pending caps). Honor the safety rails: evidence required,
confidence gate (unsure -> propose, never guess into a live skill), max 3 auto-applied
SKILL.md patches per night, dedupe, smallest viable edit. Log every auto-applied change
in skill-suggestions.md's "Applied automatically" section.

Skill Radar step (within Phase 2): read scripts/skill_radar/candidates.json (the
token-free prefilter output) and scripts/skill_radar/dismissed.json (latched
dismissals). For each candidate in candidates.json that is NOT already in
dismissed.json, judge whether it represents a genuine unmet operational need, an
extension of an existing skill, or something already covered / a one-off. New-skill and
extend proposals go to Memory/skill-suggestions.md (Pending), cap 3/night. Never
re-propose a key that appears in dismissed.json.

Phase 3 (wiki ingest): Compile pending Raw/ sources into draft wiki notes. Run
"python scripts/wiki_tool.py sources" and read Wiki/_meta/sources.md for the **pending**
rows. The coverage tracker is file-type-agnostic -- PowerPoints, PDFs, Word docs, and
transcripts count, not just markdown. Pick AT MOST 3 highest-value pending sources
(the organization's own operational documents -- procedures, price lists, contracts,
checklists, guides -- before AI-reference material) and run the wiki-ingest skill on exactly those:
it extracts per-type text (.pptx/.docx/.xlsx via the Office skills, .pdf via the pdf
skill) and writes schema-valid status:draft notes into the correct subfolder per the
folder routing table in Schema/schema.md. Skip non-textual media (.m4a/.mp4/images) and
blank fillable forms -- note them out-of-scope, never fabricate. Run
"python scripts/wiki_tool.py audit" and fix every lint error before committing. The
rest of the backlog rolls to tomorrow. A failed/unreadable ingest must never abort
Phases 1-2 -- log it and move on.

Then commit, scoping the add to ONLY the files this pass changed (git add <explicit
paths>, incl. any new Wiki/ notes + the Wiki/_meta/ files the audit regenerated; NEVER
git add -A -- the desktop instance shares this working tree). Message
"dreaming: consolidated <dates> (+N auto-applied, +M proposed, +K ingested)", dropping any
count that is zero. If there are no new logs AND no pending sources to ingest, change
nothing and reply exactly: NO NEW LOGS.
'@

try {
    Log "=== dreaming start ==="
    # Rotate oversized cron logs before anything else (non-fatal -- a failure must not break dreaming).
    try { . (Join-Path $vault 'scripts\lib\rotate_logs.ps1') } catch { Log "log rotation warning (non-fatal): $($_.Exception.Message)" }
    # Skill Radar prefilter (token-free, stdlib): refresh scripts/skill_radar/candidates.json
    # so Phase 2's radar step reads tonight's clusters, not a stale file. Non-fatal.
    try {
        $sr = Invoke-Py @((Join-Path $vault 'scripts\skill_radar\detect.py'))
        if ($sr.Trim()) { Log "SKILL-RADAR: $($sr.Trim())" }
    } catch { Log "skill-radar prefilter error (non-fatal): $_" }
    # Nightly self-test (zero-LLM): the assistant's offline suites + the send-gate tripwire.
    # Results land in the eval ledger (scripts/evals/eval_ledger.jsonl) so a regression is a
    # visible FAIL streak, not a silent one. Non-fatal for dreaming itself.
    # this organization's own nightly steps (scripts/dream_extra.ps1, a niche file that never ships): its CRM sync, its
    # extra test suites ($ExtraSuites). Dot-sourced when present; an error there never stops dreaming (2026-10-29).
    $ExtraSuites = @()
    $extra = Join-Path $vault 'scripts\dream_extra.ps1'
    if (Test-Path $extra) { try { . $extra } catch { Log "dream_extra error (non-fatal): $_" } }
    try {
        # only the suites this folder carries: an organization made from the engine has no tests and none of the
        # niche ones (a missing file is not a FAIL, it is simply not run; 2026-10-29)
        $suites = @('assistant\tests\test_assistant.py', 'assistant\tests\test_cards_exec.py', 'assistant\tests\test_followups.py', 'assistant\tests\test_mail_arm.py', 'assistant\tests\test_phone_bridge.py', 'assistant\tests\test_email_send.py', 'assistant\tests\test_quote_pipeline.py', 'assistant\tests\test_conn_check.py', 'assistant\tests\test_pending.py', 'scripts\guardrails\check_gates.py') + @($ExtraSuites) | Where-Object { Test-Path (Join-Path $vault $_) }
        $failed = @()
        foreach ($t in $suites) {
            $name = [IO.Path]::GetFileNameWithoutExtension($t)
            try {                                   # per suite: one broken suite must not hide the others
                $out = Invoke-Py @((Join-Path $vault $t))
                $ok = ($script:PyExit -eq 0)
                $verdict = if ($ok) { 'PASS' } else { 'FAIL' }
                $last = ($out.Trim() -split "`n")[-1]
                if ($last.Length -gt 200) { $last = $last.Substring(0, 200) }
                # PS 5.1 drops an empty argument and does not escape a double quote for a native
                # exe, and a note starting with "-" reads as a flag: any of those made the record
                # call fail in argparse, so a FAIL never reached the ledger.
                $last = ($last -replace '"', "'").Trim()
                if (-not $last) { $last = '(no output)' }
                [void](Invoke-Py @((Join-Path $vault 'scripts\evals\eval_ledger.py'), 'record', '--pipeline', "selftest-$name", '--artifact', $t, '--verdict', $verdict, "--note=$last"))
                if ($script:PyExit -ne 0) { Log "SELFTEST ${name}: verdict $verdict was NOT recorded in the eval ledger (exit $script:PyExit)" }
                if ($ok) { Log "SELFTEST ${name}: PASS" } else { $failed += $name; Log "SELFTEST ${name}: FAIL`n$($out.Trim())" }
            } catch { $failed += $name; Log "SELFTEST ${name}: could not run -- $_" }
        }
        if ($failed.Count) { Notify-MissionControl ("Nightly self-test FAILED: " + ($failed -join ', ') + ". The assistant's safety suites are not all green; see ~/.claude/dreaming-wv.log.") }
    } catch { Log "self-test error (non-fatal): $_" }
    # the Claude CLI: ASSISTANT_CLAUDE in the environment (sage serve sets it), else what assistant/sage.py finds
    # (PATH, the native installer, the npm global folder) - never a user path written here (2026-10-29)
    if (-not $claude -or -not (Test-Path $claude)) {
        try { $claude = (Invoke-Py @((Join-Path $vault 'assistant\sage.py'), 'claude-cli')).Trim() } catch { $claude = '' }
    }
    if (-not $claude -or -not (Test-Path $claude)) { Log "ERROR: claude CLI not found (set ASSISTANT_CLAUDE or install the Claude Code CLI; python assistant/sage.py doctor)"; exit 1 }
    $before = (& git -C $vault rev-parse HEAD 2>$null)
    Push-Location $vault
    # Headless, unattended -> skip interactive permission prompts. Writes + commits are
    # the whole point of this job; the repo is the rollback save point. The shared runner
    # feeds the prompt via stdin and retries transient socket/5xx errors (run_claude.ps1).
    # WV_CLAUDE_MAX_TURNS / WV_CLAUDE_DAILY_MAX cap per-run turns and daily invocations.
    $maxTurns = if ($env:WV_CLAUDE_MAX_TURNS -as [int]) { [int]$env:WV_CLAUDE_MAX_TURNS } else { 0 }
    $dailyMax = if ($env:WV_CLAUDE_DAILY_MAX -as [int]) { [int]$env:WV_CLAUDE_DAILY_MAX } else { 0 }
    $r = Invoke-ClaudeHeadless -Claude $claude -Prompt $prompt `
        -ClaudeArgs @('--model','sonnet','--dangerously-skip-permissions') `
        -MaxTurns $maxTurns -DailyMax $dailyMax `
        -Logger ${function:Log}
    Pop-Location
    if ($r.Output) { Log "OUTPUT:`n$($r.Output)" }
    if ($r.Stderr -and $r.Stderr.Trim()) { Log "STDERR:`n$($r.Stderr.Trim())" }
    Log "=== dreaming done (exit $($r.ExitCode), attempts $($r.Attempts)) ==="
    if ($r.Skipped) { Notify-MissionControl "Dreaming was SKIPPED on $(Get-Date -Format 'yyyy-MM-dd'): the daily headless-run budget (WV_CLAUDE_DAILY_MAX) was already used up. Memory was not consolidated." }

    # Notify the owner via Mission Control with the EXACT git diff this run produced (Phase 1 memory +
    # Phase 2 auto-applied skill/mistakes edits), so self-improvements are reviewable/revertible.
    $after = (& git -C $vault rev-parse HEAD 2>$null)
    if ($before -and $after -and ($before -ne $after)) {
        $commits = (& git -C $vault log --oneline "$before..$after") -join "`n"
        $stat    = (& git -C $vault diff --stat "$before..$after") -join "`n"
        Log "CHANGED:`n$commits`n$stat"
        Notify-MissionControl ("Dreaming {0} changed this folder:`n{1}`n`n{2}`n`nRevert any of it: git -C `"{3}`" revert <hash>" -f (Get-Date -Format 'yyyy-MM-dd'), $commits, $stat, $vault)
    }

    # SKILL.md size guard: dreaming auto-patches SKILLs, and every line is re-injected into
    # each cron's LLM context, so warn if any grew past the cap. Report-only; non-fatal.
    # (Trimming/splitting to an overflow file is a human/next-dreaming decision.)
    try {
        $sz = Invoke-Py @((Join-Path $vault 'scripts\lib\skill_size_check.py'))
        if ($script:PyExit -ne 0 -and $sz.Trim()) {
            Log "SKILL SIZE:`n$($sz.Trim())"
            Notify-MissionControl ("SKILL.md over line cap (trim/split to an overflow file):`n" + $sz.Trim())
        }
    } catch { Log "skill-size check error (non-fatal): $_" }

    if (-not $r.Ok) {
        Notify-MissionControl "Dreaming cron FAILED (exit $($r.ExitCode)) on $(Get-Date -Format 'yyyy-MM-dd'). Check ~/.claude/dreaming-wv.log."
        exit 1
    }
}
catch {
    Log "ERROR: $_"
    Notify-MissionControl "Dreaming cron ERROR on $(Get-Date -Format 'yyyy-MM-dd'): $_"
    exit 1
}
