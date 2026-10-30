---
type: workflow
area: personal
tags: [personal]
created: 2026-10-30
updated: 2026-10-30
sources: []
status: draft
---

# Fix A nightly self-test failed

> [!note] Written by Sage from chat on 2026-10-30. Draft until an owner verifies it.

## Build request

**Ask (the reflex, by code, 2026-10-30 16:50):** Fix: A nightly self-test failed

The system card below stayed open on Mission Control after its own button was pressed by the reflex (or it had none). Card qe0160d195279, finding `-`, lane personal.

> nightly self-test test_phone_bridge FAILED: 3 FAILED

Find the cause in the job, sensor or test it names and fix it so the finding clears by itself; a test encodes the failing path.

**Acceptance:** the finding `card:qe0160d195279` clears on the next reflex pass (python assistant/agent/reflex.py status) and the test encodes the slow or missing path.

**Evidence:** A nightly self-test failed: nightly self-test test_phone_bridge FAILED: 3 FAILED

<!-- reflex: {"finding": "card:qe0160d195279", "kind": "system_card", "first": "2026-10-30 16:50"} -->
