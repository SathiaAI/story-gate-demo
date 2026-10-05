"""story-gate dashboard: one honest picture of every story, built from the records on every branch.

Where it shows up (all inside the repository, so it follows the repository's own permissions):
  - a pinned "Story-gate dashboard" issue, refreshed by .github/workflows/story-gate-dashboard.yml
  - a full report (self-contained HTML, Tabler styles, Viaknox colours) attached to each run as an artifact
  - `gate.py dashboard --open` on your computer

Rules this module keeps:
  - Branch content is DATA. Records are read as git blobs (`git cat-file`), never checked out or executed, and every
    field is validated, size-capped and escaped before it reaches Markdown or HTML.
  - Every number says how it is calculated. Estimates say "(estimate)". Agent-recorded verdicts are labelled as such;
    CI is what verifies them.
  - Stdlib only. No network except the GitHub API when publishing, with the workflow's own token.
"""
import html, json, os, re, statistics, subprocess, tempfile, time
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = 1
MAX_BLOB = 1_000_000          # bytes per record file
MAX_STORIES = 2000            # stories per snapshot
ISSUE_LIMIT = 55_000          # characters (GitHub's issue body limit is about 65,536)
LABEL = "story-gate-dashboard"
STALE_DAYS = 3
STORY_FILES = ("story.md", "context.md", "tests.json", "ready.json", "done.json", "checkpoints.jsonl", "coder.json", "decisions.jsonl",
               "trace.md", "test_results.json")
STATUSES = [  # order matters: this is the pipeline, left to right
    ("draft", "Draft", "READY not passed yet"),
    ("queued", "Queued", "READY passed, nobody has started it"),
    ("in_progress", "In progress", "claimed by an agent, DONE not passed"),
    ("blocked", "Blocked", "drift escalated, or the last checkpoint said OFF_COURSE"),
    ("in_review", "In review", "DONE passed on a branch, not merged yet"),
    ("done", "Done", "DONE passed on the default branch (merged)"),
]
PASSING = ("PASS",)


# ------------------------------------------------------------------ git access (blobs only)
class Repo:
    def __init__(self, root):
        self.root = str(root)

    def git(self, *args, text=True):
        r = subprocess.run(["git", *args], cwd=self.root, capture_output=True)
        if r.returncode != 0:
            return "" if text else b""
        return r.stdout.decode("utf-8", "replace") if text else r.stdout

    def refs(self, default_ref, include_local=False):
        out, seen = [], set()
        sha = self.git("rev-parse", "--verify", "-q", default_ref + "^{commit}").strip()
        if sha:
            out.append((default_ref, sha)); seen.add(sha)
        pats = ["refs/remotes/origin"] + (["refs/heads"] if include_local else [])
        for line in self.git("for-each-ref", "--format=%(refname:short) %(objectname)", *pats).splitlines():
            name, _, s = line.partition(" ")
            if not s or name.endswith("/HEAD") or name == default_ref:
                continue
            out.append((name, s))
        return out

    def blobs(self, specs):
        """{spec: bytes or None} for 'ref:path' specs, via one `git cat-file --batch` (no checkout, nothing executed)."""
        if not specs:
            return {}
        inp = ("\n".join(specs) + "\n").encode("utf-8")
        r = subprocess.run(["git", "cat-file", "--batch"], cwd=self.root, input=inp, capture_output=True)
        data, pos, out = r.stdout, 0, {}
        for spec in specs:
            nl = data.find(b"\n", pos)
            if nl < 0:
                break
            header = data[pos:nl].decode("utf-8", "replace").split()
            pos = nl + 1
            if len(header) == 3 and header[1] == "blob":
                size = int(header[2])
                out[spec] = data[pos:pos + size] if size <= MAX_BLOB else None
                pos += size + 1
            else:
                out[spec] = None
        return out

    def ls_stories(self, ref):
        names = self.git("ls-tree", "-r", "--name-only", "-z", ref, "--", ".story-gate/stories").split("\0")
        by = {}
        for n in names:
            parts = n.split("/")
            if len(parts) == 4 and parts[3] in STORY_FILES:
                by.setdefault(parts[2], []).append(parts[3])
        return by

    def changed_stories(self, base, ref):
        names = self.git("diff", "--name-only", "-z", "%s...%s" % (base, ref), "--", ".story-gate/stories").split("\0")
        return {n.split("/")[2] for n in names if n.count("/") >= 3}

    def last_dates(self, ref):
        """{story_id: latest commit time touching it} and {path: latest commit time} on ref."""
        out, files, cur = {}, {}, None
        log = self.git("log", "-n", "3000", "--format=@%cI", "--name-only", ref, "--", ".story-gate/stories")
        for line in log.splitlines():
            if line.startswith("@"):
                cur = line[1:]
            elif line.startswith(".story-gate/stories/") and cur:
                files.setdefault(line, cur)
                parts = line.split("/")
                if len(parts) >= 3:
                    out.setdefault(parts[2], cur)
        return out, files


# ------------------------------------------------------------------ validation
def clean(s, n=200):
    s = s if isinstance(s, str) else ("" if s is None else str(s))
    s = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", s)
    return s[:n]


def as_json(raw):
    if raw is None:
        return None
    try:
        v = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None
    return v


def jsonl_rows(raw, limit=500):
    rows = []
    for line in (raw or b"").decode("utf-8", "replace").splitlines()[-limit:]:
        try:
            v = json.loads(line)
        except ValueError:
            continue
        if isinstance(v, dict):
            rows.append(v)
    return rows


def front(text):
    m = re.match(r"\s*---\s*\n(.*?)\n---", text, re.S)
    out = {}
    for line in (m.group(1).splitlines() if m else []):
        if ":" in line:
            k, v = line.split(":", 1)
            v = v.strip()
            if v.startswith("[") and v.endswith("]"):
                v = [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
            out[k.strip()] = v
    return out


def verdict(v):
    if not isinstance(v, dict) or v.get("overall") not in ("PASS", "CONCERNS", "FAIL", "ESCALATED", "WAIVED"):
        return None
    checks = v.get("checks") if isinstance(v.get("checks"), dict) else {}
    return {"overall": v["overall"], "drift": clean(v.get("drift"), 40), "judge": clean(v.get("judge"), 30), "inputs_hash": clean(v.get("inputs_hash"), 20),
            "at": clean(v.get("at"), 30), "cost": v.get("cost") if isinstance(v.get("cost"), (int, float)) and not isinstance(v.get("cost"), bool) else None,
            "waived": sum(1 for c in checks.values() if isinstance(c, dict) and c.get("status") == "WAIVED")}


def trace_acs(text):
    """[(ac_id, verified_bool or None)] from trace.md rows."""
    out = []
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 6 and re.fullmatch(r"AC-[\w.-]+", cells[0]):
            res = cells[5]
            if res in ("GREEN",) or (res and "=" in res and all(p.split("=")[-1] == "passed" for p in res.split(", "))):
                out.append((cells[0], True))
            elif res in ("—", "", "NOT RUN"):
                out.append((cells[0], None))
            else:
                out.append((cells[0], False))
    return out


def parse_story(sid, files):
    """files: {name: bytes or None}. Returns a validated, display-safe dict."""
    st = (files.get("story.md") or b"").decode("utf-8", "replace")
    fm = front(st)
    tests = as_json(files.get("tests.json"))
    acs = []
    if isinstance(tests, dict) and isinstance(tests.get("acceptance_criteria"), list):
        for a in tests["acceptance_criteria"][:200]:
            if isinstance(a, dict):
                acs.append(clean(a.get("id"), 30))
    cps = jsonl_rows(files.get("checkpoints.jsonl"))
    last_cp = cps[-1] if cps else {}
    coder = as_json(files.get("coder.json"))
    coder = coder if isinstance(coder, dict) else None
    decisions = jsonl_rows(files.get("decisions.jsonl"))
    tr = as_json(files.get("test_results.json"))
    title = clean(fm.get("title"), 120)
    feature = clean(fm.get("feature"), 40)
    return {
        "id": sid,
        "title": "" if title in ("", "TODO") else title,
        "feature": "" if feature.lower() in ("", "none", "todo") else feature,
        "acs": len(acs),
        "ready": verdict(as_json(files.get("ready.json"))),
        "done": verdict(as_json(files.get("done.json"))),
        "checkpoint": {"status": clean(last_cp.get("status"), 20), "percent": last_cp.get("percent") if isinstance(last_cp.get("percent"), int) else None,
                       "drift": clean(last_cp.get("drift"), 40), "at": clean(last_cp.get("at"), 30)} if last_cp else None,
        "coder": {"client": clean(coder.get("client"), 30), "model": clean(coder.get("model"), 60), "branch": clean(coder.get("branch"), 120),
                  "claimed_at": clean(coder.get("claimed_at") or coder.get("at"), 30)} if coder else None,
        "drift_decisions": [clean(d.get("drift"), 10) for d in decisions if d.get("kind") == "drift"][-5:],
        "trace": trace_acs((files.get("trace.md") or b"").decode("utf-8", "replace")),
        "tests_green": (tr.get("exit_code") == 0) if isinstance(tr, dict) and "exit_code" in tr else None,
    }


# ------------------------------------------------------------------ build the snapshot
def ready_ok(s):
    """READY counts only when it passed outright (a waiver is not evidence) AND still matches the story it was scored on."""
    return (s.get("ready") or {}).get("overall") in PASSING and s.get("ready_fresh") is not False


def status_of(s, merged):
    r, d, cp = s.get("ready") or {}, s.get("done") or {}, s.get("checkpoint") or {}
    if merged and d.get("overall") in ("PASS", "WAIVED"):
        return "done"  # finished; WAIVED ones are flagged and never count as proven
    if r.get("overall") == "ESCALATED" or d.get("overall") == "ESCALATED" or cp.get("status") == "OFF_COURSE":
        return "blocked"
    if d.get("overall") in ("PASS", "WAIVED"):
        return "in_review"
    if s.get("coder"):
        return "in_progress"
    if ready_ok(s):
        return "queued"
    return "draft"


def iso_days(a, b):
    try:
        fa = datetime.fromisoformat(a.replace("Z", "+00:00")); fb = datetime.fromisoformat(b.replace("Z", "+00:00"))
        return (fb - fa).total_seconds() / 86400
    except Exception:
        return None


def build(root, default_ref, id_pattern, include_local=False, now=None, gate=None):
    now = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    repo = Repo(root)
    refs = repo.refs(default_ref, include_local)
    omissions, stories, conflicts = [], {}, []
    if not refs:
        return {"schema": SCHEMA, "generated_at": now, "error": "default branch %s not found" % default_ref, "stories": [], "metrics": []}
    idre = re.compile(r"(?:%s)" % id_pattern)
    calib, learn = {}, {}
    features = {}
    for i, (ref, sha) in enumerate(refs):
        merged = i == 0
        listing = repo.ls_stories(ref)
        ids = set(listing) if merged else (repo.changed_stories(refs[0][0], ref) & set(listing))
        bad = [x for x in ids if not idre.fullmatch(x)]
        if bad:
            omissions.append("%s: ignored %d folder(s) whose names aren't story ids" % (ref, len(bad)))
        ids = sorted(x for x in ids if idre.fullmatch(x))
        specs = ["%s:.story-gate/stories/%s/%s" % (sha, sid, f) for sid in ids for f in listing.get(sid, [])]
        specs += ["%s:.story-gate/%s" % (sha, f) for f in ("calibration.jsonl", "learnings.jsonl", "features.json")]
        spec_names = []
        if gate is not None:  # PRD/TRD at this ref, to check READY verdicts against the specs they were scored on
            try:
                pc = gate.cfg()
                spec_names = sorted(set(list(pc.get("spec_files") or []) + [x[k] for x in pc.get("sources") or [] if x.get("type") == "repo" for k in ("prd", "trd") if x.get(k)]))
            except Exception:
                pc = None
            specs += ["%s:%s" % (sha, f) for f in spec_names]
        blobs = repo.blobs(specs)
        too_big = [s for s, b in blobs.items() if b is None and s.split(":", 1)[1].split("/")[-1] in STORY_FILES and s in specs]
        dates, file_dates = repo.last_dates(sha)
        for row in jsonl_rows(blobs.get("%s:.story-gate/calibration.jsonl" % sha), 20000):
            calib[json.dumps(row, sort_keys=True)] = row
        for row in jsonl_rows(blobs.get("%s:.story-gate/learnings.jsonl" % sha), 20000):
            learn[clean(row.get("id"), 60) or json.dumps(row, sort_keys=True)] = row
        fj = as_json(blobs.get("%s:.story-gate/features.json" % sha))
        if isinstance(fj, dict):
            for k, v in list(fj.items())[:500]:
                if isinstance(k, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}", k):
                    features.setdefault(k, clean((v or {}).get("title") if isinstance(v, dict) else "", 120) or k)
        for sid in ids:
            if len(stories) >= MAX_STORIES:
                omissions.append("more than %d stories: the rest are not shown" % MAX_STORIES); break
            files = {f: blobs.get("%s:.story-gate/stories/%s/%s" % (sha, sid, f)) for f in listing.get(sid, [])}
            s = parse_story(sid, files)
            if gate is not None and pc is not None and s.get("ready") and s["ready"].get("inputs_hash"):
                txt = lambda b: (b or b"").decode("utf-8", "ignore")  # exactly how gate.py's rd() reads the same files
                pairs = [(f, txt(blobs.get("%s:%s" % (sha, f)))) for f in spec_names if blobs.get("%s:%s" % (sha, f)) is not None]
                try:
                    s["ready_fresh"] = s["ready"].get("inputs_hash") == gate.ready_hash_from(
                        txt(files.get("story.md")), txt(files.get("context.md")), txt(files.get("tests.json")), pairs, pc)
                except Exception:
                    s["ready_fresh"] = None
            s["ref"] = ref
            s["merged"] = merged
            s["last_reported"] = dates.get(sid)
            s["done_at"] = file_dates.get(".story-gate/stories/%s/done.json" % sid) if merged else None
            s["status"] = status_of(s, merged)
            prev = stories.get(sid)
            if prev is None:
                stories[sid] = s
                continue
            if prev["merged"] and prev["status"] == "done":
                continue  # merged and done on the default branch: a stale branch can't reopen it
            if not prev["merged"] and prev.get("coder") and s.get("coder") and \
                    (prev["coder"].get("branch"), prev["coder"].get("client")) != (s["coder"].get("branch"), s["coder"].get("client")):
                conflicts.append({"story": sid, "branches": sorted({prev["ref"], ref})})
            if prev["merged"] or (s.get("last_reported") or "") > (prev.get("last_reported") or ""):
                stories[sid] = s
        if too_big:
            omissions.append("%s: %d record file(s) over %d bytes were skipped" % (ref, len(too_big), MAX_BLOB))
    rows = sorted(stories.values(), key=lambda s: ([k for k, _, _ in STATUSES].index(s["status"]), s["id"]))
    for s in rows:
        if s["feature"]:
            features.setdefault(s["feature"], s["feature"])
        lr = s.get("last_reported")
        s["stale"] = bool(s["status"] in ("in_progress", "blocked") and lr and (iso_days(lr, now) or 0) > STALE_DAYS)
    data = {"schema": SCHEMA, "generated_at": now, "default_ref": refs[0][0], "default_sha": refs[0][1],
            "refs_scanned": [{"ref": r, "sha": s[:12]} for r, s in refs], "omissions": omissions, "conflicts": conflicts,
            "features": [{"id": k, "title": v} for k, v in sorted(features.items())], "stories": rows}
    data["metrics"] = metrics(rows, list(calib.values()), list(learn.values()), conflicts, now)
    return data


def pct(a, b):
    return round(100.0 * a / b) if b else None


def metrics(rows, calib, learn, conflicts, now):
    by = {k: [s for s in rows if s["status"] == k] for k, _, _ in STATUSES}
    total = len(rows)
    dor = [s for s in rows if ready_ok(s)]
    active = by["in_progress"] + by["blocked"]
    finished = by["done"] + by["in_review"]
    ac_tot = ac_ok = ac_unknown = 0
    for s in finished:
        if (s.get("done") or {}).get("overall") == "WAIVED":
            ac_unknown += s["acs"]; continue  # a waiver is not test evidence
        if not s["trace"]:
            ac_unknown += s["acs"]
        for _, ok in s["trace"]:
            ac_tot += 1
            ac_ok += 1 if ok else 0
            ac_unknown += 1 if ok is None else 0
    est = [s["checkpoint"]["percent"] for s in active if s.get("checkpoint") and isinstance(s["checkpoint"].get("percent"), int)]
    scored = [r for r in calib if r.get("phase") in ("ready", "done") and r.get("overall")]
    scored.sort(key=lambda r: str(r.get("at")))
    first_ready, catches, done_runs = {}, 0, {}
    done_ids = {s["id"] for s in finished}
    for r in scored:
        sid = r.get("story")
        if r["phase"] == "ready":
            first_ready.setdefault(sid, r["overall"])
        if sid in done_ids and r["overall"] in ("FAIL", "CONCERNS", "ESCALATED"):
            catches += 1
        if r["phase"] == "done":
            done_runs[sid] = done_runs.get(sid, 0) + 1
    lead = [d for d in (iso_days(s["coder"]["claimed_at"], s["done_at"]) for s in by["done"] if s.get("coder") and s.get("done_at")) if d is not None and d >= 0]
    drifting = [s for s in active + by["queued"] if any(x and x != "none" for x in ((s.get("ready") or {}).get("drift"), (s.get("checkpoint") or {}).get("drift")))]
    cost = sum((s.get(p) or {}).get("cost") or 0 for s in rows for p in ("ready", "done"))
    M = []
    def add(key, label, value, formula, unit="", estimate=False, good=None):
        M.append({"key": key, "label": label, "value": value, "unit": unit, "formula": formula, "estimate": estimate, "good": good})
    add("features", "Features", len({s["feature"] for s in rows if s["feature"]} | set()), "distinct feature ids on stories")
    add("stories", "Stories", total, "story folders on the default branch plus stories changed on other branches")
    add("dor_met", "Meet Definition of Ready", len(dor), "stories whose READY verdict is PASS and still matches the story, tests and specs it was scored on (waivers don't count)")
    add("ready_stale", "READY out of date", sum(1 for s in rows if s.get("ready_fresh") is False), "READY verdicts recorded on a story, test plan, spec or policy that has since changed", good="low")
    add("waived", "Finished with waivers", sum(1 for s in finished if (s.get("done") or {}).get("overall") == "WAIVED" or (s.get("done") or {}).get("waived")), "finished stories whose DONE relied on a human waiver", good="low")
    add("dor_pct", "Ready rate", pct(len(dor), total), "Meet Definition of Ready / Stories", "%")
    add("queued", "Queued to start", len(by["queued"]), "READY passed and nobody has claimed it")
    add("in_progress", "In progress", len(by["in_progress"]), "claimed by an agent, DONE not passed")
    add("blocked", "Blocked", len(by["blocked"]), "drift escalated, or the last checkpoint was OFF_COURSE", good="low")
    add("agents", "Agents working", len({(s["coder"].get("client"), s["coder"].get("model")) for s in active if s.get("coder")}), "distinct client + model on claimed, unfinished stories")
    add("drifting", "Stories drifting", len(drifting), "unfinished stories whose READY or last checkpoint reports drift", good="low")
    add("done", "Done (merged)", len(by["done"]), "DONE passed on the default branch")
    add("in_review", "In review", len(by["in_review"]), "DONE passed on a branch, not merged yet")
    add("ac_verified", "Acceptance criteria proven", pct(ac_ok, ac_tot), "criteria whose mapped tests all passed / criteria traced, finished stories only (%d not traced)" % ac_unknown, "%")
    add("ac_progress", "Progress on active stories", round(statistics.mean(est)) if est else None, "average of the last checkpoint's % complete", "%", estimate=True)
    add("gate_catches", "Gate catches before merge", catches, "re-scores of finished stories that came back FAIL, CONCERNS or ESCALATED before passing")
    add("defects", "Defects recorded", sum(1 for r in learn if r.get("type") == "error"), "learnings of type error (one per confirmed defect)")
    add("first_try", "Ready on first try", pct(sum(1 for v in first_ready.values() if v == "PASS"), len(first_ready)), "stories whose first READY score passed / stories scored", "%")
    add("rework", "DONE attempts per story", round(statistics.mean(done_runs[s] for s in done_ids if s in done_runs), 1) if any(s in done_runs for s in done_ids) else None, "average DONE scores until it passed, finished stories")
    add("lead_time", "Days from claim to merge", round(statistics.median(lead), 1) if lead else None, "median, merged stories", "days")
    add("false_alarms", "Verdicts marked wrong", sum(1 for r in calib if r.get("label") == "wrong"), "gate.py label ... wrong (a human said the gate got it wrong)", good="low")
    add("stale", "Stale claims", sum(1 for s in rows if s.get("stale")), "active stories with no new record for %d days" % STALE_DAYS, good="low")
    add("conflicts", "Ownership conflicts", len(conflicts), "the same story claimed on two branches by different agents", good="low")
    add("judge_cost", "Judge cost (latest verdicts)", round(cost, 4), "sum of the cost reported on each story's latest READY and DONE verdicts", "$")
    return M


# ------------------------------------------------------------------ Markdown (the pinned issue)
def md(s, n=80):
    s = clean(s, n).replace("\n", " ")
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    s = re.sub(r"([\\`*_\[\]|#!~])", r"\\\1", s)
    return s.replace("@", "&#64;")


def fmt(m):
    v = m["value"]
    if v is None:
        return "—"
    return ("$%s" % v if m["unit"] == "$" else "%s%s" % (v, "%" if m["unit"] == "%" else (" " + m["unit"] if m["unit"] else ""))) + (" (estimate)" if m["estimate"] else "")


def ci_cell(s):
    ci = s.get("ci")
    if not ci:
        return "no PR"
    return "#%s %s" % (ci.get("pr"), md(ci.get("result"), 20))


def to_markdown(d, artifact_url=None, limit=ISSUE_LIMIT):
    M = {m["key"]: m for m in d.get("metrics", [])}
    head = ["<!-- story-gate-dashboard generated_at=%s schema=%s -->" % (d.get("generated_at"), d.get("schema")),
            "# Story-gate dashboard", "",
            "Updated **%s** from `%s` at `%s` and %d other branch(es)." % (md(d.get("generated_at"), 30), md(d.get("default_ref"), 60),
                                                                        md((d.get("default_sha") or "")[:12], 12), max(0, len(d.get("refs_scanned", [])) - 1))]
    if artifact_url:
        head.append("Full report: [download the HTML dashboard](%s) (repository members only; kept 30 days, a new one comes with every refresh)." % artifact_url)
    head += ["", "| | |", "|---|---|"]
    for k in ("features", "stories", "dor_met", "queued", "in_progress", "blocked", "in_review", "done", "ac_verified", "gate_catches", "defects"):
        if k in M:
            head.append("| %s | **%s** |" % (M[k]["label"], fmt(M[k])))
    counts = [(lab, len([s for s in d["stories"] if s["status"] == k])) for k, lab, _ in STATUSES]
    chart = ["", "```mermaid", "pie showData", "    title Stories by stage"] + ['    "%s" : %d' % (lab, n) for lab, n in counts if n] + ["```"]
    agents = ["", "## Who is working on what", "", "| Story | Agent | Model | Status | Done | Drift | CI check | Last report |", "|---|---|---|---|---|---|---|---|"]
    act = [s for s in d["stories"] if s["status"] in ("in_progress", "blocked", "in_review")]
    for s in act:
        c, cp = s.get("coder") or {}, s.get("checkpoint") or {}
        agents.append("| %s %s | %s | %s | %s%s | %s | %s | %s | %s%s |" % (
            md(s["id"], 40), md(s["title"], 50), md(c.get("client") or "?", 20), md(c.get("model") or "?", 30), dict((k, l) for k, l, _ in STATUSES)[s["status"]],
            " (%s)" % md(cp.get("status"), 12) if cp.get("status") else "",
            "%s%% (estimate)" % cp["percent"] if isinstance(cp.get("percent"), int) else "—",
            md(cp.get("drift") or (s.get("ready") or {}).get("drift") or "none", 25), ci_cell(s), md((s.get("last_reported") or "—")[:16], 16), " ⚠ stale" if s.get("stale") else ""))
    if not act:
        agents.append("| — | | | | | | | |")
    queued = ["", "## Queued to start", ""] + ["- %s %s%s" % (md(s["id"], 40), md(s["title"], 70), " · feature %s" % md(s["feature"], 30) if s["feature"] else "")
                                               for s in d["stories"] if s["status"] == "queued"] or ["- none"]
    if queued[-1:] == [""]:
        queued.append("- none")
    feats = ["", "## Features", "", "| Feature | Stories | Done | Ready | Acceptance criteria proven |", "|---|---|---|---|---|"]
    for f in d.get("features", []):
        ss = [s for s in d["stories"] if s["feature"] == f["id"]]
        tr = [ok for s in ss if s["status"] in ("done", "in_review") for _, ok in s["trace"]]
        feats.append("| %s %s | %d | %d | %d | %s |" % (md(f["id"], 40), md(f["title"], 60) if f["title"] != f["id"] else "", len(ss),
                                                    sum(s["status"] == "done" for s in ss), sum((s.get("ready") or {}).get("overall") == "PASS" for s in ss),
                                                    "%d%%" % pct(sum(1 for x in tr if x), len(tr)) if tr else "—"))
    defs = ["", "<details><summary>How each number is calculated</summary>", ""] + \
           ["- **%s**: %s." % (m["label"], md(m["formula"], 200)) for m in d.get("metrics", [])] + \
           ["", "Verdicts are recorded by the coding agents. The `story-gate` check in CI re-checks them on every pull request.", "</details>"]
    notes = []
    if d.get("conflicts"):
        notes += ["", "**Ownership conflicts:** " + "; ".join("%s on %s" % (md(c["story"], 40), ", ".join(md(b, 60) for b in c["branches"])) for c in d["conflicts"][:20])]
    if d.get("omissions"):
        notes += ["", "**Not shown:** " + "; ".join(md(o, 160) for o in d["omissions"][:10])]
    stories = ["", "## All stories", "", "| Story | Feature | Stage | READY | DONE | Branch |", "|---|---|---|---|---|---|"]
    for s in d["stories"]:
        stories.append("| %s %s | %s | %s | %s | %s | %s |" % (md(s["id"], 40), md(s["title"], 50), md(s["feature"] or "—", 30), dict((k, l) for k, l, _ in STATUSES)[s["status"]],
                                                         (s.get("ready") or {}).get("overall", "—") + (" (out of date)" if s.get("ready_fresh") is False else ""),
                                                         (s.get("done") or {}).get("overall", "—"), md(s["ref"], 60)))
    footer = ["", "_Managed by story-gate. Edits to this issue are overwritten._"]
    table_chart = ["", "| Stage | Stories |", "|---|---|"] + ["| %s | %d |" % (lab, n) for lab, n in counts]
    # Fixed order on the page; when space runs out, sections are kept by priority (deterministic):
    # summary > features > who is working > queued > chart (falls back to a table) > notes > definitions > all stories.
    order = ["head", "chart", "agents", "queued", "feats", "notes", "defs", "stories"]
    sections = {"head": head, "chart": chart, "agents": agents, "queued": queued, "feats": feats, "notes": notes, "defs": defs, "stories": stories}
    priority = ["head", "feats", "agents", "queued", "chart", "notes", "defs", "stories"]
    budget = limit - len("\n".join(footer)) - 300
    kept, used = {}, 0
    for name in priority:
        lines = sections[name]
        size = len("\n".join(lines)) + 1
        if used + size <= budget:
            kept[name] = lines; used += size; continue
        if name == "chart" and used + len("\n".join(table_chart)) + 1 <= budget:
            kept[name] = table_chart; used += len("\n".join(table_chart)) + 1; continue
        if name in ("stories", "agents", "feats", "queued"):  # long lists: keep as many rows as fit
            part, room = [], budget - used
            for line in lines:
                if room - len(line) - 1 < 120:
                    part.append("| … %d more rows in the full report |" % (len(lines) - len(part)) if line.startswith("|") else "- … more in the full report")
                    break
                part.append(line); room -= len(line) + 1
            kept[name] = part; used = budget - room
    body = "\n".join(sum((kept[n] for n in order if n in kept), []))
    return body + "\n" + "\n".join(footer)


# ------------------------------------------------------------------ HTML (the full report)
def h(s, n=200):
    return html.escape(clean(s, n), quote=True)


def bar_chart(counts):
    """Horizontal bars, one hue (magnitude), blocked in the critical colour with a label, value labels at the bar end."""
    maxv = max([n for _, _, n, _ in counts] + [1])
    rowh, w, lab = 34, 420, 96
    out = ['<svg class="sg-chart" viewBox="0 0 %d %d" role="img" aria-label="Stories by stage">' % (w, rowh * len(counts) + 8)]
    for i, (key, label, n, hint) in enumerate(counts):
        y = 4 + i * rowh
        bw = 0 if not n else max(6, (w - lab - 40) * n / maxv)
        cls = "sg-bar-critical" if key == "blocked" else "sg-bar"
        out.append('<g><title>%s: %d (%s)</title><text x="0" y="%d" class="sg-axis">%s</text>'
                   '<rect x="%d" y="%d" width="%.1f" height="%d" rx="4" class="%s"></rect>'
                   '<text x="%.1f" y="%d" class="sg-value">%d</text></g>'
                   % (h(label), n, h(hint), y + 21, h(label), lab, y + 6, bw, rowh - 12, cls, lab + bw + 8, y + 21, n))
    out.append("</svg>")
    return "".join(out)


WORDMARK_FILE = Path(__file__).resolve().parent / "vendor" / "viaknox-wordmark.svg"
TABLER_FILE = Path(__file__).resolve().parent / "vendor" / "tabler.min.css"

BRAND_CSS = """
:root{--sg-accent:#FFD84D;--sg-accent-text:#8A6A00;--sg-graphite:#1F2327;--sg-paper:#FFF8F2;--sg-ink:#1F2327;--sg-muted:#5B6168;--sg-surface:#FFF8F2;--sg-card:#FFFFFF;
--sg-line:#E4E1DB;--sg-critical:#B42318;--sg-wordmark:#29122B;--tblr-primary:#1F2327;--tblr-body-bg:#FFF8F2;--tblr-body-color:#1F2327}
@media (prefers-color-scheme: dark){:root{--sg-ink:#F1F5F2;--sg-muted:#B9C0C6;--sg-surface:#16191C;--sg-card:#1F2327;--sg-line:#3A4046;--sg-accent-text:#FFD84D;
--sg-critical:#FF8A80;--sg-wordmark:#FFF8F2;--tblr-primary:#FFD84D;--tblr-body-bg:#16191C;--tblr-body-color:#F1F5F2}}
body{background:var(--sg-surface);color:var(--sg-ink)}
h1,h2,h3,.h1,.card-title,strong{color:var(--sg-ink)} .card-header{border-color:var(--sg-line)}
.table{--tblr-table-bg:transparent;--tblr-table-color:var(--sg-ink);--tblr-table-border-color:var(--sg-line)}
.table thead th{background:transparent;color:var(--sg-muted);border-color:var(--sg-line)}
.alert{background:var(--sg-card);color:var(--sg-ink);border:1px solid var(--sg-line);border-left:4px solid var(--sg-accent)}
.card{background:var(--sg-card);border-color:var(--sg-line)} .table{color:var(--sg-ink)} .text-secondary{color:var(--sg-muted)!important}
.sg-kpi .h1{font-weight:700;letter-spacing:-.01em;margin:0} .sg-kpi .subheader{color:var(--sg-muted)}
.sg-chart{width:100%;height:auto} .sg-bar{fill:var(--sg-accent);stroke:var(--sg-graphite);stroke-width:1.5} .sg-bar-critical{fill:var(--sg-critical)}
.sg-axis,.sg-value{fill:var(--sg-ink);font-size:14px} .sg-value{font-weight:600}
.sg-pill{display:inline-block;padding:.1rem .5rem;border-radius:999px;border:1px solid var(--sg-line);font-size:.8rem;white-space:nowrap}
.sg-pill.pass{border-color:#2E7D4F} .sg-pill.fail{border-color:var(--sg-critical)} .sg-est{color:var(--sg-muted);font-size:.8rem}
.sg-brand{color:var(--sg-ink);display:inline-flex;align-items:center;gap:.4rem} .sg-brand svg{height:14px;width:auto;color:var(--sg-wordmark)}
header.sg-head{border-bottom:3px solid var(--sg-accent)}
@media (max-width:640px){.container-xl{padding-left:16px;padding-right:16px}}
"""


def pill(v):
    if not v:
        return '<span class="sg-pill">—</span>'
    cls = "pass" if v in ("PASS",) else ("fail" if v in ("FAIL", "ESCALATED") else "")
    icon = {"PASS": "✓ ", "FAIL": "✕ ", "ESCALATED": "! ", "CONCERNS": "~ ", "WAIVED": "w "}.get(v, "")
    return '<span class="sg-pill %s">%s%s</span>' % (cls, icon, h(v, 12))


def pill_level(text):
    kind = "pass" if text.startswith("hard:") else ("fail" if text == "none" else "warn")  # hard-on-change is not a hard stop
    return '<span class="sg-pill %s">%s</span>' % (kind, h(text, 120))


def to_html(d, artifact_note=""):
    M = d.get("metrics", [])
    label = dict((k, l) for k, l, _ in STATUSES)
    counts = [(k, l, len([s for s in d["stories"] if s["status"] == k]), hint) for k, l, hint in STATUSES]
    css = TABLER_FILE.read_text(encoding="utf-8") if TABLER_FILE.is_file() else ""
    mark = WORDMARK_FILE.read_text(encoding="utf-8") if WORDMARK_FILE.is_file() else ""
    mark = re.sub(r'fill="#[0-9A-Fa-f]{6}"', 'fill="currentColor"', re.sub(r"<title>.*?</title>", "", mark)) if mark else ""
    kpi = "".join('<div class="col-6 col-md-4 col-xl-2"><div class="card sg-kpi"><div class="card-body"><div class="subheader" title="%s">%s</div>'
                  '<div class="h1">%s</div>%s</div></div></div>' % (h(m["formula"], 300), h(m["label"]), h(fmt(m).replace(" (estimate)", ""), 40),
                                                                     '<div class="sg-est">estimate</div>' if m["estimate"] else "")
                  for m in M if m["key"] in ("stories", "dor_met", "queued", "in_progress", "done", "ac_verified"))
    quality = "".join('<tr><td>%s%s</td><td class="text-end"><strong>%s</strong></td><td class="text-secondary">%s</td></tr>'
                      % (h(m["label"]), ' <span class="sg-est">(estimate)</span>' if m["estimate"] else "", h(fmt(m).replace(" (estimate)", ""), 40), h(m["formula"], 300)) for m in M)
    agents = "".join('<tr><td><strong>%s</strong><div class="text-secondary">%s</div></td><td>%s</td><td>%s</td><td>%s%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s%s</td></tr>' % (
        h(s["id"], 40), h(s["title"], 80), h((s.get("coder") or {}).get("client") or "?", 30), h((s.get("coder") or {}).get("model") or "?", 60), label[s["status"]],
        " · " + h((s.get("checkpoint") or {}).get("status"), 20) if (s.get("checkpoint") or {}).get("status") else "",
        ("%d%% <span class=\"sg-est\">estimate</span>" % s["checkpoint"]["percent"]) if isinstance((s.get("checkpoint") or {}).get("percent"), int) else "—",
        h((s.get("checkpoint") or {}).get("drift") or (s.get("ready") or {}).get("drift") or "none", 30),
        h(("PR #%s · %s" % (s["ci"].get("pr"), s["ci"].get("result"))) if s.get("ci") else "no PR", 40), h((s.get("last_reported") or "—")[:16], 16),
        ' <span class="sg-pill fail">stale</span>' if s.get("stale") else "") for s in d["stories"] if s["status"] in ("in_progress", "blocked", "in_review")) \
        or '<tr><td colspan="8" class="text-secondary">No agent is working on a story right now.</td></tr>'
    feats = "".join('<tr><td><strong>%s</strong> <span class="text-secondary">%s</span></td><td class="text-end">%d</td><td class="text-end">%d</td><td class="text-end">%d</td></tr>' % (
        h(f["id"], 40), h(f["title"], 80) if f["title"] != f["id"] else "", len([s for s in d["stories"] if s["feature"] == f["id"]]),
        len([s for s in d["stories"] if s["feature"] == f["id"] and (s.get("ready") or {}).get("overall") == "PASS"]),
        len([s for s in d["stories"] if s["feature"] == f["id"] and s["status"] == "done"])) for f in d.get("features", [])) \
        or '<tr><td colspan="4" class="text-secondary">No features yet. Add one with gate.py feature &lt;ID&gt; --title "…"</td></tr>'
    rows = "".join('<tr><td><strong>%s</strong><div class="text-secondary">%s</div></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td class="text-secondary">%s</td></tr>' % (
        h(s["id"], 40), h(s["title"], 100), h(s["feature"] or "—", 40), label[s["status"]], pill((s.get("ready") or {}).get("overall")),
        pill((s.get("done") or {}).get("overall")), ("%d/%d" % (sum(1 for _, ok in s["trace"] if ok), len(s["trace"]))) if s["trace"] else "—", h(s["ref"], 80)) for s in d["stories"]) \
        or '<tr><td colspan="8" class="text-secondary">No stories yet. Plan one with gate.py plan &lt;ID&gt; --title "…"</td></tr>'
    notes = ""
    if d.get("conflicts"):
        notes += '<div class="alert alert-warning">Ownership conflicts: %s</div>' % h("; ".join("%s on %s" % (c["story"], ", ".join(c["branches"])) for c in d["conflicts"]), 1000)
    if d.get("omissions"):
        notes += '<div class="alert alert-info">Not shown: %s</div>' % h("; ".join(d["omissions"]), 1000)
    if d.get("computer"):  # local runs only: how well this computer is protected from hooks shipped inside branches
        notes += ('<div class="card mb-3"><div class="card-header"><h3 class="card-title">This computer: hooks shipped inside branches</h3></div>'
                  '<div class="table-responsive"><table class="table card-table"><thead><tr><th>AI tool</th><th>Protection</th><th>Next step</th></tr></thead><tbody>%s'
                  '</tbody></table></div><div class="card-body text-secondary small">Hard = the tool itself refuses repository hooks. Hard-on-change = the tool asks you again whenever a hook changes. Partial = '
                  'story-gate\'s checkout filter removes unapproved hook commands in the repositories it covers. Prove it: gate.py doctor --prove</div></div>'
                  % "".join('<tr><td>%s</td><td>%s</td><td class="text-secondary">%s</td></tr>' % (h(r["tool"], 20), pill_level(r["protection"]), h(r["next"], 200))
                            for r in d["computer"]))
    return """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:">
<title>Story-gate dashboard</title><style>%s</style><style>%s</style></head><body>
<header class="sg-head py-3 mb-3"><div class="container-xl d-flex flex-wrap justify-content-between align-items-center gap-2">
<div><h1 class="m-0">Story-gate dashboard</h1><div class="text-secondary">Updated %s · %s at %s · %d branches scanned%s</div></div>
</div></header>
<main class="container-xl">%s
<div class="row row-cards mb-3">%s</div>
<div class="row row-cards">
<div class="col-lg-5"><div class="card"><div class="card-header"><h3 class="card-title">Pipeline</h3></div><div class="card-body">%s
<p class="text-secondary small mt-2 mb-0">Each bar counts stories at that stage. Hover a bar for what the stage means.</p></div></div></div>
<div class="col-lg-7"><div class="card"><div class="card-header"><h3 class="card-title">Who is working on what</h3></div><div class="table-responsive">
<table class="table table-vcenter card-table"><thead><tr><th>Story</th><th>Agent</th><th>Model</th><th>Stage</th><th>Done</th><th>Drift</th><th>CI check</th><th>Last report</th></tr></thead><tbody>%s</tbody></table></div></div></div>
<div class="col-lg-5"><div class="card"><div class="card-header"><h3 class="card-title">Features</h3></div><div class="table-responsive">
<table class="table card-table"><thead><tr><th>Feature</th><th class="text-end">Stories</th><th class="text-end">Ready</th><th class="text-end">Done</th></tr></thead><tbody>%s</tbody></table></div></div></div>
<div class="col-lg-7"><div class="card"><div class="card-header"><h3 class="card-title">Quality and flow</h3></div><div class="table-responsive">
<table class="table card-table"><thead><tr><th>Measure</th><th class="text-end">Value</th><th>How it's calculated</th></tr></thead><tbody>%s</tbody></table></div></div></div>
<div class="col-12"><div class="card"><div class="card-header"><h3 class="card-title">All stories</h3></div><div class="table-responsive">
<table class="table table-vcenter card-table"><thead><tr><th>Story</th><th>Feature</th><th>Stage</th><th>READY</th><th>DONE</th><th>Criteria proven</th><th>Branch</th></tr></thead><tbody>%s</tbody></table></div></div></div>
</div>
<p class="text-secondary small my-3">Verdicts are recorded by the coding agents; the story-gate check in CI re-checks them on every pull request. Estimates are marked. %s</p>
</main>
<footer class="container-xl py-3"><span class="sg-brand text-secondary">story-gate · by <span aria-label="Viaknox">%s</span></span></footer>
</body></html>""" % (css, BRAND_CSS, h(d.get("generated_at"), 30), h(d.get("default_ref"), 60), h((d.get("default_sha") or "")[:12], 12),
                      len(d.get("refs_scanned", [])), (" · " + h(artifact_note, 200)) if artifact_note else "", notes, kpi, bar_chart(counts), agents, feats,
                      quality, rows, h("Snapshot of: " + ", ".join(r["ref"] for r in d.get("refs_scanned", [])[:30]), 2000), mark)


# ------------------------------------------------------------------ publishing (CI)
def call(G, method, path, token, body=None, tries=3):
    """GitHub API call that backs off on rate limits (403/429 with Retry-After or a reset time)."""
    for i in range(tries):
        st, data, hdr = G.call(method, path, token, body)
        if st not in (403, 429) or i == tries - 1:
            return st, data, hdr
        wait = (hdr or {}).get("Retry-After") or 0
        try:
            wait = int(wait) or max(1, int((hdr or {}).get("X-RateLimit-Reset", 0)) - int(time.time()))
        except (TypeError, ValueError):
            wait = 5
        time.sleep(min(60, max(1, wait)))
    return st, data, hdr


def ci_status(G, repo, token, data):
    """Mark each unmerged story with the result of the story-gate check on its open pull request (CI-verified or not)."""
    prs, page = [], 1
    while page <= 5:  # up to 500 open pull requests
        st, chunk, _ = call(G, "GET", "/repos/%s/pulls?state=open&per_page=100&page=%d" % (repo, page), token)
        if st != 200 or not isinstance(chunk, list):
            data["omissions"].append("could not read open pull requests (HTTP %s), so some CI results aren't shown" % st)
            break
        prs += chunk
        if len(chunk) < 100:
            break
        page += 1
    own = [p for p in prs if (((p.get("head") or {}).get("repo") or {}).get("full_name") or "").lower() == repo.lower()]  # a fork's branch can share a name
    heads = {"origin/" + (p.get("head") or {}).get("ref", ""): ((p.get("head") or {}).get("sha"), p.get("number")) for p in own}
    for s in data["stories"]:
        if s["merged"] or s["ref"] not in heads:
            continue
        sha, num = heads[s["ref"]]
        st, runs, _ = call(G, "GET", "/repos/%s/commits/%s/check-runs?check_name=story-gate" % (repo, sha), token)
        runs = (runs or {}).get("check_runs") if isinstance(runs, dict) else None
        if st == 200 and runs:
            r = runs[0]
            s["ci"] = {"pr": num, "result": clean(r.get("conclusion") or r.get("status"), 20)}
        else:
            s["ci"] = {"pr": num, "result": "not run"}

def publish_issue(G, repo, token, body, generated_at, issue_number=None):
    """Create or update the single dashboard issue. Never overwrites a newer snapshot. Returns a plain-English result.
    The issue is found by `dashboard_issue` in config (if a human set it), else by the story-gate-dashboard label."""
    st, _, _ = call(G, "GET", "/repos/%s/labels/%s" % (repo, LABEL), token)
    if st == 404:
        call(G, "POST", "/repos/%s/labels" % repo, token, {"name": LABEL, "color": "FFD84D", "description": "Managed by story-gate"})
    st, issues, _ = call(G, "GET", "/repos/%s/issues?labels=%s&state=all&per_page=20&sort=created&direction=asc" % (repo, LABEL), token)
    if st == 410:
        return "Issues are turned off in this repository, so the dashboard is only in the workflow summary and the report artifact."
    if st != 200 or not isinstance(issues, list):
        return "Could not read issues (HTTP %s); the dashboard is in the workflow summary." % st
    issues = [i for i in issues if not i.get("pull_request")]
    if issue_number:
        st, one, _ = call(G, "GET", "/repos/%s/issues/%s" % (repo, int(issue_number)), token)
        if st == 200 and isinstance(one, dict) and not one.get("pull_request"):
            issues = [one]
    if not issues:
        st, made, _ = call(G, "POST", "/repos/%s/issues" % repo, token, {"title": "Story-gate dashboard", "body": body, "labels": [LABEL]})
        if st not in (200, 201):
            return "Could not create the dashboard issue (HTTP %s)." % st
        pin(G, token, repo, made.get("node_id"))
        return "Created the dashboard issue #%s." % made.get("number")
    issue = issues[0]
    m = re.search(r"generated_at=(\S+)", issue.get("body") or "")
    if m and m.group(1) > generated_at:
        return "Skipped: issue #%s already shows a newer snapshot (%s)." % (issue["number"], m.group(1))
    patch = {"body": body}
    if issue.get("state") == "closed":
        patch["state"] = "open"
    st, _, _ = call(G, "PATCH", "/repos/%s/issues/%s" % (repo, issue["number"]), token, patch)
    return ("Updated the dashboard issue #%s." % issue["number"]) if st == 200 else "Could not update issue #%s (HTTP %s)." % (issue["number"], st)


FAIL_MARK = "<!-- story-gate-dashboard refresh-failed -->"


def report_failure(G, repo, token, when, run_url="", issue_number=None):
    """A refresh failed: keep the last good snapshot, and say so at the top of the issue."""
    st, issues, _ = call(G, "GET", "/repos/%s/issues?labels=%s&state=all&per_page=20&sort=created&direction=asc" % (repo, LABEL), token)
    issues = [i for i in (issues if st == 200 and isinstance(issues, list) else []) if not i.get("pull_request")]
    if issue_number:
        st, one, _ = call(G, "GET", "/repos/%s/issues/%s" % (repo, int(issue_number)), token)
        issues = [one] if st == 200 and isinstance(one, dict) else issues
    if not issues:
        return "no dashboard issue to annotate"
    issue = issues[0]
    body = re.sub(r"\n?%s[^\n]*\n" % re.escape(FAIL_MARK), "\n", issue.get("body") or "")
    note = "%s **The last refresh failed at %s**%s. This shows the last good snapshot.\n" % (
        FAIL_MARK, md(when, 30), (" ([run log](%s))" % run_url) if run_url.startswith("https://") else "")
    body = body.replace("# Story-gate dashboard\n", "# Story-gate dashboard\n\n" + note, 1) if "# Story-gate dashboard\n" in body else note + body
    st, _, _ = call(G, "PATCH", "/repos/%s/issues/%s" % (repo, issue["number"]), token, {"body": body})
    return "marked issue #%s: last refresh failed" % issue["number"] if st == 200 else "could not mark the issue (HTTP %s)" % st


def pin(G, token, repo, node_id):
    """Pin the issue only when one of the repository's three pin slots is free (never unpins anything)."""
    if not node_id:
        return "not pinned"
    try:
        owner, name = repo.split("/", 1)
        d = G.graphql("query($o:String!,$n:String!){repository(owner:$o,name:$n){pinnedIssues(first:3){totalCount}}}", {"o": owner, "n": name}, token)
        if ((d.get("repository") or {}).get("pinnedIssues") or {}).get("totalCount", 3) >= 3:
            return "not pinned (all three pin slots are in use)"
        G.graphql("mutation($id:ID!){pinIssue(input:{issueId:$id}){issue{number}}}", {"id": node_id}, token)
        return "pinned"
    except Exception:
        return "not pinned (the workflow token may not be allowed to pin)"


# ------------------------------------------------------------------ command line
def cli(gate, kv, rest):
    import sg_github as G
    c = gate.cfg()
    root = gate.ROOT
    offline = "--offline" in rest
    if "--report-failure" in rest:
        token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
        if not token or not repo:
            return 1
        run_url = "%s/%s/actions/runs/%s" % (os.environ.get("GITHUB_SERVER_URL", "https://github.com"), repo, os.environ.get("GITHUB_RUN_ID", ""))
        print(report_failure(G, repo, token, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), run_url, c.get("dashboard_issue")))
        return 0
    in_ci = bool(os.environ.get("GITHUB_ACTIONS"))
    if kv.get("from-json"):
        data = json.loads(Path(kv["from-json"]).read_text(encoding="utf-8"))
    else:
        local_note = ""
        if not in_ci and not offline:
            r = subprocess.run(["git", "fetch", "--quiet", "--prune", "origin"], cwd=str(root), capture_output=True, text=True)
            local_note = "" if r.returncode == 0 else "could not fetch from origin (%s); this snapshot may be out of date" % (r.stderr.strip()[:120] or "offline")
        default = os.environ.get("SG_DEFAULT_BRANCH")
        ref = ("origin/" + default) if default else (gate.T.default_policy_ref(root, c.get("base_branch", "main")) or c.get("base_branch", "main"))
        data = build(root, ref, c["story_id_pattern"], include_local=not in_ci, gate=gate)
        if in_ci and os.environ.get("GITHUB_TOKEN") and os.environ.get("GITHUB_REPOSITORY"):
            ci_status(G, os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"], data)
        if not in_ci:
            import sg_guard as SG
            data["computer"] = [{"tool": t, "protection": p, "next": n} for t, p, n in SG.client_matrix(str(root))]
            data["omissions"] = ([local_note] if local_note else []) + ["local snapshot: includes this computer's local branches and anything not pushed"] + data["omissions"]
    out = Path(kv.get("out") or tempfile.mkdtemp(prefix="story-gate-dashboard-"))
    out.mkdir(parents=True, exist_ok=True)
    (out / "dashboard.json").write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    body = to_markdown(data, kv.get("artifact-url"))
    (out / "dashboard.md").write_text(body, encoding="utf-8")
    (out / "dashboard.html").write_text(to_html(data), encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and "--publish" in rest:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(body + "\n")
    msg = "story-gate dashboard: %d stories, %s. Files in %s" % (len(data.get("stories", [])), data.get("generated_at"), out)
    if "--publish" in rest:
        token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
        if not token or not repo:
            print("::warning::--publish needs GITHUB_TOKEN and GITHUB_REPOSITORY"); return 1
        msg += "\n" + publish_issue(G, repo, token, body, data.get("generated_at") or "", c.get("dashboard_issue"))
    print(msg)
    if "--open" in rest:
        import webbrowser
        webbrowser.open((out / "dashboard.html").resolve().as_uri())
    return 0
