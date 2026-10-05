"""Validation evidence: proof that a finished story works when it runs, written for a non-coder owner.

Two records per story, both written only by story-gate commands (agents can't edit them):
  scenarios.json          what to run to show each acceptance criterion working (command, expected exit, expected output)
  scenario_results.json   the last run of each scenario: exit code, whether the output matched, redacted output tail,
                          the code fingerprint it ran on, and who ran it ("local" = the agent's computer, "ci" = the PR check)

The agent also writes validation.md (the owner's summary). CI runs every scenario again on the PR's code, in the job
that has no secrets, so "it was run and it worked" is checked, not just claimed. These controls limit and record a run;
they are not a sandbox. Stdlib only.
"""
import hashlib, json, os, re, shutil, signal, subprocess, tempfile, time

SECTIONS = ("## Result", "## Acceptance criteria", "## Scenarios run", "## Bugs found and fixed", "## Lessons learnt",
            "## Known limits", "## Demo")
TEMPLATE = """# Validation: {id}
For the owner. Plain English, short sentences (PROTOCOL.md > Writing). story-gate adds the checked facts (tests, scenario runs).

## Result
TODO: 2 to 4 sentences. Is the story done? What can a user do now that they couldn't before?

## Acceptance criteria
TODO: one line per AC: AC-1: met / not met, and the scenario or test that shows it.

## Scenarios run
TODO: what you ran to see the feature work (record each with `story-gate scenario`), and what you saw.

## Bugs found and fixed
TODO: bugs you hit while building or testing, and how you fixed and re-checked each one. Or 'None found'.

## Lessons learnt
TODO: what you'd do differently next time, in plain words (the details go in `story-gate learn`). Or 'None'.

## Known limits
TODO: what this story does not do, or 'None'.

## Demo
TODO: steps the owner can follow to try it (what to open, what to click or type, what they should see).
Or 'Not demo-able: <reason>' for internal changes with nothing to see.
"""
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,79}")
TAIL, MATCH_LIMIT, EXPECT_LIMIT = 4000, 1_000_000, 300
DEFAULT_TIMEOUT, MAX_TIMEOUT, CI_BUDGET, MAX_SCENARIOS = 120, 600, 1200, 30
SECRET_PATTERNS = [re.compile(p) for p in (
    r"gh[pousr]_[A-Za-z0-9]{20,}", r"github_pat_[A-Za-z0-9_]{20,}", r"sk-[A-Za-z0-9_-]{16,}", r"xox[abprs]-[A-Za-z0-9-]{10,}",
    r"AKIA[0-9A-Z]{16}", r"AIza[0-9A-Za-z_-]{30,}", r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{16,}=*",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")]
SECRET_ENV = re.compile(r"(?i)(?:key|token|secret|password|passwd|pat|credential)s?$")


def redact(text, env=None):
    """Remove values of secret-looking environment variables and common token shapes. Best effort, never complete."""
    env = os.environ if env is None else env
    for name, val in env.items():
        if SECRET_ENV.search(name) and val and len(val) >= 8:
            text = text.replace(val, "[redacted]")
    for p in SECRET_PATTERNS:
        text = p.sub("[redacted]", text)
    return text


def spec_hash(s):
    """Fingerprint of a scenario's own fields, so a result is only trusted against the spec that produced it."""
    keep = {k: s.get(k) for k in ("name", "acs", "argv", "expect_exit", "expect_output", "timeout", "local_only")}
    return hashlib.sha256(json.dumps(keep, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def problems(s, ac_ids):
    """Why a scenario spec is not usable ([] when it is)."""
    out = []
    if not isinstance(s, dict):
        return ["not an object"]
    if not isinstance(s.get("name"), str) or not NAME.fullmatch(s["name"]):
        out.append("name must be 1-80 letters, digits, spaces, '.', '_' or '-'")
    acs = s.get("acs")
    if not isinstance(acs, list) or not acs or not all(isinstance(a, str) for a in acs):
        out.append("needs at least one acceptance criterion (--ac AC-1)")
    else:
        unknown = [a for a in acs if a not in ac_ids]
        if unknown:
            out.append("unknown acceptance criteria %s (tests.json has %s)" % (", ".join(unknown), ", ".join(ac_ids) or "none"))
    argv = s.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
        out.append("needs a command after --")
    exp = s.get("expect_output")
    if not isinstance(exp, str) or not exp.strip():
        out.append("needs --expect: text (a regular expression) the output must contain, so the run proves something")
    elif len(exp) > EXPECT_LIMIT:
        out.append("--expect is longer than %d characters" % EXPECT_LIMIT)
    else:
        try:
            if re.search(exp, ""):
                out.append("--expect matches empty output, so it proves nothing: name text the output must contain")
        except re.error as e:
            out.append("--expect is not a valid regular expression (%s)" % e)
    if not isinstance(s.get("expect_exit"), int) or isinstance(s.get("expect_exit"), bool):
        out.append("--exit must be a whole number")
    t = s.get("timeout")
    if not isinstance(t, int) or isinstance(t, bool) or not 1 <= t <= MAX_TIMEOUT:
        out.append("--timeout must be 1 to %d seconds" % MAX_TIMEOUT)
    if not isinstance(s.get("local_only", ""), str):
        out.append("local_only must be a reason (text)")
    return out


def specs_of(doc):
    """The scenario specs in a scenarios.json document; anything malformed is dropped, never a crash."""
    s = doc.get("scenarios") if isinstance(doc, dict) else None
    return [x for x in s if isinstance(x, dict)] if isinstance(s, list) else []


def results_of(doc):
    """The scenario results in a scenario_results.json document; anything malformed is dropped, never a crash."""
    r = doc.get("results") if isinstance(doc, dict) else None
    return {k: v for k, v in r.items() if isinstance(v, dict)} if isinstance(r, dict) else {}


def _kill(p):
    """Best-effort kill of a scenario's whole process group (or process tree on Windows)."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True, timeout=30)
        else:
            os.killpg(p.pid, signal.SIGKILL)
    except Exception:
        pass
    try:
        p.kill()
    except Exception:
        pass


def fingerprint_of(state):
    """Hash a work_state() mapping down to the short fingerprint stored with a scenario result."""
    h = hashlib.sha256()
    for n, body in sorted(state.items()):
        h.update(n.encode("utf-8", "replace") + b"\0" + body)
    return h.hexdigest()[:16]


def run(s, root, work_state, source, timeout=None):
    """Run one scenario without a shell. Returns the result record. work_state() -> {path: digest} of the work.
    A run that creates or changes files in the work is void: it would be testing code that isn't the code under review."""
    before = work_state()
    argv = list(s["argv"])
    exe = shutil.which(argv[0], path=os.environ.get("PATH")) if not os.path.dirname(argv[0]) else None
    if exe:
        argv[0] = exe  # Windows: finds npm.cmd, pytest.exe; elsewhere the same program the shell would run
    t0, limit = time.time(), int(timeout or s["timeout"])
    out, code, note = "", None, ""
    try:
        kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        with tempfile.TemporaryFile() as buf:  # a file, not a pipe: a server the scenario leaves running can't hold the run open
            p = subprocess.Popen(argv, cwd=root, stdin=subprocess.DEVNULL, stdout=buf, stderr=subprocess.STDOUT, **kw)
            try:
                code = p.wait(timeout=limit)
            except subprocess.TimeoutExpired:
                note = "stopped after %d seconds (timeout)" % limit
            _kill(p)  # the scenario's whole process group ends with it (servers it started, stray children)
            try:
                p.wait(timeout=10)
            except Exception:
                pass
            size = buf.seek(0, 2)
            buf.seek(max(0, size - MATCH_LIMIT))  # the end of the output: where results and errors are
            out = buf.read(MATCH_LIMIT).decode("utf-8", "replace").replace("\r\n", "\n")  # Windows line ends: '$' still matches
    except OSError as e:
        note = "could not start: %s" % e
    matched = bool(code is not None and re.search(s["expect_output"], out))
    after = work_state()
    changed = sorted(n for n in set(before) | set(after) if before.get(n) != after.get(n))
    if changed:
        note = (note + "; " if note else "") + ("the run created or changed files in the repository (%s), so it doesn't count: "
                                                "write output to a temporary folder or an ignored path" % ", ".join(changed[:5]))
    ok = code == s["expect_exit"] and matched and not changed and not note
    return {"spec": spec_hash(s), "passed": ok, "exit_code": code, "expect_exit": s["expect_exit"], "output_matched": matched,
            "note": note, "seconds": round(time.time() - t0, 1), "fingerprint": fingerprint_of(before), "source": source,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "output_tail": redact(out)[-TAIL:]}


def run_all(specs, root, work_state, source, ac_ids, budget=CI_BUDGET, skip_local_only=False):
    """Run every usable scenario in order inside a total time budget. Returns {name: result}."""
    results, t0 = {}, time.time()
    for s in specs[:MAX_SCENARIOS]:
        name = s.get("name") if isinstance(s, dict) else None
        if not isinstance(name, str):
            continue
        bad = problems(s, ac_ids)
        if bad:
            results[name] = {"spec": spec_hash(s) if isinstance(s, dict) else "", "passed": False, "note": "; ".join(bad), "source": source}
            continue
        if skip_local_only and s.get("local_only"):
            continue
        left = budget - (time.time() - t0)
        if left < 5:
            results[name] = {"spec": spec_hash(s), "passed": False, "source": source,
                             "note": "not run: the %d-second budget for all scenarios ran out" % budget}
            continue
        results[name] = run(s, root, work_state, source, timeout=min(s["timeout"], int(left)))
    return results


def coverage(doc, results, ac_ids, fingerprint, in_ci):
    """Per acceptance criterion: 'ok', 'local' (only a run on the agent's computer, for a local-only scenario, in CI) or
    'missing'. A result counts only for the current scenario spec, on the current code, and when it passed."""
    specs = specs_of(doc)
    state = {}
    for ac in ac_ids:
        best = "missing"
        for s in specs:
            if not isinstance(s.get("acs"), list) or ac not in s["acs"]:
                continue
            r = results.get(s.get("name")) if isinstance(s.get("name"), str) else None
            r = r or {}
            if not (r.get("passed") and r.get("spec") == spec_hash(s) and r.get("fingerprint") == fingerprint):
                continue
            if not in_ci or r.get("source") == "ci":
                best = "ok"
                break
            if s.get("local_only") and r.get("source") == "local":
                best = "local"
        state[ac] = best
    return state


def failing(doc, results, fingerprint, in_ci):
    """Recorded scenarios that don't stand on the current code: failed, not run on it, or a duplicate name.
    In CI a local-only scenario is judged by coverage() instead (CI never runs it)."""
    out, seen = [], set()
    for s in specs_of(doc):
        n = s.get("name")
        if not isinstance(n, str):
            out.append("a scenario with no name")
            continue
        if n in seen:
            out.append("%s (two scenarios share this name)" % n)
            continue
        seen.add(n)
        r = results.get(n) or {}
        if in_ci and s.get("local_only") and r.get("source") == "local":
            continue
        if not r or r.get("spec") != spec_hash(s) or r.get("fingerprint") != fingerprint:
            out.append("%s (not run on this code)" % n)
        elif not r.get("passed"):
            out.append("%s (%s)" % (n, (r.get("note") or "exit %s, output %s" % (
                r.get("exit_code"), "matched" if r.get("output_matched") else "did not match"))[:160]))
    return out


def sections_missing(text):
    """validation.md sections that are absent, empty or still TODO."""
    found, cur = {}, None
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        if line.startswith("## "):
            cur = line.strip()
            found[cur] = []
        elif cur:
            found[cur].append(line)
    out = []
    for s in SECTIONS:
        body = "\n".join(found.get(s, [])).strip()
        if not body or re.search(r"(?m)^\s*TODO\b", body):  # the template's placeholder lines, not the word in real text
            out.append(s[3:])
    return out


def result_section(text, limit=1200):
    """The '## Result' section of validation.md, plain text, capped (for the CI summary)."""
    m = re.search(r"(?ms)^## Result[ \t]*$(.*?)(?=^## |\Z)", (text or "")[:200_000].replace("\r\n", "\n"))
    body = m.group(1)[:limit * 4].strip() if m else ""
    if not body or re.search(r"(?m)^\s*TODO\b", body):
        return ""
    return body[:limit] + (" ..." if len(body) > limit else "")


def summary_lines(doc, results, ac_ids, fingerprint, in_ci):
    """Markdown table for the CI summary: scenario -> ACs -> who ran it -> result."""
    specs = specs_of(doc)
    if not specs:
        return []
    rows = ["", "### Scenarios (the feature, run end to end)", "", "| Scenario | Covers | Ran on | Result |", "|---|---|---|---|"]
    for s in specs:
        r = (results.get(s.get("name")) if isinstance(s.get("name"), str) else None) or {}
        fresh = r.get("spec") == spec_hash(s) and r.get("fingerprint") == fingerprint
        res = ("pass" if r.get("passed") else "FAIL") if fresh else ("not run on this code" if r else "never run")
        if r.get("note"):
            res += " (%s)" % r["note"][:120]
        who = {"ci": "CI (checked)", "local": "agent's computer (reported)"}.get(r.get("source"), "-")
        acs_ = s.get("acs") if isinstance(s.get("acs"), list) else []
        rows.append("| %s | %s | %s | %s |" % (str(s.get("name"))[:80].replace("|", "/"), ", ".join(map(str, acs_))[:60].replace("|", "/"),
                                              who, res.replace("|", "/")))
    cov = coverage(doc, results, ac_ids, fingerprint, in_ci)
    gaps = [a for a, v in cov.items() if v != "ok"]
    if gaps:
        rows.append("")
        rows.append("Not proved by a scenario run%s: %s" % (" in CI" if in_ci else "", ", ".join(gaps)))
    return rows
