# CRM contract

The engine ships with **no CRM**. When an owner tells Sage which CRM they use (in onboarding or later in chat),
Sage's builder writes a connector for it: one module, `assistant/scripts/crm_<id>.py`, listed in
`assistant/config.json -> plugins.modules`. Everything in the engine that touches clients, projects, stages,
meetings, tasks or forms reads and writes through `assistant/scripts/crm.py`, which asks that connector.
With no connector installed every CRM path stays quiet: an empty snapshot, no cards, no errors.

## 1. The snapshot (required)

The connector keeps one JSON file fresh (a spine job it declares, read-only against the CRM). Default path
`assistant/data/crm.json`; the connector may set `SNAPSHOT_PATH`, or the owner `config.json -> crm.snapshot`.

```json
{
  "ts": "2026-10-29T14:00:00Z",
  "stages":   [{"stage_id": "s1", "name": "Inquiry", "category": "lead", "order": 0}],
  "projects": [{"id": "p1", "name": "Client name", "date": "2027-06-01", "stage_id": "s1", "status": "lead",
                "stage_moved_at": "...", "last_activity_at": "...", "created_at": "...", "thread_id": "p1"}],
  "contacts": [{"id": "c1", "name": "...", "email": "...", "phone": "...", "company": "..."}],
  "meetings": [{"id": "m1", "title": "...", "start": "...", "end": "...", "thread_id": "p1", "clients": [], "hosts": []}],
  "tasks":    [{"id": "t1", "description": "...", "status": "open", "due_date": "...", "project": "..."}],
  "forms":    [{"id": "f1", "name": "...", "thread_id": "p1", "status": "submitted", "sent_at": "..."}],
  "money":    {"year": 2026, "paid_cents": 0, "not_paid_cents": 0}
}
```

- `status` on a project: `lead` (no contract yet) or `client` (booked); anything else is the CRM's own word.
- `stages[].order` grows along the pipeline; `category` is `lead`, `booked` or the CRM's own.
- `thread_id`: the id a message to that client is sent against (a project, deal or conversation).
- Every list may be empty and every field may be missing; readers treat missing as unknown.

## 2. What the connector module declares

| Name | Kind | What it is |
|---|---|---|
| `CRM = True` | flag | marks the module as the organization's CRM connector |
| `SERVICE` | str | the CRM's name as owners say it (`crm.name()`) |
| `SNAPSHOT_PATH` | str | where the snapshot lives, relative to the organization root (optional) |
| `FIELD_ALIASES` | dict | the organization's older record field names for `crm_id`, `crm_status`, `crm_stage`, `thread_id`, `forms` |
| `SENSOR_CMDS` | dict | the command that refreshes the snapshot (health checks run it) |
| `MCP_SERVERS` | dict | the CRM's MCP server, if it has one (added to the agent's turn) |

Optional, only when the CRM can do it and the owner wants it (every write still waits for an owner's tap):

| Name | What it does |
|---|---|
| `valid_thread(s)` | is `s` an id a message can be sent against |
| `message_exec(thread_id, body, subject, in_reply_to)` | the `{kind, payload}` of a message in the client's thread |
| `stage_exec(thread_id, stage)` / `apply_stage(thread_id, stage, dry)` | move a project along the pipeline |
| `booked_meeting(thread_id, who)` | a meeting the client already booked through the CRM's scheduler |
| `crm_people()` | the CRM's contacts as phone-book people |
| `PREVIEW_TAG` | the tag a card carries before the exact text of a CRM message |
| `MESSAGE_KINDS`, `INBOUND_KINDS`, `STAGE_KINDS` | the connector's exec kinds by role |
| `KIND_LANES`, `REQUIRED_KEYS`, `validate(kind, payload)`, `EXECUTORS`, `result_line`, `after_done`, `OUTBOUND_KINDS`, `SENT_KINDS`, `KIND_WORDS`, `SEND_WORDS` | the executor hooks (see `exec_store.py`, `drain.py`) |
| `ROUTES`, `RETRY_KINDS`, `FOREVER_GATED`, `REPLY_KINDS`, `CHANNEL_ALIASES` | how its kinds report, retry, graduate and reply |
| `sentinel_findings()`, `resolved(words)`, `SERVICE_WORDS` | its own health findings |

A hook the connector does not define is simply absent: the engine runs without it.

## 3. How a connector gets built

1. The owner names their CRM (onboarding Discovery, or "we use X" in chat).
2. `onboarding.py` matches it in `assistant/catalog/` (how that CRM connects: MCP, API, export) and, when no
   connector module is installed, files ONE build request: "Write `crm_<id>.py` against CRM_CONTRACT.md".
3. The builder writes the module and its snapshot job, the owner taps to approve anything protected, and the
   connector is added to `plugins.modules`. Reads start at once; writes are offered only as cards.
4. `python assistant/scripts/crm.py check` (or the tests) confirms the snapshot matches this contract.

The organization's connector is its own: it never travels back into the engine.
