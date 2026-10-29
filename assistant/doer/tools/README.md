# Self-built tools (assistant/doer/tools)

Scripts the doer's computer lane (`assistant/doer/computer.py`) wrote for itself while doing a job
an owner asked for, kept so the next time is faster. Each file carries a header
(`# tool / # triggers / # tier / # usage / # needs`); `python assistant/doer/computer.py --index`
rebuilds `tools.json` from the headers and the turn's prompt lists them, matches first.

Every tool passed the static scan in `computer_policy.py` when it was written (hook on Write) and
is scanned again each time it runs (hook on Bash): no sockets, mail, registry, input automation,
credential stores, eval/exec or encoded code; a delete or a move inside a tool makes the run tier C,
so tools keep those behind an `--apply` flag and the dry run stays tier B. Git-tracked: read any of
them, `git revert` any of them. An owner may also drop a hand-written tool here with the same header.
