---
name: story-gate
description: "Quality gate for AI coding work. Use it whenever the repository has a .story-gate/ folder and you start, build or finish a feature, bug fix or story; when the user wants proof that AI-written code works before it merges; or when they ask to set up story-gate. Before code: write the story, its acceptance criteria and tests, and pass READY. While coding: run checkpoints that catch drift and scope creep. Before saying done: run the tests, run the feature for every acceptance criterion (scenarios that CI repeats), write a plain-English validation.md and handoff, record learnings, and pass DONE. A human approves on GitHub; you never approve or merge."
---

# story-gate

**Success criteria.** A story:
- starts on defined specs,
- is built and tested against them (AC → test cases → automated tests → results),
- ends on them, complete and delivered,
- and is accepted by a human, not by you.

Follow **`.story-gate/PROTOCOL.md`** step by step. `gate.py` means **`story-gate`**, the verified copy installed on this computer (if `story-gate` isn't found, use the full command story-gate's messages print). Only where story-gate isn't installed (cloud agents, where hooks don't run) use `python3 .story-gate/gate.py` (`python` on Windows). With story-gate installed, the hooks refuse running the repository's copy, because a branch can replace it.

## When the repository has no `.story-gate/` folder

This skill is guidance only. Nothing is protected until setup finishes and `story-gate doctor` says so.

Set story-gate up in the human's own project: the folder they have open. https://github.com/SathiaAI/story-gate is only where story-gate comes from. Never clone it to set up, and never open issues or pull requests there.

1. Tell the human what you will run, and wait for a clear yes:
   - `uv tool install --python 3.12 git+https://github.com/SathiaAI/story-gate@v0.6.2` (if `uv --version` fails, install uv first with its official installer from astral.sh)
   - `story-gate init`, in the project folder
   - If `story-gate` isn't found after the install, run `uv tool update-shell` and use a new shell, or run it by its full path in the folder that `uv tool dir --bin` prints.
2. Run `story-gate init` in the background. It opens a setup page in the browser. Tell the human: "A setup page opened. Follow it; I'll wait."
3. The human signs in to GitHub, creates the AI's own login, adds the judge key and merges the setup pull request. Never do these steps for them, and never see or type the key.
4. When `init` prints that story-gate is protecting the repository, run `story-gate doctor` and report its summary in plain words.

If the human only wants to see what story-gate does, run `story-gate try` after the install instead of `init`. It needs no setup and touches nothing in their projects; tell them it opened a validation page with one passing and one failing goal.

If a step fails, read the error to the human and stop. Never copy story-gate files by hand, and never write hook files yourself.

## Your identity
- Work under the **agent identity**: run `gate.py agent-env --repo owner/name` and use its token and git name.
- Never use the human's GitHub login.
- If `gate.py doctor --repo owner/name` warns that this shell holds a code owner's login, stop and tell the human.

## Sub-agents: tiers, not model names

| Tier | Use it for | Example models (pick what your client offers) |
|---|---|---|
| `small` | Fetch, copy, log | Claude haiku, a Gemini Flash model, a "mini" or "fast" tier model |
| `medium` | Read specs, write test plans and handoffs | Claude sonnet, or your client's standard coding model |
| never | Frontier models are not used for gate work | |

- **If your client can't choose a model for sub-agents** (or has no sub-agents), do the steps yourself, in order.
- **Judging never uses your model:** `gate.py` sends the evidence to the configured judge. That's Jev by default; see https://github.com/SathiaAI/story-gate/blob/main/docs/guide.md#fallbacks for the other options.

## The moments
1. **READY:** on a story branch (e.g. `feat/<ID>-short-name`), run `start <ID> --model <your model id>`, fill `story.md`, `context.md` and `tests.json`, then run `score <ID> ready`.
2. **CHECKPOINT:** runs automatically in clients with after-edit hooks. Otherwise run `gate.py checkpoint <ID>` after each AC. If it says OFF_COURSE, stop and correct the work, or escalate.
3. **DONE:**
   - Run `record-tests`.
   - Set `test_refs` for every AC.
   - Self-review the diff (`/engineering:code-review` in Claude clients).
   - For every AC, run the feature for real and record it: `scenario <ID> --name ... --ac AC-1 --expect <text> -- <command>`. CI runs these again.
   - Fill in `validation.md` for the owner (result, ACs, scenarios, bugs, lessons, limits, demo steps or 'Not demo-able: reason'). Add screenshots with `evidence`.
   - Write `handoff.md` (all seven sections), then run `learn` (`--type error|pattern` also needs `--root-cause` and `--rule`), then run `score <ID> done`.
   - Show the owner: `report <ID> --open` builds the validation page. Show it for a change they can see; otherwise point to validation.md.
   - Any later change to code, tests, `tests.json` or the specs makes the verdict out of date: re-run `record-tests` and re-score.
4. **ACCEPTANCE:** open the PR as the agent. CI re-checks everything, and a code owner approves the latest commit and merges. You never approve or merge.
5. **DRIFT:** never resolved silently.
   - Escalate it.
   - Waivers and decisions you record are only proposals until a code owner approves.

## Rules
- Quote the `gate.py` verdict line. Never declare a pass yourself.
- Write for a non-coder: short sentences, active voice, plain words, a Mermaid diagram where a picture is clearer (PROTOCOL.md > Writing). This covers chat replies, PR descriptions, the story's plain summary, validation.md, handoffs and learnings.
- Never edit `.story-gate` code, config or verdicts, CODEOWNERS or the story-gate workflows, by any route.
- Never run `install`, `install --user`, `uninstall`, `enroll`, `unenroll`, `upgrade`, `rollback`, `release-sign`, `setup-repo`, `setup-agent`, `judge-calibrate`, `filter`, `lockdown` or `hook-trust`, and never touch the story-gate runtime, your tool's user hook settings or git's filter settings. Those are for the human.
- The judge gives scores, not reasons. For each failing check, explain the likely cause in one line.
- If the judge is unavailable, say so. Nothing passes without it.
- In Cowork, Cursor Cloud and Codex cloud, hooks don't run: run `gate.py status` before editing and run the checkpoints yourself. CI is the backstop.

## Claude / Cowork specifics
- **Judge key:** local verdicts read the key from the environment variable that `judge.api_key_env` names (default per provider, e.g. `OPENROUTER_API_KEY`). If it isn't set, they read `judge.env` in the human's story-gate user folder, or the file that `STORY_GATE_ENV_FILE` names. Never ask for the key and never copy it. With no key (common in cloud shells), local verdicts can't PASS. CI still judges, using the repo secret.
- **Sub-agents:** use the Agent tool, with `model: haiku` for `small` and `model: sonnet` for `medium`. Sub-agents write only their evidence file.
- **Drift with real options:** give the human plain-English options, each with pros and cons, and your recommendation.
- **Report:**
  - The verdict line.
  - A table of check → why → fix, only if the verdict isn't PASS.
  - One next step.
