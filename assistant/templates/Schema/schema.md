# Wiki Schema - The Contract

This file defines the structure every note in `Wiki/` must follow. The lint tool
(`scripts/wiki_tool.py lint`) parses the fenced blocks in this file directly -
**the lists below are machine-read**. To change the allowed types, tags or routing,
edit the fenced blocks here; the linter and the agent's note writer update automatically.

This is the generic contract the engine ships. The organization owns this copy: extend
the types, tags and routing to fit the business (every change is a one-line edit to a
fenced block), and keep the field rules as they are.

## Front matter (required on every Wiki/ note)

```yaml
---
type: workflow
area: operations
tags: [procedure]
created: 2026-01-01
updated: 2026-01-01
sources:
  - Raw/Uploads/How we onboard a client.md
status: draft
---
```

Field rules:

| Field | Required | Rule |
|-------|----------|------|
| `type` | yes | One of the allowed types below |
| `area` | yes | Lowercase path mirroring the folder, e.g. `operations`, `clients`, `people`, `builder` |
| `tags` | yes | List; every tag must be in the allowed tags below (an empty list is allowed) |
| `created` | yes | `YYYY-MM-DD`, never changes after creation |
| `updated` | yes | `YYYY-MM-DD`, bump on every edit |
| `sources` | yes | List of `Raw/` paths or `[[wikilinks]]` this note was compiled from. Empty list allowed only with `status: draft` (lint warns) |
| `status` | yes | `draft` (agent-written, not yet reviewed) or `verified` (an owner has reviewed) |

Files named `about.md` and everything under `Wiki/_meta/` are exempt from front
matter requirements - they are folder descriptions and generated files.

## Allowed types

```types
workflow
concept
reference
person
client
project
vendor
provider
asset
account
integration
builder
```

- **workflow** - a step-by-step procedure (how an inquiry is answered, how an order is fulfilled, how a month is closed)
- **concept** - compiled knowledge on a topic (pricing logic, a policy, how a service is delivered)
- **reference** - lookup data (price list, specifications, templates, checklists)
- **person** - one person (a team member, a contact, a family member): role, contact, preferences, open threads. Lives in `Wiki/People/`
- **client** - one client or customer relationship: who they are, what was agreed, history, open items. Lives in `Wiki/Clients/`
- **project** - one piece of work with a start and an end (an engagement, an order, a build): scope, dates, status. Lives in `Wiki/Clients/`
- **vendor** - one supplier or collaborator the organization buys from or works alongside. Lives in `Wiki/Vendors/`
- **provider** - one service provider the owners use (accountant, insurer, utility, landlord): what they do, contact, cadence. Routed by lane
- **asset** - one owned thing that needs upkeep (equipment, vehicle, device, software licence): model, purchase date, warranty, maintenance. Routed by lane
- **account** - one financial relationship (bank, card, loan, subscription, insurance policy, tax obligation) described **without** any secret (see the rules below). Routed by lane
- **integration** - how an external system connects (CRM, payments, e-sign, mail, calendar, storage). Lives in `Wiki/Integrations/`
- **builder** - a pattern for building or changing the agent itself (build requests, tool notes). Lives in `Wiki/Builder/`

## Allowed tags

```tags
inquiry
quote
booking
contract
invoice
payment
delivery
follow-up
client-comms
vendor-comms
team
hiring
procedure
policy
pricing
legal
marketing
website
social-media
seo
automation
agents
skills
templates
mail
calendar
storage
crm
onboarding
blueprint
business
personal
family
health
goals
routine
home
maintenance
vehicle
insurance
money
bill
subscription
banking
taxes
budget
```

## Routing (machine-read by the agent's note writer)

```routing
type person: People
type client: Clients
type project: Clients
type vendor: Vendors
type integration: Integrations
type builder: Builder
lane business: Operations
lane personal: Personal
lane home: Home
lane money: Money
```

One line per rule: `type <type>: <Folder>` routes a note of that type to `Wiki/<Folder>/`;
`lane <lane>: <Folder>` routes every other type by the lane it belongs to. A lane with no
line here routes to `Wiki/<Lane>/` when that folder exists, else to `Wiki/Operations/`.

## Naming conventions

- Wiki notes: a descriptive Title Case noun phrase - `How We Answer an Inquiry.md`, not `inquiry2.md`
- Client profiles: the client's name as the organization uses it - `Wiki/Clients/<Client Name>.md`
- Project notes: `YYYY-MM-DD <Client> - <Project>` - `Wiki/Clients/2026-03-14 Acme - Spring Launch.md`
- Person profiles: the name the owners use - `Wiki/People/<Name>.md`
- Vendor profiles: business or person name - `Wiki/Vendors/<Name>.md`
- Asset, provider and account profiles: a plain descriptive name, never a number - `Wiki/Operations/Delivery Van.md`, `Wiki/Money/Main Checking.md`
- Daily logs: `Memory/Daily_Logs/YYYY-MM-DD.md`
- Allowed filename characters: letters, digits, space, `-`, `&`, `+`, `'`, `,`, `.`, `(`, `)`, `#`
- No emoji in filenames

## Folder routing

| Content | Destination |
|---------|-------------|
| Procedures, policies, how the business runs | `Wiki/Operations/` |
| Client and customer profiles, projects and orders | `Wiki/Clients/` |
| Supplier and collaborator profiles | `Wiki/Vendors/` |
| Team members, contacts, family and friends | `Wiki/People/` |
| External systems (CRM, payments, e-sign, mail, calendar, storage) | `Wiki/Integrations/` |
| Building or changing the agent (build requests, tool notes) | `Wiki/Builder/` |
| A lane's own notes (personal, home, money ...) | `Wiki/<Lane>/` |
| Generated index / manifest / log | `Wiki/_meta/` (machine-written, never hand-edit) |

## Lanes

The agent works the lanes listed in `assistant/config.json -> lanes` (the first one is the
business lane). Business knowledge lives in the folders above; every other lane has a folder
of its own named after it (`Wiki/Personal/`, `Wiki/Home/`, `Wiki/Money/` ...). Use `area:` to
match the folder.

Rules:

1. **No secrets, ever - anywhere in `Wiki/`.** No passwords, PINs, security answers, full
   account / card / routing numbers, government identifiers or API tokens. An account is
   identified by institution + nickname (+ last 4 digits at most). Credentials live in the
   operating system's credential store or the agent's vault, never here.
2. **Lanes don't leak.** Personal, home and money notes are never quoted in anything
   client- or vendor-facing.
3. **The money lane never pays, transfers or enters credentials** - notes describe; the owners act.
4. Profiles use the templates in `Schema/templates/`.

## Obsidian formatting

This folder reads well in Obsidian. Prefer wikilinks (`[[Note Name]]`), callouts
(`> [!note]`) and properties over plain Markdown constructs; this schema only defines the
front-matter contract and the routing rules.

## Linking rules

- Use wikilinks: `[[Note Name]]` or `[[Note Name|display text]]`
- Every Wiki note should link to related Wiki notes - orphan notes are a lint warning (client, person and vendor profiles are reference leaves)
- Cite sources in front matter, not just inline
- A link to a note that does not exist yet is allowed (it marks a note worth writing) but lint reports it

## Non-negotiable rules

1. **Never move, rename or delete anything in `Raw/`** - source material is immutable.
2. **Never modify `Memory/Daily_Logs/`** - append-only raw capture, the dreaming source material.
3. **Never hand-edit `Wiki/_meta/`** - it is rebuilt by `wiki_tool.py`.
4. **Everything client- or vendor-facing is a draft** until an owner approves the exact message.
5. **Never commit to a price, a date or contract language** without an owner's say-so.
6. **Log every ingest / maintenance run** to `Wiki/_meta/log.md` (the tool does this).
7. **Log every mistake** to `Schema/mistakes.md` before ending the session.
