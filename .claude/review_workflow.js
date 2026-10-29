export const meta = {
  name: 'wiki-review',
  description: 'Adversarially review every Wiki/ note for accuracy, client privacy, footage-handling safety, delivery-promise accuracy, and schema integrity; return structured findings',
  phases: [
    { title: 'Operational', detail: 'deep source-verification of knowledge notes across all Wiki/ sections' },
    { title: 'Couples', detail: 'client-privacy + sensitive-data + schema scan' },
    { title: 'VendorsVenuesPackages', detail: 'accuracy + schema scan for vendor/venue/package/integration notes' },
  ],
}

// ── Batch definitions ────────────────────────────────────────────────────────
// Operational notes: all non-couple notes reviewed one agent per note.
// Sourced dynamically so new notes are picked up automatically.
// If the wiki is sparse at time of running, agents will report "file not found"
// gracefully — verdict will be "blocked" and findings will explain.

const OPERATIONAL_BATCHES = [
  // Operations + Post-Production (one batch)
  [
    'Wiki/Operations/Automations.md',
    'Wiki/Operations/Gear Checklist.md',
    'Wiki/Operations/Media Handling.md',
    'Wiki/Operations/Backup Protocol.md',
    'Wiki/Operations/Shoot Day Runsheet.md',
    'Wiki/Post-Production/Edit Workflow.md',
    'Wiki/Post-Production/Review Process.md',
    'Wiki/Post-Production/Delivery Methods.md',
    'Wiki/Post-Production/Color Grading Notes.md',
    'Wiki/Post-Production/Audio Workflow.md',
  ],
  // Packages + Integrations (one batch)
  [
    'Wiki/Packages/Package Overview.md',
    'Wiki/Packages/Add-Ons.md',
    'Wiki/Integrations/Google Apps.md',
    'Wiki/Integrations/Calendar Setup.md',
    'Wiki/Integrations/Gmail Setup.md',
    'Wiki/Integrations/Storage.md',
    'Wiki/Integrations/Project Tracker.md',
  ],
]

// Couple profiles reviewed in batches of ~10
const COUPLE_BATCHES = [
  ['Wiki/Couples'],   // agent globs the folder; handled as a directory hint
]

// Vendor / Venue notes reviewed together
const VENDOR_VENUE_BATCHES = [
  [
    'Wiki/Vendors',
    'Wiki/Venues',
  ],
]

// ── Findings schema ──────────────────────────────────────────────────────────
const FINDINGS_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['notes'],
  properties: {
    notes: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['path', 'verdict', 'findings'],
        properties: {
          path: { type: 'string' },
          verdict: { type: 'string', enum: ['clean', 'minor', 'major', 'blocked'] },
          findings: {
            type: 'array',
            items: {
              type: 'object',
              additionalProperties: false,
              required: ['severity', 'category', 'detail'],
              properties: {
                severity: { type: 'string', enum: ['critical', 'major', 'minor', 'nit'] },
                category: { type: 'string', enum: ['accuracy', 'client-privacy', 'footage-safety', 'delivery-promise', 'send-gating', 'contract-pricing', 'scope', 'schema', 'links', 'other'] },
                detail: { type: 'string' },
                suggested_fix: { type: 'string' },
              },
            },
          },
        },
      },
    },
  },
}

// ── Review rules ─────────────────────────────────────────────────────────────
const RULES = `You are reviewing notes in an LLM wiki for a wedding videography business run by owner1. Be adversarial and precise — your job is to catch problems, not to praise.

Hard rules this wiki must obey (from CLAUDE.md and Schema/schema.md):

- ACCURACY: every compiled claim must match the cited Raw/ source. Read the note's front-matter "sources:" and open each Raw/ path to verify. Flag any claim not supported by a source, any date/price/turnaround figure that disagrees, and any source cited but not reflected.

- CLIENT PRIVACY: couple profiles legitimately hold contact, booking, and shoot-logistics facts. Flag only: content that looks like an unnecessary exposure of personal data (e.g. home addresses in git-tracked notes where a shoot address would suffice), medical detail beyond a neutral access need, or anything that would embarrass the couple if read by a stranger. Operational contact info needed to coordinate the shoot is acceptable.

- FOOTAGE SAFETY: never suggest moving, renaming, or deleting original media files. Flag any note that implies it is acceptable to format a card or delete originals before a verified backup exists. Flag any note that gives a file path to originals that contradicts the established backup protocol. This is a hard safety rail — findings here are always severity "critical".

- DELIVERY PROMISE ACCURACY: delivery dates, turnaround times, and promised deliverables in couple profiles must match the project state file at "{{ORG_ROOT}}/scripts/projects/project_state.json". Flag any date or deliverable mismatch. Turnaround language must not overpromise beyond what owner1 has committed to.

- SEND GATING: no note should imply that any email, text, social post, or client-facing communication can go out without owner1's explicit per-item approval. Flag any note that describes or implies an auto-send flow without owner1's approval.

- CONTRACT / PRICING / TURNAROUND LANGUAGE: contract terms, pricing figures, and turnaround commitments must come from owner1-approved sources only (Wiki/Packages/, signed contracts in Raw/). Flag any note that asserts pricing, turnaround, or contract terms not traceable to an approved source.

- SCOPE: a note should compile reusable knowledge, not dump a transcript or raw email thread. Flag notes that are off-topic, duplicative of another note, or mis-filed for their Wiki/ area.

- SCHEMA: front matter must have type/area/tags/created/updated/sources/status; tags must be from the allowed list in Schema/schema.md; area must mirror the folder. Flag violations.

- LINKS: [[wikilinks]] should resolve to real notes (basename match). Flag obviously broken/typo'd links. (Unresolved links to not-yet-written notes are acceptable — only flag clear errors.)

For each note: give an overall verdict (clean = ready to mark verified; minor = small fixes then verify; major = needs real rework; blocked = can't verify, e.g. source is image-only with no text). List specific findings with severity, category, a concrete detail, and a suggested_fix where you can. If a note is clean, return it with verdict "clean" and an empty findings array. Do NOT edit any files — report only.`

// ── Phase: Operational ───────────────────────────────────────────────────────
phase('Operational')
const opResults = await parallel(
  OPERATIONAL_BATCHES.map((batch, i) => () =>
    agent(
      `${RULES}\n\nReview this BATCH of operational wiki notes: ${JSON.stringify(batch)}\n\nFor each path: if the file does not exist yet, return verdict "blocked" with a single finding explaining it is not yet written. Otherwise read it in full, then read every Raw/ source it cites and verify compiled claims against them. Sanity-check wikilinks and front matter. Return one entry per path.`,
      { label: `op:batch${i + 1}`, phase: 'Operational', schema: FINDINGS_SCHEMA, agentType: 'Explore' }
    )
  )
)

// ── Phase: Couples ───────────────────────────────────────────────────────────
phase('Couples')
const coupleResults = await parallel(
  COUPLE_BATCHES.map((batch, i) => () =>
    agent(
      `${RULES}\n\nReview couple profile notes. Glob the directory/directories: ${JSON.stringify(batch)} and list every .md file found. Review each one.\n\nThese are client data profiles. Focus on: CLIENT-PRIVACY (unnecessary personal data exposure, contact info beyond operational need), DELIVERY-PROMISE (dates and deliverables matching project_state.json), SEND-GATING (no auto-send implication), and SCHEMA (front matter validity). Do not flag ordinary booking/shoot/contact facts that are operationally necessary. Read each note; return one entry per path.`,
      { label: `couples:batch${i + 1}`, phase: 'Couples', schema: FINDINGS_SCHEMA, agentType: 'Explore' }
    )
  )
)

// ── Phase: Vendors / Venues / Packages / Integrations ───────────────────────
phase('VendorsVenuesPackages')
const vvpResults = await parallel(
  VENDOR_VENUE_BATCHES.map((batch, i) => () =>
    agent(
      `${RULES}\n\nReview vendor, venue, package, and integration notes. Glob each directory in: ${JSON.stringify(batch)} and list every .md file found. Review each one.\n\nFocus on: ACCURACY (contact details, pricing, turnaround match Raw/ sources), CONTRACT-PRICING (no untraced pricing claims), SCHEMA (front matter), and LINKS. Return one entry per path; most vendor/venue notes should be verdict "clean" if their data matches sources.`,
      { label: `vvp:batch${i + 1}`, phase: 'VendorsVenuesPackages', schema: FINDINGS_SCHEMA, agentType: 'Explore' }
    )
  )
)

// ── Aggregate ────────────────────────────────────────────────────────────────
const all = [...opResults, ...coupleResults, ...vvpResults].filter(Boolean)
const flat = all.flatMap((r) => r.notes || [])
const summary = {
  total_reviewed: flat.length,
  clean: flat.filter((n) => n.verdict === 'clean').length,
  minor: flat.filter((n) => n.verdict === 'minor').length,
  major: flat.filter((n) => n.verdict === 'major').length,
  blocked: flat.filter((n) => n.verdict === 'blocked').length,
}
const issues = flat.filter((n) => n.verdict !== 'clean')
log(`review complete: ${summary.total_reviewed} reviewed — ${summary.clean} clean, ${summary.minor} minor, ${summary.major} major, ${summary.blocked} blocked`)
return { summary, issues, clean_paths: flat.filter((n) => n.verdict === 'clean').map((n) => n.path) }
