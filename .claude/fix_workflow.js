export const meta = {
  name: 'wiki-fix',
  description: 'Verify-then-fix operational wiki notes against their Raw sources; apply only confirmed fixes, skip false positives and decisions that need the owner',
  phases: [{ title: 'Fix', detail: 'one agent per note, re-verify then edit' }],
}

// Note list is provided via workflow args as an array of note paths (relative or absolute).
// If args is missing or empty, log a clear message and exit cleanly.
const notePaths = (args && Array.isArray(args) && args.length > 0) ? args : null
if (!notePaths) {
  log('fix_workflow: no note paths supplied in args. Pass an array of Wiki/ note paths (e.g. from review_workflow output) to run fixes. Nothing to do.')
  return { notes: 0, applied: [], needs_owner: [], skipped: [] }
}

const VAULT_ROOT = '{{ORG_ROOT}}'

// Normalise each entry: accept either a bare relative path string
// or an object with {abs, rel, findings}. When given a bare path, findings defaults to [].
const FIXES = notePaths.map((entry) => {
  if (typeof entry === 'string') {
    const rel = entry.replace(/\\/g, '/')
    const abs = rel.startsWith('C:/') ? rel : `${VAULT_ROOT}/${rel}`
    return { abs, rel: rel.replace(`${VAULT_ROOT}/`, ''), findings: [] }
  }
  // Already shaped as {abs, rel, findings}
  const rel = (entry.rel || entry.path || '').replace(/\\/g, '/')
  const abs = (entry.abs || '').replace(/\\/g, '/') || `${VAULT_ROOT}/${rel}`
  return { abs, rel, findings: entry.findings || [] }
})

const RESULT_SCHEMA = {
  type: 'object', additionalProperties: false, required: ['path', 'applied', 'skipped'],
  properties: {
    path: { type: 'string' },
    applied: { type: 'array', items: { type: 'string' } },
    skipped: { type: 'array', items: { type: 'object', additionalProperties: false, required: ['finding','reason'], properties: { finding: {type:'string'}, reason: {type:'string'} } } },
    needs_owner: { type: 'array', items: { type: 'string' } },
    audit_note: { type: 'string' },
  },
}

phase('Fix')
const results = await parallel(
  FIXES.map((item) => () =>
    agent(
      `You are fixing ONE note in an Obsidian wedding-videography wiki. The file is at the ABSOLUTE path:
${item.abs}

A prior review flagged these findings (severity/category/detail/suggested-fix):
${JSON.stringify(item.findings, null, 1)}

YOUR JOB — for each finding:
1. Read the note at the absolute path above. (It exists. If a Read fails, retry with the exact absolute path; do not conclude it is missing.)
2. Re-verify the finding against the note's cited Raw/ sources (front-matter "sources:"; Raw/ lives at "${VAULT_ROOT}/Raw/..."). Open the source and confirm the discrepancy is real before changing anything.
3. If CONFIRMED real and the fix is unambiguous (accuracy correction matching source, delivery-date correction matching project_state.json, broken-link repair, schema/tag/source-path fix, removing an unsourced figure): APPLY it with Edit. Preserve Obsidian formatting (callouts, ^block-refs, aliases, mermaid).
4. If the finding is a FALSE POSITIVE (e.g. claims a tag is disallowed when it IS in Schema/schema.md's allowed list; claims a file is missing when it exists): SKIP it and record why.
5. If the fix needs an OWNER DECISION (which of two conflicting delivery dates is correct; whether a package price actually exists; reconciling source-of-truth conflicts; anything where you cannot determine the right answer from sources): DO NOT guess — record it under needs_owner and leave the text, but you MAY add a brief "> [!warning] Unverified — confirm with owner1" callout flagging it.
6. Never remove a cited fact that is true but just mis-attributed — fix the attribution instead.
7. Do NOT change the front-matter "status:" field. Do NOT touch any file other than this one.
8. FOOTAGE SAFETY: never suggest moving, renaming, or deleting original media files. If a finding touches footage paths, record it under needs_owner only.
9. CLIENT PRIVACY: never add or expand personal or contact details beyond what is already present in the note.

Allowed tags (Schema/schema.md): couples, vendors, venues, packages, post-production, operations, integrations, inquiry, booking, shoot-prep, shoot-day, ingest, edit, review, delivery, contract, payment, timeline, backup, equipment, travel, communication, templates, automation, memory, agents, claude-code, obsidian, skills, mcp, email, calendar, google-drive, google-voice.
The vault area convention uses hyphens and the linter passes with it — do NOT change area slugs to spaces; that is a false positive.

Bump the front-matter "updated:" to today's date if you change the body. Report what you applied, skipped (with reason), and what needs the owner.`,
      { label: item.rel.split('/').pop(), phase: 'Fix', schema: RESULT_SCHEMA, agentType: 'claude' }
    )
  )
)

const all = results.filter(Boolean)
const applied = all.flatMap((r) => (r.applied || []).map((a) => `${r.path}: ${a}`))
const needsOwner = all.flatMap((r) => (r.needs_owner || []).map((a) => `${r.path}: ${a}`))
const skipped = all.flatMap((r) => (r.skipped || []).map((s) => `${r.path}: ${s.finding} — ${s.reason}`))
log(`fix complete: ${all.length} notes, ${applied.length} fixes applied, ${needsOwner.length} need owner, ${skipped.length} skipped`)
return { notes: all.length, applied, needs_owner: needsOwner, skipped }
