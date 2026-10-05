<!-- story-gate:start -->
## Story gate (required for any code change)
Every code change belongs to a story and passes the story gate. Full steps: `.story-gate/PROTOCOL.md`.
Run story-gate as `story-gate <command>`: that is the verified copy installed on this computer. If `story-gate` isn't
found, use the full command a story-gate message prints. Only where story-gate isn't installed (cloud agents, where hooks
don't run) use `C:\Users\pjpou\AppData\Roaming\uv\tools\story-gate\Scripts\python.exe .story-gate/gate.py <command>`.
1. Before editing code: `story-gate start <STORY-ID>`, fill the story folder, then `story-gate score <STORY-ID> ready`.
2. Drift between story and PRD/TRD is never resolved silently: escalate, then record `story-gate decide`.
3. Before saying you are done: run tests via `story-gate record-tests`, run the feature for every acceptance criterion with `story-gate scenario`, fill in `validation.md` and `handoff.md`, record learnings with `story-gate learn`, then `story-gate score <STORY-ID> done`. Show the owner the page from `story-gate report <STORY-ID> --open`.
4. Read past learnings first: `story-gate learnings <keywords>`.
5. Write for a non-coder: short sentences, active voice, plain words, and a Mermaid diagram where a picture is clearer (`.story-gate/PROTOCOL.md` > Writing). This covers replies, PR descriptions, story summaries, validation.md, handoffs and learnings.
Mode is in `.story-gate/config.json` (warn = report only, enforce = block).
<!-- story-gate:end -->
