"""story-gate pinned project hooks: an approved hook runs only the default branch's copy of the repository files it uses.

Problem this solves: approval of a project hook (on the default branch) covers the command *text*, e.g.
`bash scripts/check.sh`. A branch can change scripts/check.sh without touching the settings file, so git never re-runs
the checkout filter and the AI tool would run the branch's version of the script.

How:
  * Every approved command is sorted into a tier:
      plain     - runs nothing from the repository (e.g. `ruff check`, `/usr/local/bin/lint`): left as is
      pinned    - listed in project_hooks_allowed with `pins` (the repo files/folders it uses), and simple enough to
                  rewrite safely: the checkout filter swaps it for the story-gate runner
      accepted  - listed with "runs_repo_code": "accepted": runs as is; doctor labels it unverified
      blocked   - runs repository code but isn't pinned or accepted: removed from the hook file on disk
  * The runner (`gate.py run-approved <id>`, from the signed runtime) re-checks at run time that the command is still
    approved and pinned on the default branch, that the pinned files in the working tree match the default branch
    exactly (no edits, no extra files), then runs the command with the pinned paths pointing at a protected copy taken
    from the default branch's commit. Anything else: refused (exit 2) with the exact files and the one command a
    human can use to trust their own edits (bound to that exact content).
See docs/hook-pinning.md for the threat model.
"""
import base64, hashlib, json, os, re, shlex, shutil, subprocess, sys, time
from pathlib import Path

import sg_trust as T

RUNNERS = {"npm", "npx", "yarn", "pnpm", "bun", "bunx", "make", "just", "task", "gradle", "gradlew", "mvn", "mvnw", "rake",
           "bundle", "poetry", "uv", "uvx", "pipenv", "tox", "nox", "cargo", "go", "deno", "dotnet", "composer", "pre-commit",
           "lefthook", "husky", "turbo", "nx"}  # run code chosen by repository files (package.json, Makefile, ...)
CONFIG_LOADERS = {"pytest", "py.test", "eslint", "prettier", "jest", "vitest", "mocha", "tsc", "webpack", "vite", "rollup",
                  "babel", "gulp", "grunt", "rspec", "phpunit", "playwright", "cypress", "stylelint", "commitlint", "lint-staged",
                  "tsx", "ts-node", "nodemon", "rails", "django-admin"}  # load code from repository config (conftest.py, *.config.js)
WRAPPERS = {"env", "time", "nice", "exec", "command", "sudo", "doas", "xargs", "nohup", "stdbuf", "timeout", "caffeinate",
            "start", "call"}  # run another program named later in the line
INTERPRETERS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "python", "python3", "py", "pythonw", "node", "deno", "bun", "ruby",
                "perl", "php", "pwsh", "powershell", "cmd", "lua", "rscript", "java", "osascript", "tclsh", "wscript", "cscript"}
CODE_FLAGS = {"-c", "-e", "-m", "-r", "-p", "-x", "--eval", "--require", "--import", "--loader", "--command", "-command",
              "-encodedcommand", "/c", "/k", "-file"}  # code or modules given on the command line instead of a pinned file
SCRIPT_EXT = re.compile(r"\.(sh|bash|zsh|ksh|py|pyw|js|mjs|cjs|ts|mts|cts|rb|pl|php|ps1|psm1|bat|cmd|lua|r|jar|exe|com)$", re.I)
PROJECT_VARS = ("CLAUDE_PROJECT_DIR", "CURSOR_PROJECT_DIR", "GEMINI_PROJECT_DIR", "CODEX_PROJECT_DIR", "PROJECT_DIR")
_VAR_PREFIX = re.compile(r"^(?:\$\{?(?:%s)\}?|%%(?:%s)%%)[/\\]" % ("|".join(PROJECT_VARS), "|".join(PROJECT_VARS)))
SHELL_META = re.compile(r"[|&;<>()`*?\[\]{}~!#\n\\%^]|\$")  # anything beyond plain words and quotes
ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
DEFAULT_MAX_AGE_DAYS = 7


# ------------------------------------------------------------------ policy
def entries(cfg):
    """project_hooks_allowed, normalised: {command: {"pins": [...], "accepted": bool}}. Plain strings have no pins."""
    out = {}
    for e in (cfg or {}).get("project_hooks_allowed") or []:
        if isinstance(e, str):
            out[e] = {"pins": [], "accepted": False}
        elif isinstance(e, dict) and isinstance(e.get("command"), str):
            pins = []
            for p in e.get("pins") or []:
                p = p.strip().replace("\\", "/") if isinstance(p, str) else ""
                p = p[2:] if p.startswith("./") else p
                p = p.rstrip("/")
                if (p and p != "." and ".." not in p.split("/") and "." not in p.split("/") and not p.startswith("/")
                        and not re.match(r"^[A-Za-z]:", p) and not re.search(r"[:*?\[\]\\]", p)):  # plain paths only, no pathspec magic
                    pins.append(p)
            out[e["command"]] = {"pins": pins, "accepted": e.get("runs_repo_code") == "accepted"}
    return out


def allowed_commands(cfg):
    return set(entries(cfg))


def policy(top):
    """(ref, commit, config) for an enrolled repository, read from the default branch; (None, None, {}) otherwise."""
    e = T.enrollment(top) or {}
    ref = e.get("policy_ref")
    sha = T.policy_commit(top, ref) if ref and e.get("enrolled") else None
    if not sha:
        return ref, None, {}
    try:
        cfg = json.loads(T.policy_text(top, ref, "config.json") or "{}")
    except ValueError:
        cfg = {}
    return ref, sha, cfg if isinstance(cfg, dict) else {}


def _git(top, *args, data=None):
    r = subprocess.run(["git", *args], cwd=str(top), input=data, capture_output=True)
    return r.stdout if r.returncode == 0 else None


# ------------------------------------------------------------------ classifying commands
def _rel(token):
    """The repository-relative path a command token names, if it is written as one."""
    t = _VAR_PREFIX.sub("", token, count=1)
    explicit = t != token or t.startswith("./")
    t = t[2:] if t.startswith("./") else t
    return t.replace("\\", "/"), explicit


def _head(tok):
    return re.sub(r"\.(exe|cmd|bat|ps1|com)$", "", os.path.basename(tok.replace("\\", "/")).lower())


def analyse(cmd, top, sha):
    """Default-deny reading of a hook command. -> dict(parse_ok, simple, runner, refs, outside, reasons).
    refs = repository paths the command uses (existing on the default branch, or written like a path/script);
    outside = paths that leave the repository; runner = the command runs code chosen some other way (package.json,
    Makefile, -c/-e/-m, wrappers, config files that are code). Only whole-token paths can be pinned."""
    info = {"parse_ok": True, "simple": "\\" not in cmd, "runner": False, "refs": [], "outside": [], "reasons": []}
    try:
        tokens = shlex.split(cmd.replace("\\", "/"), posix=True)
    except ValueError:
        info.update(parse_ok=False, simple=False)
        return info
    if not tokens:
        info["parse_ok"] = False
        return info
    if SHELL_META.search(" ".join(_VAR_PREFIX.sub("", t) for t in tokens)) or any(v in cmd for v in PROJECT_VARS) and not all(
            _VAR_PREFIX.match(t) for t in tokens if any(v in t for v in PROJECT_VARS)):
        info["simple"] = False
        info["reasons"].append("uses shell features")
    if any(ENV_ASSIGN.match(t) for t in tokens):
        info["simple"] = False
        info["reasons"].append("sets environment variables")
    head = _head(tokens[0])
    if head in WRAPPERS or head in RUNNERS or head in CONFIG_LOADERS:
        info["runner"] = True
        info["reasons"].append("%s runs code chosen by repository files" % head)
    interp = head in INTERPRETERS or bool(re.match(r"^python\d", head))
    if interp:
        if any(t.lower() in CODE_FLAGS or re.match(r"^-[a-z]*[cem]$", t.lower()) for t in tokens[1:]):
            info["runner"] = True
            info["reasons"].append("code given on the command line")
        script = next((t for t in tokens[1:] if not t.startswith("-")), None)
        if script is None:
            info["runner"] = True
            info["reasons"].append("interpreter without a script file")
    cands, whole = [], set()
    for i, tok in enumerate(tokens):
        parts = [tok] + [x for x in re.split(r"[=,:]", tok) if x and x != tok]
        if tok.startswith("-") and len(tok) > 2 and not tok.startswith("--"):
            parts.append(tok[2:])
        for j, part in enumerate(parts):
            rel, explicit = _rel(part)
            if not rel or re.match(r"^[A-Za-z]$", rel):
                continue
            if rel == ".." or rel.startswith("../") or "/../" in rel:
                info["outside"].append(part)
                continue
            if rel.startswith("/") or re.match(r"^[A-Za-z]:/", rel) or rel.startswith("-"):
                continue  # this computer's own absolute paths, and plain options
            pathlike = explicit or "/" in rel or bool(SCRIPT_EXT.search(rel)) or (interp and i > 0 and part == next(
                (t for t in tokens[1:] if not t.startswith("-")), None))
            cands.append((rel.rstrip("/"), pathlike, j == 0))
    if cands:
        found = []
        if sha:
            out = _git(top, "cat-file", "--batch-check", data=("\n".join("%s:%s" % (sha, c) for c, _, _ in cands) + "\n").encode()) or b""
            found = [not l.endswith("missing") for l in out.decode("utf-8", "replace").splitlines()]
        found += [False] * (len(cands) - len(found))
        for (c, pathlike, is_whole), exists in zip(cands, found):
            if exists or pathlike:
                if c not in info["refs"]:
                    info["refs"].append(c)
                if is_whole:
                    whole.add(c)
        if any(r not in whole for r in info["refs"]):
            info["simple"] = False  # a repo path inside a longer token (VAR=x, --opt=x, -fx): can't be redirected safely
            info["reasons"].append("repository path inside an option")
    return info


def covered(ref, pins):
    return any(ref == p or ref.startswith(p + "/") for p in pins)


def classify(cmd, top, sha, entry):
    """-> (tier, info). tier: plain | pinned | accepted | blocked. Anything not clearly plain or pinned is blocked
    unless a code owner explicitly accepted it."""
    info = analyse(cmd, top, sha)
    plain = info["parse_ok"] and info["simple"] and not info["runner"] and not info["refs"] and not info["outside"]
    if plain:
        return "plain", info
    if entry and entry.get("accepted"):
        return "accepted", info
    pins = (entry or {}).get("pins") or []
    if (pins and info["parse_ok"] and info["simple"] and not info["runner"] and not info["outside"]
            and info["refs"] and all(covered(r, pins) for r in info["refs"])):
        return "pinned", info
    return "blocked", info


# ------------------------------------------------------------------ wrapping (checkout filter side)
def encode(cmd):
    return base64.urlsafe_b64encode(cmd.encode("utf-8")).decode().rstrip("=")


def decode(token):
    return base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode("utf-8")


def wrap(cmd, py, launcher):
    return '"%s" -I "%s" run-approved %s' % (str(py).replace("\\", "/"), str(launcher).replace("\\", "/"), encode(cmd))


_WRAP_FULL = re.compile(r'"([^"]+)" -I "([^"]+)" run-approved ([A-Za-z0-9_-]+)')


def _same_path(a, b):
    n = lambda p: os.path.normcase(os.path.abspath(str(p).replace("\\", "/"))).replace("\\", "/")
    return n(a) == n(b)


def unwrap_cmd(c, launcher=None):
    """The original command if `c` is exactly our runner call through this computer's story-gate launcher; else None."""
    m = _WRAP_FULL.fullmatch(c) if isinstance(c, str) else None
    if not m or not _same_path(m.group(2), launcher or T.launcher_path()):
        return None
    try:
        return decode(m.group(3))
    except (ValueError, UnicodeDecodeError):
        return None


def _walk_commands(data, fn):
    """Apply fn(command) -> new command | None (drop entry) to every hook entry's "command" string."""
    dropped = []
    def walk(x, depth):
        if isinstance(x, dict):
            if depth and isinstance(x.get("command"), str):
                new = fn(x["command"])
                if new is None:
                    dropped.append(x["command"])
                    return None
                x = dict(x, command=new)
            out = {}
            for k, v in x.items():
                nv = walk(v, depth + 1)
                if nv is None or (isinstance(v, (dict, list)) and v and not nv):
                    continue
                out[k] = nv
            return out
        if isinstance(x, list):
            return [w for w in (walk(v, depth + 1) for v in x) if not (w is None or (w in ({}, [])))]
        return x
    return walk(data, 0), dropped


def smudge(data_bytes, top, path, py=None, launcher=None):
    """After the sanitizer: swap pinned commands for the runner, drop repository-code commands that aren't pinned or
    accepted. Returns (bytes, notes)."""
    if not path.endswith(".json"):
        return data_bytes, []
    try:
        data = json.loads(data_bytes.decode("utf-8")) if data_bytes.strip() else None
    except (ValueError, UnicodeDecodeError):
        return data_bytes, []
    if not isinstance(data, dict):
        return data_bytes, []
    _, sha, cfg = policy(top)
    ents = entries(cfg)
    py = py or sys.executable
    launcher = launcher or T.launcher_path()
    notes = []
    def fn(cmd):
        if unwrap_cmd(cmd) is not None or T.HOOK_SIGNATURE.search(cmd):
            return cmd  # already a story-gate command
        tier, info = classify(cmd, top, sha, ents.get(cmd))
        if tier == "pinned":
            return wrap(cmd, py, launcher)
        if tier == "blocked":
            notes.append(cmd)
            return None
        return cmd
    new, dropped = _walk_commands(data, fn)
    if not dropped and json.dumps(new, sort_keys=True) == json.dumps(data, sort_keys=True):
        return data_bytes, []
    return (json.dumps(new, indent=2) + "\n").encode("utf-8"), notes


def unwrap_bytes(data_bytes):
    """Our filtered copy with every runner command turned back into the original (for the clean filter)."""
    try:
        data = json.loads(data_bytes.decode("utf-8")) if data_bytes.strip() else None
    except (ValueError, UnicodeDecodeError):
        return data_bytes
    if not isinstance(data, (dict, list)):
        return data_bytes
    changed = []
    def fn(cmd):
        orig = unwrap_cmd(cmd)
        if orig is not None:
            changed.append(1)
            return orig
        return cmd
    new, _ = _walk_commands(data, fn)
    return (json.dumps(new, indent=2) + "\n").encode("utf-8") if changed else data_bytes


# ------------------------------------------------------------------ the runner (AI tool side)
def _skip(rel):
    """Bytecode Python leaves in YOUR working tree. Ignored only where nothing runs from the working tree (the default
    path runs the protected copy). The protected copy and trusted-content hashes count every file."""
    parts = rel.split("/")
    return "__pycache__" in parts or rel.endswith((".pyc", ".pyo"))


def _tree(top, sha, pins):
    """{path: (mode, oid)} for the pinned paths in the default branch's commit."""
    raw = _git(top, "ls-tree", "-r", "-z", "--full-tree", sha, "--", *pins)
    if raw is None:
        raise T.TrustError("can't read the default branch's files")
    out = {}
    for row in filter(None, raw.split(b"\0")):
        meta, name = row.split(b"\t", 1)
        mode, kind, oid = meta.decode().split()
        out[name.decode("utf-8", "surrogateescape")] = (mode, kind, oid)
    return out


def _walk(root, rel):
    """Every file and symlink under top/rel (not following links), ignored files included."""
    base = os.path.join(root, *rel.split("/"))
    if os.path.islink(base) or os.path.isfile(base):
        return [rel]
    found = []
    for d, dirs, files in os.walk(base):
        for name in dirs + files:
            full = os.path.join(d, name)
            r = os.path.relpath(full, root).replace("\\", "/")
            if name in dirs and not os.path.islink(full):
                continue
            found.append(r)
        dirs[:] = [x for x in dirs if not os.path.islink(os.path.join(d, x))]
    return found


def differing(top, sha, pins):
    """Pinned paths whose working-tree content isn't exactly the default branch's: edited, deleted, retyped as a
    symlink, or extra files (ignored ones included) inside a pinned folder. Compares content hashes, so git's stat cache,
    assume-unchanged and skip-worktree can't hide a change."""
    tree = _tree(top, sha, pins)
    out = set(p for p in pins if not any(n == p or n.startswith(p + "/") for n in tree))
    for p in pins:  # a symlinked folder on the way to a pin can point anywhere
        parts = p.split("/")
        for i in range(1, len(parts) + 1):
            if os.path.islink(os.path.join(top, *parts[:i])):
                out.add("/".join(parts[:i]))
    regular = []
    for n, (mode, kind, oid) in tree.items():
        f = os.path.join(top, *n.split("/"))
        if os.path.islink(f) or not os.path.isfile(f):
            out.add(n)
        else:
            regular.append(n)
    if regular:
        r = subprocess.run(["git", "hash-object", "--stdin-paths"], cwd=str(top), input="\n".join(regular) + "\n",
                           capture_output=True, text=True, encoding="utf-8")
        oids = r.stdout.split() if r.returncode == 0 else []
        if len(oids) != len(regular):
            out.add("(unreadable)")
        else:
            out |= {n for n, o in zip(regular, oids) if o != tree[n][2]}
    for p in pins:
        out |= {f for f in _walk(top, p) if f not in tree and not _skip(f)}
    return sorted(out)


def content_hash(top, pins):
    """Hash of exactly what is in the working tree under the pins (content, mode, links, ignored files)."""
    h = hashlib.sha256()
    for rel in sorted({f for p in pins for f in _walk(top, p)}):  # bytecode included: a trusted run executes the working tree
        full = os.path.join(top, *rel.split("/"))
        st = os.lstat(full)
        h.update(rel.encode("utf-8", "surrogateescape") + b"\0" + str(st.st_mode).encode())
        if os.path.islink(full):
            h.update(b"link:" + os.readlink(full).encode("utf-8", "surrogateescape"))
        else:
            with open(full, "rb") as f:
                h.update(hashlib.sha256(f.read()).digest())
    return h.hexdigest()


def trust_path():
    return T.G.config_dir() / "local-hook-trust.json"


def trusted_locally(top, cmd, pins):
    ident = T.repo_identity(top)[1]
    rec = T.read_json(trust_path()).get(ident, {}).get(cmd) if ident else None
    try:
        return bool(rec and rec.get("content_sha256") == content_hash(top, pins))
    except OSError:
        return False


def trust_local(top, cmd, revoke=False):
    """Human-only: run my own edited copy of this pinned hook's files, for exactly this content. Logged."""
    _, sha, cfg = policy(top)
    ent = entries(cfg).get(cmd)
    if not ent or not ent["pins"]:
        raise T.TrustError("'%s' isn't a pinned hook in project_hooks_allowed on the default branch" % cmd)
    data = T.read_json(trust_path())
    ident = T.repo_identity(top)[1]
    if not ident:
        raise T.TrustError("can't identify this repository")
    if revoke:
        data.get(ident, {}).pop(cmd, None)
    else:
        try:
            digest = content_hash(top, ent["pins"])
        except OSError as ex:
            raise T.TrustError("can't read the pinned files: %s" % ex)
        data.setdefault(ident, {})[cmd] = {"content_sha256": digest, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                           "by": os.environ.get("USER") or os.environ.get("USERNAME") or "?"}
    T.write_json_atomic(trust_path(), data)
    return ent["pins"]


def cache_root():
    return T.G.config_dir() / "approved-cache"


def _cache_ok(top, dest, tree):
    files = sorted(n for n in tree)
    have = {f for f in _walk(str(dest), ".") if f != ".complete"} if dest.is_dir() else set()  # any extra file (bytecode too) fails
    have = {f[2:] if f.startswith("./") else f for f in have}
    if have != set(files):
        return False
    r = subprocess.run(["git", "hash-object", "--no-filters", "--stdin-paths"], cwd=str(top),
                       input="\n".join(str(dest.joinpath(*n.split("/"))) for n in files) + "\n", capture_output=True, text=True)
    oids = r.stdout.split() if r.returncode == 0 else []
    return len(oids) == len(files) and all(o == tree[n][2] for n, o in zip(files, oids))


def ensure_cache(top, sha, pins):
    """A protected copy of the pinned files from the default branch's commit. Re-verified against the commit's hashes
    every time it is used, so nothing planted in the cache can run."""
    tree = _tree(top, sha, pins)
    if not tree:
        raise T.TrustError("the pinned files aren't on the default branch (%s)" % ", ".join(pins))
    for n, (mode, kind, oid) in tree.items():
        if kind != "blob" or mode not in ("100644", "100755"):
            raise T.TrustError("%s is a %s on the default branch; pinned hooks run regular files only" % (n, "symlink" if mode == "120000" else kind))
        if ".." in n.split("/") or n.startswith("/"):
            raise T.TrustError("unsafe path in the default branch: %s" % n)
    key = hashlib.sha256(("%s\0%s" % (sha, "\0".join(sorted(pins)))).encode()).hexdigest()[:24]
    dest = cache_root() / key
    if (dest / ".complete").is_file() and _cache_ok(top, dest, tree):
        return dest
    tmp = cache_root() / (key + ".tmp-%d-%d" % (os.getpid(), int(time.time() * 1000)))
    for n, (mode, kind, oid) in tree.items():
        blob = _git(top, "cat-file", "blob", oid)
        if blob is None:
            shutil.rmtree(tmp, ignore_errors=True)
            raise T.TrustError("can't read %s from the default branch" % n)
        f = tmp.joinpath(*n.split("/"))
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(blob)
        f.chmod(0o555 if mode == "100755" else 0o444)  # read-only
    (tmp / ".complete").write_text(sha, encoding="utf-8")
    if dest.exists():
        _rm(dest)
    try:
        os.replace(tmp, dest)
    except OSError:  # another hook built it at the same moment
        _rm(tmp)
    if not _cache_ok(top, dest, tree):
        raise T.TrustError("the protected copy of the pinned files failed its check")
    return dest


def _rm(p):
    def onerr(func, path, _):
        try:
            os.chmod(path, 0o700)
            func(path)
        except OSError:
            pass
    shutil.rmtree(p, onerror=onerr)


def rewrite(cmd, cache, pins):
    """The command's words with every pinned path pointing at the protected copy (only whole-word paths are pinned)."""
    out = []
    for t in shlex.split(cmd.replace("\\", "/"), posix=True):
        rel, _ = _rel(t)
        rel = rel.rstrip("/")
        out.append(str(cache.joinpath(*rel.split("/"))).replace("\\", "/") if rel and covered(rel, pins) else t)
    return out


def _git_bash():
    git = shutil.which("git")
    if git:
        root = Path(git).resolve().parent.parent  # ...\Git\cmd\git.exe -> ...\Git
        for cand in (root / "bin" / "bash.exe", root / "usr" / "bin" / "bash.exe"):
            if cand.is_file():
                return str(cand)
    return None


def run_argv(tokens=None, cmdline=None):
    """How the command runs: POSIX sh; on Windows Git Bash (what Claude Code uses, never WSL's bash), else cmd.exe.
    Returns (args, use_string)."""
    if os.name == "nt":
        bash = _git_bash()
        if bash:
            return [bash, "-c", shlex.join(tokens) if tokens is not None else cmdline], False
        line = subprocess.list2cmdline(tokens) if tokens is not None else cmdline
        return 'cmd.exe /d /s /c "%s"' % line, True  # /s strips exactly the outer quotes we add
    return ["/bin/sh", "-c", shlex.join(tokens) if tokens is not None else cmdline], False


def _inside(path, root):
    try:
        a = os.path.normcase(os.path.realpath(path)).lower()
        b = os.path.normcase(os.path.realpath(root)).lower()
        return os.path.commonpath([a, b]) == b
    except ValueError:
        return False


def policy_age_days(top):
    try:
        fh = Path(_git(top, "rev-parse", "--git-common-dir").decode().strip())
        fh = (fh if fh.is_absolute() else Path(top) / fh) / "FETCH_HEAD"
        return (time.time() - fh.stat().st_mtime) / 86400 if fh.is_file() else None
    except Exception:
        return None


def refuse(msg):
    sys.stderr.write("story-gate: %s\n" % msg)
    return 2


def run_approved(token):
    """Entry point for `gate.py run-approved <id>`. Never runs anything it can't vouch for."""
    os.environ["NoDefaultCurrentDirectoryInExePath"] = "1"  # Windows: never pick up a program from the repository folder
    try:
        cmd = decode(token)
    except (ValueError, UnicodeDecodeError):
        return refuse("this hook entry is damaged; re-check it out (git checkout -- <hook file>)")
    start = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    top = T.repo_identity(start)[0]
    if not top:
        return refuse("hook '%s' not run: not inside a git repository" % cmd)
    ref, sha, cfg = policy(top)
    if not sha:
        return refuse("hook '%s' not run: this repository isn't enrolled, or its default branch (%s) can't be found. "
                      "Fetch it (git fetch) or run gate.py enroll" % (cmd, ref or "?"))
    ent = entries(cfg).get(cmd)
    tier, _ = classify(cmd, top, sha, ent)
    if tier != "pinned":
        return refuse("hook '%s' not run: it isn't approved with pins on %s any more (tier: %s)" % (cmd, ref, tier))
    pins = ent["pins"]
    age = policy_age_days(top)
    limit = cfg.get("policy_max_age_days", DEFAULT_MAX_AGE_DAYS)
    if age is not None and isinstance(limit, (int, float)) and age > limit:
        sys.stderr.write("story-gate: warning: your copy of %s is %d days old; run git fetch so hooks use current approvals\n" % (ref, age))
    try:
        diff = differing(top, sha, pins)
    except (T.TrustError, OSError) as ex:
        return refuse("hook '%s' not run: %s" % (cmd, ex))
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep) if p and not _inside(p, top))  # no repo programs
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # keep the protected copy exactly as verified
    if diff:
        if not trusted_locally(top, cmd, pins):
            return refuse("hook '%s' not run: %s differ%s from %s, so this could be a branch's code. If these are your own "
                          "edits, a human can allow exactly this content with: gate.py hook-trust \"%s\""
                          % (cmd, ", ".join(diff[:5]), "s" if len(diff) == 1 else "", ref, cmd))
        args, as_string = run_argv(cmdline=cmd)  # your own edits, explicitly trusted for this exact content
    else:
        try:
            cache = ensure_cache(top, sha, pins)
        except (T.TrustError, OSError) as ex:
            return refuse("hook '%s' not run: %s" % (cmd, ex))
        words = rewrite(cmd, cache, pins)
        first = Path(words[0])
        if first.is_absolute() and _inside(first, cache) and first.is_file():  # the pinned script is run directly
            line = first.read_bytes()[:256].split(b"\n", 1)[0]
            if line.startswith(b"#!"):
                interp = line[2:].strip().split()[0] if line[2:].strip() else b""
                name = interp.decode("utf-8", "replace")
                absolute = name.startswith("/") or (os.name == "nt" and bool(re.match(r"^[A-Za-z]:[/\\]", name)))  # C:/ only on Windows
                if not absolute or _inside(name, top):
                    return refuse("hook '%s' not run: its script's interpreter (%s) %s, which would run a program from the "
                                  "repository" % (cmd, name, "is a relative path" if not absolute else "is inside the repository"))
        args, as_string = run_argv(tokens=words)
    try:
        return subprocess.run(args, cwd=start if os.path.isdir(start) else top, env=env).returncode  # stdin/out/err pass through
    except OSError as ex:
        return refuse("hook '%s' could not start: %s" % (cmd, ex))


# ------------------------------------------------------------------ reporting (doctor, CI)
_REF_PATTERNS = (re.compile(r"""(?:^|\s)(?:source|\.)\s+["']?([\w./${}-]+)"""),
                 re.compile(r"""\$\(dirname\s+["']?\$\{?0\}?["']?\)/([\w./-]+)"""),
                 re.compile(r"""require\(\s*["'](\.{1,2}/[^"']+)["']\s*\)"""),
                 re.compile(r"""from\s+["'](\.{1,2}/[^"']+)["']"""),
                 re.compile(r"""(?:^|\s)(?:from|import)\s+([A-Za-z_][\w.]*)""", re.M),
                 re.compile(r"""["']([\w./-]+\.(?:sh|bash|py|js|mjs|cjs|ts|rb|ps1|pl|json|toml|ya?ml))["']"""))


def unpinned_references(top, sha, pins):
    """Best-effort: repository files the pinned scripts appear to use that aren't pinned. A hint, not proof."""
    listing = (_git(top, "ls-tree", "-r", "-z", "--name-only", sha) or b"").decode("utf-8", "replace").split("\0")
    tree = set(filter(None, listing))
    found = set()
    for name in [n for n in tree if covered(n, pins)]:
        blob = _git(top, "cat-file", "blob", "%s:%s" % (sha, name)) or b""
        if len(blob) > 300000 or b"\0" in blob[:2000]:
            continue
        text = blob.decode("utf-8", "replace")
        base = os.path.dirname(name)
        for pat in _REF_PATTERNS:
            for m in pat.finditer(text):
                ref = m.group(1).replace("${", "").replace("}", "")
                cands = [os.path.normpath(os.path.join(base, ref)).replace("\\", "/"), ref.lstrip("./")]
                if "." in ref and "/" not in ref and pat is _REF_PATTERNS[4]:  # python dotted module
                    mod = ref.replace(".", "/")
                    cands += [os.path.join(base, mod + ".py").replace("\\", "/"), mod + ".py", mod + "/__init__.py"]
                elif pat is _REF_PATTERNS[4]:
                    cands += [os.path.join(base, ref + ".py").replace("\\", "/"), ref + ".py", ref + "/__init__.py"]
                for c in cands:
                    if c in tree and not covered(c, pins):
                        found.add(c)
    return sorted(found)


def report(top, default_hook_commands=()):
    """Every project hook command the default branch approves, with its tier, for doctor and CI."""
    ref, sha, cfg = policy(top)
    if not sha:
        return []
    ents = entries(cfg)
    rows = []
    for cmd in sorted(set(ents) | set(default_hook_commands)):
        if T.HOOK_SIGNATURE.search(cmd) or unwrap_cmd(cmd) is not None:
            continue
        tier, info = classify(cmd, top, sha, ents.get(cmd))
        hint = ""
        if tier == "pinned":
            extra = unpinned_references(top, sha, ents[cmd]["pins"])
            hint = ("pins may be incomplete - the scripts seem to use: %s" % ", ".join(extra[:5])) if extra else ""
        elif tier == "blocked":
            if info["runner"] or not info["simple"] or info["outside"] or not info["parse_ok"]:
                hint = ('runs code chosen by repository files; can only run if a code owner sets "runs_repo_code": "accepted" '
                        '(unverified) in project_hooks_allowed')
            else:
                hint = 'add {"command": %s, "pins": %s} to project_hooks_allowed on %s' % (json.dumps(cmd), json.dumps(sorted(set(info["refs"]))), ref)
        rows.append((cmd, tier, hint))
    return rows
