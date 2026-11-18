# Refresh the turn times dashboard

`scripts/turn_report.py` starts a background Claude Code session with this
file as its instructions every workday morning. Nobody is at the terminal
and nobody can answer a question, so do exactly the steps below and nothing
else: no game tools, no other files, no changes to the dashboard's page.

The dashboard is the artifact
`https://claude.ai/artifact/R7iddDEahbRBdTJ3KrGmTD` (the Data type: a fixed
page that draws whatever its one data file holds). The script has just
written a new data file, `scripts/data/turn_report.json`, from the game's
turn log. Your job is to put that file on the dashboard in place of the old
one. The numbers are the script's; do not change, check or summarize them.

1. Read `scripts/data/turn_report.json`. Note the `generated` value in its
   `info` row (an ISO time such as `2026-11-18T09:00:00`).
2. Upload the file to the artifact as an asset: the `Artifact` tool with
   `url` (the artifact above), `file_path` (the data file) and
   `asset: true`. The result gives the new file's address, `/_blob/<id>`.
3. Load the `ArtifactData` tool (ToolSearch, `select:ArtifactData`) and
   `get` the document `report` in the collection `datasets` of the same
   artifact. Note its `version` and its current `source.url` (the old
   file's address).
4. `update` that document, with `if_version` set to the version you read
   and this data, and no other field:
   `{"source": {"url": "<new address>"}, "updated": {"at": "<generated>", "by": "turn_report.py"}}`
5. If the old address differs from the new one, delete the old file: the
   `Artifact` tool with `action: "delete"`, the artifact's `url` and `path`
   set to the old file's id (the 32 characters after `/_blob/`). Delete
   nothing else, and never the artifact itself (a delete without `path`).
6. Write `scripts/data/turn_report_refresh.json` with exactly
   `{"generated": "<generated>", "asset": "<new address>"}`. The script is
   waiting for this file.

If a step fails and a second try fails too, stop there and write
`scripts/data/turn_report_refresh.json` with
`{"generated": "<generated>", "error": "<which step failed and what the tool said>"}`
instead. A failed step 5 is not worth an error: finish with step 6 and
mention the leftover file's id in an extra `"leftover"` field.

Your last message is not read by anyone; the file from step 6 is the result.
