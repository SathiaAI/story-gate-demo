"""story-gate guard: stop hooks shipped inside a branch from running on your computer.

Three layers (panel decision sg-repohooks, chosen by the owner):
  1. Checkout filter (per enrolled repository, no admin, on by default with enrollment): a git filter set in the
     repository's own .git folder (never committed). Whenever git writes an AI-tool hook file, it writes the version
     the default branch approved, minus any command that isn't approved. A branch's hook changes never reach disk.
  2. Lockdown (optional, OFF by default, needs your explicit permission and admin rights once): turns on the AI tool's
     own hard switch where one exists. Today that is Claude Code's `allowManagedHooksOnly`. It is computer-wide, so
     it's explained in full before anything changes, and it can be undone.
  3. story-gate's own pre-edit block and the CI label rule (in gate.py) for everything else.

`gate.py doctor --prove` plants a harmless canary hook on a throwaway commit and shows that it never lands on disk.
"""
import hashlib, json, os, re, shutil, subprocess, sys, tempfile, time
from pathlib import Path

import sg_trust as T
import sg_pin as PIN

FILTER = "storygate-hooks"
FILTERED = (".claude/settings.json", ".claude/settings.local.json", ".mcp.json", ".codex/hooks.json", ".codex/config.toml",
            ".cursor/hooks.json", ".cursor/mcp.json", ".gemini/settings.json", ".devin/hooks.json", ".windsurf/hooks.json",
            ".grok/hooks/*.json", ".github/hooks/*.json", ".agents/hooks.json")  # matched at any depth (a tool started in a sub-folder reads that folder's files)
# .github/hooks/*.json: VS Code agent hooks; .agents/hooks.json: Antigravity hooks (both run commands on this computer).
# Top-level settings a branch may not change from the default branch's version: they run programs, reach the network,
# load servers or plugins, or loosen permissions.
GUARDED_KEYS = {"env", "permissions", "mcpServers", "enableAllProjectMcpServers", "enabledMcpjsonServers", "disabledMcpjsonServers",
                "apiKeyHelper", "awsAuthRefresh", "awsCredentialExport", "otelHeadersHelper", "statusLine", "fileSuggestion",
                "subagentStatusLine", "enabledPlugins", "extraKnownMarketplaces", "disableAllHooks", "allowManagedHooksOnly",
                "sandbox", "tools", "hooksConfig"}
ACTOR_TYPES = {"command", "http", "prompt", "agent", "mcp_tool"}
ACTOR_KEYS = ("command", "url", "prompt", "agent")
ATTR_MARK = "# story-gate: AI-tool hook files are filtered so a branch can't add commands (gate.py enroll / unenroll)"
CANARY = "story-gate-canary-should-never-run"
_DROP = object()


# ------------------------------------------------------------------ sanitizing hook files
def is_exec_key(k):
    """Keys whose value an AI tool runs: command, apiKeyHelper, Gemini's discoveryCommand/callCommand, ..."""
    lk = k.lower() if isinstance(k, str) else ""
    return lk.endswith("command") or lk.endswith("helper") or k in ("awsAuthRefresh", "awsCredentialExport")


def exec_strings(data):
    """Every command-like string in a hook/settings JSON value."""
    out = set()
    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if is_exec_key(k) and isinstance(v, str):
                    out.add(v)
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(data)
    return out


def _canon(x):
    return json.dumps(x, sort_keys=True)


def _is_actor(d):
    return any(k in d for k in ACTOR_KEYS) or d.get("type") in ACTOR_TYPES


def actor_entries(data):
    """Canonical form of every hook/server entry (anything with a command, url, prompt or agent)."""
    out = set()
    def walk(x, depth):
        if isinstance(x, dict):
            if depth and _is_actor(x):
                out.add(_canon(x))
            for v in x.values():
                walk(v, depth + 1)
        elif isinstance(x, list):
            for v in x:
                walk(v, depth + 1)
    walk(data, 0)
    return out


def strip_unapproved(data, approved_cmds, approved_entries=frozenset()):
    """Remove every hook/server entry and command the default branch didn't approve. Returns (new_data, removed).
    An entry survives if it is identical to an approved one, or if its only active part is an approved command string."""
    removed = []
    def walk(x, depth):
        if isinstance(x, dict):
            if depth and _is_actor(x) and _canon(x) not in approved_entries:
                active = [k for k in x if k in ACTOR_KEYS or is_exec_key(k)]
                plain_cmd = (active == ["command"] and isinstance(x["command"], str) and x["command"] in approved_cmds
                             and not set(x) & {"args", "env", "headers", "url", "cwd"})
                if not plain_cmd:
                    removed.append(str(x.get("command") or x.get("url") or x.get("prompt") or x.get("agent") or x.get("type"))[:200])
                    return _DROP
            out = {}
            for k, v in x.items():
                if is_exec_key(k) and not (isinstance(v, str) and v in approved_cmds):
                    removed.append(str(v)[:200])
                    continue
                nv = walk(v, depth + 1)
                if nv is _DROP or (isinstance(v, (dict, list)) and v and not nv):
                    continue  # an entry that is now empty disappears
                out[k] = nv
            return out
        if isinstance(x, list):
            out = []
            for v in x:
                nv = walk(v, depth + 1)
                if nv is _DROP or (isinstance(v, (dict, list)) and v and not nv):
                    continue
                out.append(nv)
            return out
        return x
    return walk(data, 0), removed


def _load(b):
    return json.loads(b.decode("utf-8")) if b and b.strip() else {}


def sanitize(branch_bytes, approved_bytes, allowed=()):
    """What git should write for a hook file: the branch's version minus anything the default branch didn't approve.
    Unparseable or non-JSON content (e.g. .codex/config.toml) is replaced by the approved version."""
    if branch_bytes == approved_bytes:
        return branch_bytes, []
    try:
        data = _load(branch_bytes)
    except (ValueError, UnicodeDecodeError):
        return (approved_bytes or b""), ["<file replaced by the approved version: not JSON>"]
    try:
        appr = _load(approved_bytes) if approved_bytes else {}
    except (ValueError, UnicodeDecodeError):
        appr = {}
    if not isinstance(data, dict):
        return (approved_bytes or b"{}\n"), ["<file replaced by the approved version: not a JSON object>"]
    appr = appr if isinstance(appr, dict) else {}
    removed = []
    for k in GUARDED_KEYS:  # settings that run or load things: exactly the default branch's value, or absent
        if k in data and data.get(k) != appr.get(k):
            removed.append("<setting %s>" % k)
            if k in appr:
                data[k] = appr[k]
            else:
                del data[k]
    new, more = strip_unapproved(data, exec_strings(appr) | set(allowed), actor_entries(appr))
    removed += more
    if not removed:
        return branch_bytes, []
    return (json.dumps(new, indent=2) + "\n").encode("utf-8"), removed


def is_filtered_form(stored, on_disk, allowed=()):
    """True when on_disk is `stored` with some entries taken out (and nothing else changed): our own filtered copy,
    possibly filtered under an older approval."""
    try:
        return _load(sanitize(stored, on_disk, allowed)[0]) == _load(on_disk)
    except (ValueError, UnicodeDecodeError):
        return False


# ------------------------------------------------------------------ the git filter
def _git(top, *args, data=None):
    r = subprocess.run(["git", *args], cwd=top, input=data, capture_output=True)
    return r.stdout if r.returncode == 0 else None


def approved_for(top, path):
    """The default branch's version of `path` and its project_hooks_allowed, read from the commit the policy ref points
    at right now (resolved unambiguously, so a local branch called origin/main can't stand in for it)."""
    e = T.enrollment(top) or {}
    sha = T.policy_commit(top, e.get("policy_ref")) if e.get("policy_ref") else None
    if not sha:
        return None, []
    approved = _git(top, "show", "%s:%s" % (sha, path))
    allowed = []
    try:
        allowed = sorted(PIN.allowed_commands(json.loads(T.policy_text(top, e["policy_ref"], "config.json") or "{}")))
    except (ValueError, AttributeError):
        pass
    return approved, allowed


def _conflicted(data):
    """Merge-conflict markers in a file no tool can parse (so passing it through runs nothing)."""
    if not re.search(rb"^<{7} ", data, re.M) or not re.search(rb"^>{7} ", data, re.M):
        return False
    try:
        _load(data)
        return False
    except (ValueError, UnicodeDecodeError):
        return True


def filter_main(mode, path):
    """git calls this (through the launcher) for every hook file it reads or writes. stdin -> stdout, never fails open."""
    data = sys.stdin.buffer.read()
    top = os.getcwd()
    try:
        approved, allowed = approved_for(top, path)
        if mode == "smudge":  # git is about to write `path` into the working tree
            if _conflicted(data):
                out = data  # a merge conflict: the markers make it unparseable for every tool; resolving it is your call
            else:
                out, removed = sanitize(data, approved, allowed)
                out, blocked = PIN.smudge(out, top, path)  # repo scripts: run only the default branch's copy, or not at all
                if blocked:
                    log(top, "filtered %s: %d hook(s) run repository code without pins" % (path, len(blocked)))
                    sys.stderr.write("story-gate: %d hook(s) in %s run repository code that isn't pinned, so they won't run "
                                     "(gate.py doctor shows how to pin them)\n" % (len(blocked), path))
                if removed:
                    log(top, "filtered %s: removed %d unapproved entr%s" % (path, len(removed), "y" if len(removed) == 1 else "ies"))
                    sys.stderr.write("story-gate: removed %d unapproved hook entr%s or setting(s) from %s (they came from this branch, "
                                     "not the default branch)\n" % (len(removed), "y" if len(removed) == 1 else "ies", path))
        else:  # clean: git is reading the working tree; map our filtered copy back to the stored version so status stays clean
            idx = _git(top, "cat-file", "blob", ":%s" % path)
            idx = idx if idx is not None else _git(top, "show", "HEAD:%s" % path)
            plain = PIN.unwrap_bytes(data)  # never commit runner commands (they hold this computer's paths)
            out = plain
            if idx is not None and (PIN.smudge(sanitize(idx, approved, allowed)[0], top, path)[0] == data
                                    or is_filtered_form(idx, plain, allowed)):
                out = idx
            elif idx is not None:  # you edited the filtered copy: say so if committing it would drop branch commands
                try:
                    lost = exec_strings(_load(idx)) - exec_strings(_load(plain))
                except (ValueError, UnicodeDecodeError):
                    lost = set()
                if lost:
                    sys.stderr.write("story-gate: %s on disk is the filtered copy, so committing it drops %d hook command(s) this branch "
                                     "added. Get new hook commands approved on the default branch first.\n" % (path, len(lost)))
    except Exception as ex:  # fail closed: never pass the branch's hook file through unchecked
        sys.stderr.write("story-gate filter error on %s: %r\n" % (path, ex))
        out = (b"{}\n" if path.endswith(".json") else b"") if mode == "smudge" else data
    sys.stdout.buffer.write(out)
    sys.stdout.flush()
    return 0


def log(top, line):
    try:
        with open(common_dir(top) / "story-gate-guard.log", "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), line))
    except Exception:
        pass


def common_dir(top):
    raw = _git(str(top), "rev-parse", "--git-common-dir")
    if raw is None:
        raise T.TrustError("not inside a git repository")
    p = Path(raw.decode().strip())
    return Path(os.path.realpath(p if p.is_absolute() else Path(top) / p))


def repo_key(top):
    return os.path.normcase(str(common_dir(top)))


def info_attributes(top):
    return common_dir(top) / "info" / "attributes"


def attr_lines():
    return [ATTR_MARK] + ["**/%s filter=%s" % (p, FILTER) for p in FILTERED]


def _shq(s):
    """Single-quote for the shell git runs filters with ($ and backticks stay literal)."""
    return "'%s'" % str(s).replace("\\", "/").replace("'", "'\\''")


def filter_settings(py, launcher):
    cmd = lambda mode: "%s -I %s hook-filter %s %%f" % (_shq(py), _shq(launcher), mode)  # git fills in %f
    return {"filter.%s.smudge" % FILTER: cmd("smudge"), "filter.%s.clean" % FILTER: cmd("clean"), "filter.%s.required" % FILTER: "true"}


def filter_conflicts(top):
    """Other attributes or filters already handling these paths (we never silently override them)."""
    out = []
    for p in FILTERED:
        probe = p.replace("*", "x")
        r = _git(top, "check-attr", "filter", "--", probe)
        val = (r or b"").decode().strip().rsplit(": ", 1)[-1]
        if val not in ("unspecified", FILTER, ""):
            out.append("%s already uses filter '%s'" % (p, val))
    return out


def enable_filter(top, py, launcher, dry_run=False):
    top = str(top)
    out = []
    conflicts = filter_conflicts(top)
    if conflicts:
        return False, ["Checkout filter NOT turned on: " + "; ".join(conflicts) + ". Remove that setting or ask for help; story-gate won't override it."]
    settings = filter_settings(py, launcher)
    attrs = info_attributes(top)
    old = attrs.read_bytes().decode("latin-1") if attrs.is_file() else ""  # latin-1: every byte round-trips exactly
    if ATTR_MARK in old:  # turned on by an older story-gate: bring its list of covered files up to date, keep everything else
        lines, kept, inside = old.split("\n"), [], False
        for l in lines:
            bare, eol = l.rstrip("\r"), l[len(l.rstrip("\r")):]  # Windows line endings stay as they were
            if bare == ATTR_MARK:
                inside = True
                kept.extend(x + eol for x in attr_lines())
                continue
            if inside and bare.endswith(" filter=%s" % FILTER):
                continue
            inside = False
            kept.append(l)
        new = "\n".join(kept)
    else:
        new = old + ("" if not old or old.endswith("\n") else "\n") + "\n".join(attr_lines()) + "\n"
    out.append("  %s: %s" % (attrs, "already set" if new == old else "adds %d lines (local to this computer, never committed)" % len(attr_lines())))
    out += ["  git config --local %s = %s" % (k, v) for k, v in settings.items()]
    if dry_run:
        return True, out
    dirty = _dirty_hook_files(top)
    key = repo_key(top)
    if key not in T.read_json(T.manifest_path()).get("repos", {}):  # first time: remember exactly how things were
        before = {k: (_git(top, "config", "--local", "--get", k) or b"").decode().strip() or None for k in settings}
        T.record("repos", key, config_before=before, attributes_before=old if attrs.is_file() else None)
    attrs.parent.mkdir(parents=True, exist_ok=True)
    attrs.write_bytes(new.encode("latin-1"))
    for k, v in settings.items():
        _git(top, "config", "--local", k, v)
    T.record("repos", key, toplevel=os.path.realpath(top), attributes_path=str(attrs), attributes_after=new, config_after=settings)
    if not filter_active(top):
        return False, out + ["  Checkout filter NOT on: git didn't accept the settings (is .git/config read-only?)"]
    out += rematerialize(top, [n for n in tracked_hook_files(top) if n not in dirty], dirty)
    return True, out


def tracked_hook_files(top):
    names = (_git(top, "ls-files", "-z", "--", *[":(glob)**/%s" % p for p in FILTERED]) or b"").decode("utf-8", "replace").split("\0")
    return [n for n in names if n]


def _dirty_hook_files(top):
    """Tracked hook files you've changed and not committed, as git sees them right now."""
    return [n for n in tracked_hook_files(top) if _git(top, "diff", "--quiet", "--", n) is None]


def rematerialize(top, files, skipped=()):
    """Re-write these tracked hook files from git so the current filter settings apply."""
    out = ["  %s: you have uncommitted edits here, so it wasn't re-checked. Commit or discard them, then run gate.py filter on" % n
           for n in skipped]
    for n in files:
        try:
            os.remove(os.path.join(top, n))
        except OSError:
            pass
        _git(top, "checkout", "--", n)
    return out


def _strip_our_lines(text):
    return "".join(l for l in text.splitlines(True) if l.rstrip("\r\n") != ATTR_MARK and ("filter=%s" % FILTER) not in l)


def disable_filter(top, dry_run=False):
    """Undo enable_filter: attributes and git config back exactly as they were (or, if you changed them since, only
    story-gate's lines and keys removed), and hook files re-checked out without the filter."""
    top = str(top)
    key = repo_key(top)
    m = T.read_json(T.manifest_path()).get("repos", {})
    e = m.get(key) or m.get(os.path.normcase(os.path.realpath(top)))  # older manifests were keyed by the working folder
    attrs = info_attributes(top)
    if dry_run:
        return ["  would restore %s and the git filter settings, then re-check out the hook files" % attrs]
    clean = [n for n in tracked_hook_files(top) if _git(top, "diff", "--quiet", "--", n) is not None]  # judged while filtered
    cur = attrs.read_bytes().decode("latin-1") if attrs.is_file() else None
    if e is not None and cur is not None and cur == e.get("attributes_after"):
        if e.get("attributes_before") is None:
            attrs.unlink()
        else:
            attrs.write_bytes(e["attributes_before"].encode("latin-1"))
    elif cur is not None:  # changed since: keep your lines, remove ours
        kept = _strip_our_lines(cur)
        attrs.write_bytes(kept.encode("latin-1")) if kept else attrs.unlink()
    for k, ours in ((e or {}).get("config_after") or filter_settings("", "")).items():
        now = (_git(top, "config", "--local", "--get", k) or b"").decode().strip()
        if e is not None and now != ours:
            continue  # someone changed it since: leave it
        before = (e or {}).get("config_before", {}).get(k)
        if before is None:
            _git(top, "config", "--local", "--unset-all", k)
        else:
            _git(top, "config", "--local", k, before)
    if e is None:
        _git(top, "config", "--local", "--remove-section", "filter.%s" % FILTER)
    T.forget("repos", key)
    T.forget("repos", os.path.normcase(os.path.realpath(top)))
    out = ["  %s and git filter settings restored" % attrs]
    out += rematerialize(top, clean, [n for n in tracked_hook_files(top) if n not in clean])
    return out


def filter_active(top):
    """The filter as git will actually apply it (effective config, includes and attributes), not just our files."""
    try:
        top = str(top)
        info_attributes(top)
    except T.TrustError:
        return False
    get = lambda k: (_git(top, "config", "--get", k) or b"").decode().strip()
    attr = (_git(top, "check-attr", "filter", "--", ".claude/settings.json") or b"").decode().strip().rsplit(": ", 1)[-1]
    return (attr == FILTER and "hook-filter smudge" in get("filter.%s.smudge" % FILTER)
            and "hook-filter clean" in get("filter.%s.clean" % FILTER) and get("filter.%s.required" % FILTER) == "true")


def default_hook_commands(top):
    """Command strings in the default branch's AI-tool hook files (what it approves)."""
    import sg_pin
    _, sha, _ = sg_pin.policy(top)
    if not sha:
        return set()
    import fnmatch
    names = (_git(str(top), "ls-tree", "-r", "-z", "--name-only", sha) or b"").decode("utf-8", "replace").split("\0")
    pats = [p for p in FILTERED if p.endswith(".json")]
    cmds = set()
    for n in [n for n in names if n and any(fnmatch.fnmatch(n, p) or fnmatch.fnmatch(n, "*/" + p) for p in pats)]:
        try:
            cmds |= exec_strings(_load(_git(str(top), "show", "%s:%s" % (sha, n)) or b""))
        except (ValueError, UnicodeDecodeError):
            pass
    return cmds


HOOK_DIRS = (".claude", ".cursor", ".codex", ".gemini", ".windsurf", ".devin", ".grok", ".grok/hooks", ".github", ".github/hooks", ".agents")


def symlinked_hook_paths(top):
    """Covered hook files, or the folders that hold them, stored in git as symlinks. Git writes symlinks without running
    the checkout filter, so a branch could use one to point an AI tool at unapproved hooks."""
    specs = [":(glob)**/%s" % p for p in FILTERED + HOOK_DIRS]
    raw = (_git(str(top), "ls-files", "-s", "-z", "--", *specs) or b"").decode("utf-8", "replace")
    return sorted({e.split("\t", 1)[1] for e in raw.split("\0") if e.startswith("120000 ") and "\t" in e})


def tampered(top):
    """story-gate turned the filter on here, nobody turned it off with `gate.py filter off`, yet it's not active."""
    try:
        return repo_key(top) in T.read_json(T.manifest_path()).get("repos", {}) and not filter_active(top)
    except T.TrustError:
        return False


# ------------------------------------------------------------------ proof
def prove(top):
    """Plant a canary in every filtered file on a throwaway commit, check those files out in a throwaway worktree, and
    show whether the canary reached disk. Nothing in your branches or working tree changes."""
    top = str(top)
    lines, ok = [], True
    if not filter_active(top):
        return False, ["  checkout filter: OFF in this repository (gate.py filter on, or gate.py enroll)"]
    canary = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "echo %s" % CANARY}]}],
                        "stop": [{"command": "echo %s" % CANARY}]}, "apiKeyHelper": "echo %s" % CANARY,
              "mcpServers": {"canary": {"command": "echo", "args": [CANARY]}}}
    blob = (json.dumps(canary, indent=2) + "\n").encode()
    toml = ('notify = ["sh", "-c", "echo %s"]\n' % CANARY).encode()
    head = (_git(top, "rev-parse", "HEAD") or b"").decode().strip()
    if not head:
        return False, ["  can't prove: this repository has no commits yet"]
    work = Path(tempfile.mkdtemp(prefix="sg-prove-"))
    who = {"GIT_AUTHOR_NAME": "story-gate", "GIT_AUTHOR_EMAIL": "story-gate@localhost", "GIT_COMMITTER_NAME": "story-gate",
           "GIT_COMMITTER_EMAIL": "story-gate@localhost"}
    env = dict(os.environ, GIT_INDEX_FILE=str(work / "index"), **who)
    def g(*a, data=None):
        r = subprocess.run(["git", *a], cwd=top, input=data, capture_output=True, env=env)
        return r.stdout.decode().strip() if r.returncode == 0 else None
    paths = [p.replace("*", "canary") for p in FILTERED]
    wt = work / "wt"
    try:
        g("read-tree", head)
        sha, tsha = g("hash-object", "-w", "--no-filters", "--stdin", data=blob), g("hash-object", "-w", "--no-filters", "--stdin", data=toml)
        for p in paths:
            g("update-index", "--add", "--cacheinfo", "100644,%s,%s" % (tsha if p.endswith(".toml") else sha, p))
        commit = g("commit-tree", g("write-tree") or "", "-p", head, "-m", "story-gate canary (throwaway)")
        if not commit:
            return False, ["  can't prove: git couldn't create the throwaway commit"]
        (work / "nohooks").mkdir()
        nohooks = ["-c", "core.hooksPath=%s" % (work / "nohooks")]
        r = subprocess.run(["git", *nohooks, "worktree", "add", "--no-checkout", "--detach", str(wt), commit], cwd=top, capture_output=True, text=True)
        if r.returncode == 0:  # only the hook files are written, through the filter, so this is quick in big repositories
            r = subprocess.run(["git", *nohooks, "checkout", commit, "--", *paths], cwd=str(wt), capture_output=True, text=True)
        if r.returncode != 0:
            return False, ["  can't prove: %s" % r.stderr.strip()[:200]]
        for p in paths:
            f = wt / p
            if not f.is_file():
                ok = False
                lines.append("  %-30s not written - can't tell" % p)
                continue
            landed = CANARY in f.read_text(encoding="utf-8", errors="ignore")
            ok &= not landed
            lines.append("  %-30s %s" % (p, "CANARY REACHED DISK - NOT protected" if landed else "canary removed before it reached disk"))
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=top, capture_output=True)
        subprocess.run(["git", "worktree", "prune"], cwd=top, capture_output=True)
        shutil.rmtree(work, ignore_errors=True)
    return ok, lines


# ------------------------------------------------------------------ lockdown (optional, admin, explicit consent)
# Claude Code merges managed-settings.json with every *.json in managed-settings.d/ (lists such as hooks combine), so
# story-gate adds ONE drop-in file of its own and never edits a managed-settings.json that IT may own.
# Source: https://code.claude.com/docs/en/managed-settings and https://code.claude.com/docs/en/hooks (October 2026).
LOCKDOWN_MARK = "storyGateLockdown"
DROPIN = "50-story-gate.json"


def claude_managed_dir():
    if os.environ.get("STORY_GATE_MANAGED_DIR"):  # tests, and IT teams staging files
        return Path(os.environ["STORY_GATE_MANAGED_DIR"]) / "claude"
    if sys.platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode")
    if os.name == "nt":
        return Path(r"C:\Program Files\ClaudeCode")
    return Path("/etc/claude-code")


def claude_dropin():
    return claude_managed_dir() / "managed-settings.d" / DROPIN


def _user_claude_hooks(user_hooks_text):
    try:
        user = json.loads(user_hooks_text) if user_hooks_text.strip() else {}
    except ValueError:
        return {}
    hooks = {}
    for ev, entries in ((user.get("hooks") or {}) if isinstance(user, dict) else {}).items():
        kept = [e for e in (entries if isinstance(entries, list) else []) if not T.ours(e)]
        if kept:
            hooks[ev] = kept
    return hooks


def managed_body(py, launcher, user_hooks_text="", keep=()):
    """The drop-in file: the hard switch, story-gate's hooks, your own user-level Claude hooks (so they keep working),
    and any project hook command you chose to keep."""
    ours = T.hook_entries(py.replace("\\", "/"), str(launcher).replace("\\", "/"))["claude"]["hooks"]
    hooks = _user_claude_hooks(user_hooks_text)
    for ev, entries in ours.items():
        hooks.setdefault(ev, []).extend(entries)
    for event, group in keep:  # project hooks you chose to keep, with their own event, matcher and timeout
        hooks.setdefault(event, []).append(group)
    body = {LOCKDOWN_MARK: {"version": 1, "user_hooks_sha256": hashlib.sha256(json.dumps(_user_claude_hooks(user_hooks_text), sort_keys=True).encode()).hexdigest(),
                            "kept_project_hooks": [h["command"] for _, g in keep for h in g.get("hooks", [])]},
            "allowManagedHooksOnly": True, "hooks": hooks}
    return json.dumps(body, indent=2) + "\n"


def find_project_hook(approved_text, command):
    """The (event, group) holding `command` in the default branch's .claude/settings.json, trimmed to that command.
    Refuses commands that run files from the repository: under lockdown they would run in EVERY repository."""
    import sg_pin
    info = sg_pin.analyse(command, ".", None)
    if (command.startswith(("./", "../", ".\\")) or "CLAUDE_PROJECT_DIR" in command or info["runner"] or info["refs"]
            or info["outside"] or not info["parse_ok"]):  # e.g. `python scripts/hook.py`, `npm run lint`
        raise T.TrustError("'%s' runs a file from the repository, so under lockdown any repository could supply it. "
                           "Keep only commands that run something installed on this computer." % command)
    try:
        data = json.loads(approved_text or "{}")
    except ValueError:
        data = {}
    for event, groups in ((data.get("hooks") or {}) if isinstance(data, dict) else {}).items():
        for g in groups if isinstance(groups, list) else []:
            mine = [h for h in (g.get("hooks") or []) if isinstance(h, dict) and h.get("command") == command]
            if mine:
                return event, dict(g, hooks=mine)
    raise T.TrustError("'%s' isn't a hook in this repository's .claude/settings.json on the default branch" % command)


def lockdown_status(user_hooks_text=None):
    p = claude_dropin()
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None
    except (ValueError, OSError):
        data = None
    ours = bool(isinstance(data, dict) and data.get(LOCKDOWN_MARK))
    hooks = json.dumps((data or {}).get("hooks") or {}) if ours else ""
    on = bool(ours and data.get("allowManagedHooksOnly") is True and T.HOOK_SIGNATURE.search(hooks.replace('\\"', '"')))
    stale = False
    if on and user_hooks_text is not None:  # you changed your own Claude hooks since lockdown: they no longer run until refreshed
        now_sha = hashlib.sha256(json.dumps(_user_claude_hooks(user_hooks_text), sort_keys=True).encode()).hexdigest()
        stale = now_sha != data[LOCKDOWN_MARK].get("user_hooks_sha256")
    base = claude_managed_dir() / "managed-settings.json"
    return {"file": str(p), "exists": p.is_file(), "ours": ours, "on": on, "stale": stale,
            "name_taken": bool(p.is_file() and not ours), "it_managed_file": base.is_file()}


def explain(st=None):
    st = st or lockdown_status()
    now = "ON" if st["on"] else ("a file with story-gate's name exists but isn't story-gate's - not touched" if st["name_taken"] else "OFF")
    return """Lockdown is OFF by default. We recommend it, and it only turns on with your explicit permission.

What it does
  Claude Code has a switch, allowManagedHooksOnly, that IT teams use. When it's on, Claude Code ignores hooks from
  every repository and branch (and from your user settings) and runs only hooks in its system-wide settings.
  story-gate's lockdown file contains: the switch, story-gate's own hooks, and a copy of your own Claude hooks so they
  keep working.

Why
  The checkout filter already stops a branch's hook changes from reaching disk in the repositories story-gate covers.
  Lockdown is Claude Code's own hard switch: Claude Code won't run a repository's hooks however the file got there
  (a script, a download, a repository story-gate doesn't cover).

What changes on this computer
  One new file, owned by story-gate: %s
  Nothing else. An existing managed-settings.json (often your IT team's) is never edited; Claude Code merges both.
  Writing it needs admin rights once: story-gate prepares the files and shows you the one command to run.

What it affects
  Computer-wide in Claude Code: project hooks stop running in EVERY repository on this computer, not only the ones
  story-gate covers. To keep a project hook you rely on, run this inside that repository:
  gate.py lockdown --on --keep-project-hook "<its exact command>"  (repeat the flag for more; commands that run a
  file from the repository are refused, because every repository could then supply that file).
  If you change your own Claude hooks later, run gate.py lockdown --on again (doctor tells you when).
  If your company manages Claude Code through MDM or the Windows registry, those settings win over files: give the
  bundle (gate.py lockdown --bundle <folder>) to IT instead.
  Other tools: Codex re-asks you to trust any changed project hook. Cursor, Gemini, Windsurf and Grok have no such
  switch, so the checkout filter is their protection.

Proof
  gate.py doctor --prove plants a harmless canary hook on a throwaway commit, shows it never reaches disk, and shows
  whether lockdown is on.

Undo
  gate.py lockdown --off shows the one command that deletes story-gate's file. Nothing else needs restoring.

Status now: %s
""" % (st["file"], now)


def build_bundle(dest, py, launcher, user_hooks_text="", keep=()):
    """Write the lockdown drop-in plus install/uninstall scripts, an IT readme and checksums into `dest`.
    Nothing outside `dest` is touched."""
    dest = Path(dest)
    st = lockdown_status()
    if st["name_taken"]:
        raise T.TrustError("%s exists and wasn't written by story-gate. Rename or remove it first (or ask IT)." % st["file"])
    body = managed_body(py, launcher, user_hooks_text, keep)
    json.loads(body)  # Claude Code refuses to start on an unparseable managed file: never write one
    (dest / "claude").mkdir(parents=True, exist_ok=True)
    (dest / "claude" / DROPIN).write_bytes(body.encode("utf-8"))  # bytes: LF on every OS
    target = str(claude_dropin())
    (dest / "install.sh").write_bytes(("""#!/bin/sh
# story-gate lockdown for Claude Code (macOS / Linux / WSL). Run: sudo sh install.sh
# Adds ONE file; never edits managed-settings.json. Refuses to replace a file story-gate didn't write.
set -e
T="%s"
if [ -f "$T" ] && ! grep -q '"%s"' "$T"; then echo "Not changed: $T exists and isn't story-gate's."; exit 1; fi
mkdir -p "$(dirname "$T")"
cp "$(dirname "$0")/claude/%s" "$T"
chmod 644 "$T"
echo "story-gate lockdown is ON for Claude Code. Undo: sudo sh $(dirname "$0")/uninstall.sh"
""" % (target, LOCKDOWN_MARK, DROPIN)).encode("utf-8"))
    (dest / "uninstall.sh").write_bytes(("""#!/bin/sh
# Removes story-gate's lockdown file for Claude Code. Run: sudo sh uninstall.sh
set -e
T="%s"
if [ -f "$T" ] && grep -q '"%s"' "$T"; then rm -f "$T"; fi
rmdir "$(dirname "$T")" 2>/dev/null || true
echo "story-gate lockdown is OFF for Claude Code."
""" % (target, LOCKDOWN_MARK)).encode("utf-8"))
    (dest / "install.ps1").write_bytes(("""# story-gate lockdown for Claude Code (Windows). As Administrator: powershell -ExecutionPolicy Bypass -File install.ps1
# Adds ONE file; never edits managed-settings.json. Refuses to replace a file story-gate didn't write.
$ErrorActionPreference = "Stop"
$T = "%s"
if ((Test-Path $T) -and -not (Select-String -Path $T -SimpleMatch '"%s"' -Quiet)) { Write-Host "Not changed: $T exists and isn't story-gate's."; exit 1 }
New-Item -ItemType Directory -Force -Path (Split-Path $T) | Out-Null
Copy-Item (Join-Path $PSScriptRoot "claude\\%s") $T -Force
Write-Host "story-gate lockdown is ON for Claude Code. Undo, as Administrator: powershell -ExecutionPolicy Bypass -File uninstall.ps1"
""" % (target, LOCKDOWN_MARK, DROPIN)).encode("utf-8"))
    (dest / "uninstall.ps1").write_bytes(("""# Removes story-gate's lockdown file for Claude Code. As Administrator: powershell -ExecutionPolicy Bypass -File uninstall.ps1
$T = "%s"
if ((Test-Path $T) -and (Select-String -Path $T -SimpleMatch '"%s"' -Quiet)) { Remove-Item $T -Force }
Write-Host "story-gate lockdown is OFF for Claude Code."
""" % (target, LOCKDOWN_MARK)).encode("utf-8"))
    (dest / "README-IT.md").write_bytes(("""# story-gate lockdown: files for IT

Turns on Claude Code's `allowManagedHooksOnly` switch with one drop-in file, so Claude Code runs only managed hooks and
ignores hooks shipped inside repositories. Claude Code merges `managed-settings.json` with every `*.json` file in
`managed-settings.d/` (hook lists combine), so this never edits a `managed-settings.json` you already deploy.
Source: https://code.claude.com/docs/en/managed-settings

| Platform | Deploy `claude/%s` to |
|---|---|
| macOS (Jamf, Kandji, ...) | `/Library/Application Support/ClaudeCode/managed-settings.d/%s` |
| Linux / WSL | `/etc/claude-code/managed-settings.d/%s` |
| Windows (Intune, GPO, ...) | `C:\\\\Program Files\\\\ClaudeCode\\\\managed-settings.d\\\\%s` |

If you deliver Claude Code settings through MDM profiles or the HKLM registry, those take precedence over files: put
the same keys (`allowManagedHooksOnly`, `hooks`) in that channel instead.

The hooks call the story-gate runtime each person installs with `gate.py install --user` (it checks its own signature
and fingerprint). The path in this file is for the person who generated it (%s). For a fleet, set STORY_GATE_HOME to
the same folder for everyone, or generate one file per person with `gate.py lockdown --bundle <folder>`.
The file also carries a copy of that person's own Claude hooks, because user-level hooks stop running under this switch.

Check the files against SHA256SUMS. Run the scripts with `sudo sh install.sh` or, on Windows as Administrator,
`powershell -ExecutionPolicy Bypass -File install.ps1`. Undo: delete the one file (uninstall.sh / uninstall.ps1).
""" % (DROPIN, DROPIN, DROPIN, DROPIN, str(launcher).replace("\\", "/"))).encode("utf-8"))
    sums = []
    for f in sorted(p for p in dest.rglob("*") if p.is_file() and p.name != "SHA256SUMS"):
        sums.append("%s  %s" % (hashlib.sha256(f.read_bytes()).hexdigest(), f.relative_to(dest).as_posix()))
    (dest / "SHA256SUMS").write_bytes(("\n".join(sums) + "\n").encode("utf-8"))
    return dest, body


def client_matrix(top=None):
    """Honest per-tool status for hooks shipped inside repositories: hard / hard-on-change / partial / none."""
    st = lockdown_status()
    filt = bool(top) and filter_active(top)
    soft = "partial: checkout filter" if filt else "none"
    return [
        ("claude", "hard: lockdown (allowManagedHooksOnly)" if st["on"] else soft, "" if st["on"] else "turn on lockdown for a hard stop"),
        ("codex", "hard-on-change: Codex re-asks you to trust any changed project hook" + (" + checkout filter" if filt else ""), ""),
        ("cursor", soft, "no hard switch in Cursor yet"),
        ("gemini", soft, "turn on Gemini folder trust (security.folderTrust.enabled) for untrusted folders"),
        ("windsurf", soft, "no hard switch documented"),
        ("grok", soft, "trust folders only with /hooks-trust when you mean it"),
        ("vscode", soft, "VS Code's Workspace Trust decides whether a folder's own hooks run"),
    ]
