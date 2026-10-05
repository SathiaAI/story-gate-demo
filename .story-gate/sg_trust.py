"""story-gate trusted local runtime: the same rule CI follows, applied on the developer's computer.

- Hooks run a copy of story-gate installed in YOUR user folder (pinned by sha256), never the copy in the
  checked-out branch. A branch can't swap the gate code.
- Settings (mode, thresholds, judge) come from the repository's default branch (`git show origin/HEAD:...`).
  The working tree may only make them stricter. A branch can't loosen the rules.
- Hooks are registered in each AI client's USER settings (~/.claude, ~/.codex, ~/.cursor, ~/.gemini), outside
  any repository. Windsurf/Devin and Grok have no verified user-level hooks yet: they run in reduced protection.
- Upgrades are explicit and verified against the story-gate release key with `ssh-keygen -Y verify`.

Stdlib only. Nothing here talks to the network.
"""
import difflib, hashlib, json, os, re, shutil, subprocess, tempfile, time
from pathlib import Path


# Public key that signs story-gate releases. Its fingerprint is published on the GitHub release page; compare
# them the first time you install (`gate.py install --user` prints it).
RELEASE_NAMESPACE = "story-gate-release"
RELEASE_SIGNERS = "story-gate-release ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIN8LkolTGexkEFLGhW/dq7fmyAMw8qW8tsQUx1koD3y"
RELEASE_FINGERPRINT = "SHA256:YN6hCUUHe1XHbhYDj1VdYwoeVoIWDDlFJ6yEXDAcR+4"
RUNTIME_GLOBS = ("*.py", "PROTOCOL.md", "SKILL.md", "vendor/*")
HOOK_SIGNATURE = re.compile(r'(?:gate|launch)\.py"?\s+hook\s+--client')  # every story-gate hook command, repo or runtime
LAUNCHER = '''"""story-gate launcher: hooks and git filters call this stable path; it runs the active, pinned runtime."""
import json, os, subprocess, sys
here = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("STORY_GATE_HOME", os.path.dirname(here))
try:
    with open(os.path.join(here, "active.json"), encoding="utf-8") as f:
        gate = os.path.join(json.load(f)["dir"], "gate.py")
except Exception:
    sys.stderr.write("story-gate: the trusted runtime is missing. Run: gate.py install --user\\n")
    sys.exit(2)
sys.exit(subprocess.run([sys.executable, "-I", gate] + sys.argv[1:]).returncode)
'''
USER_CLIENTS = ("claude", "codex", "cursor", "gemini", "windsurf", "vscode", "hermes")
# Set up by default even when the tool isn't found (older installs did); vscode and hermes only when found on this computer.
ALWAYS_CLIENTS = ("claude", "codex", "cursor", "gemini", "windsurf")
DEGRADED_CLIENTS = ("grok",)  # user-level location documented, but merging with project hooks is unverified; see docs/client-security.md


class _LazyGitHub:  # sg_github pulls in http/xml modules; load it only when needed so hooks stay fast
    def __getattr__(self, name):
        import sg_github
        return getattr(sg_github, name)


G = _LazyGitHub()


class TrustError(Exception):
    pass


# ------------------------------------------------------------------ locations
def user_home():
    return Path(os.environ.get("STORY_GATE_USER_HOME") or Path.home())


def hermes_home():
    """Hermes Agent's data folder: HERMES_HOME, else %LOCALAPPDATA%\\hermes on Windows, else ~/.hermes.
    Source: hermes-agent hermes_constants.get_hermes_home (installation docs: Windows uses %LOCALAPPDATA%\\hermes)."""
    if os.environ.get("HERMES_HOME", "").strip():
        return Path(os.path.expanduser(os.path.expandvars(os.environ["HERMES_HOME"].strip())))
    if os.name == "nt" and os.environ.get("LOCALAPPDATA") and not os.environ.get("STORY_GATE_USER_HOME"):
        return Path(os.environ["LOCALAPPDATA"]) / "hermes"
    return user_home() / ".hermes"


def detected_clients():
    """The AI tools whose settings folder exists on this computer (a hint for the setup page, never a security decision)."""
    h = user_home()
    marks = {"claude": [h / ".claude"], "codex": [h / ".codex"], "cursor": [h / ".cursor"], "gemini": [h / ".gemini"],
             "windsurf": [h / ".codeium" / "windsurf"], "vscode": [h / ".copilot", h / ".vscode"], "hermes": [hermes_home()]}
    return [cl for cl in USER_CLIENTS if any(p.is_dir() for p in marks[cl])]


def default_clients():
    found = detected_clients()
    return [cl for cl in USER_CLIENTS if cl in ALWAYS_CLIENTS or cl in found]


def runtime_root():
    return G.config_dir() / "runtime"


def enrolled_path():
    return G.config_dir() / "enrolled.json"


def active_path():
    return runtime_root() / "active.json"


def read_json(p, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return {} if default is None else default


def write_json_atomic(p, obj):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp-%d" % os.getpid())
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def is_runtime(script_dir):
    """True when this gate.py is running from the installed user runtime (not from a repository)."""
    try:
        sd = Path(script_dir).resolve()
        return sd.parent == runtime_root().resolve() and (sd / "manifest.json").is_file()
    except Exception:
        return False


# ------------------------------------------------------------------ manifests and signatures
def sha256(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def runtime_files(src):
    src = Path(src)
    out = set()
    for g in RUNTIME_GLOBS:
        for f in src.glob(g):
            if f.is_file() and not f.is_symlink() and "__pycache__" not in f.parts:
                out.add(f.relative_to(src).as_posix())
    return sorted(out)


def make_manifest(src, version):
    return {"version": version, "files": {f: sha256(Path(src) / f) for f in runtime_files(src)}}


def ssh_keygen():
    cands = [shutil.which("ssh-keygen")]
    if os.name == "nt":
        cands += [r"C:\Windows\System32\OpenSSH\ssh-keygen.exe", r"C:\Program Files\Git\usr\bin\ssh-keygen.exe"]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    raise TrustError("ssh-keygen not found (it comes with OpenSSH or Git for Windows); it is needed to check release signatures")


def key_fingerprint(signers=RELEASE_SIGNERS):
    """sha256 fingerprint of the release public key, in the form ssh-keygen prints."""
    import base64
    parts = signers.split()
    try:
        blob = base64.b64decode(parts[2])
    except Exception:
        return "unknown"
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def verify_release(src, signers=RELEASE_SIGNERS):
    """src holds release.json + release.json.sig. Returns the manifest if the signature is valid and every file matches."""
    src = Path(src)
    rel, sig = src / "release.json", src / "release.json.sig"
    if not rel.is_file() or not sig.is_file():
        raise TrustError("no signed release in %s (release.json + release.json.sig). Use a tagged story-gate release, "
                         "or pass --unsigned to install a development copy at your own risk" % src)
    with tempfile.TemporaryDirectory() as td:
        allowed = Path(td) / "allowed_signers"
        allowed.write_text(signers.strip() + "\n", encoding="utf-8")
        with open(rel, "rb") as fh:
            r = subprocess.run([ssh_keygen(), "-Y", "verify", "-f", str(allowed), "-I", RELEASE_NAMESPACE,
                                "-n", RELEASE_NAMESPACE, "-s", str(sig)], stdin=fh, capture_output=True, text=True)
    if r.returncode != 0:
        raise TrustError("release signature is NOT valid for the story-gate release key (%s)" % (r.stderr or r.stdout).strip()[:200])
    man = read_json(rel)
    files = man.get("files") or {}
    if not files:
        raise TrustError("signed release lists no files")
    bad = [f for f, h in files.items() if not (src / f).is_file() or sha256(src / f) != h]
    if bad:
        raise TrustError("files differ from the signed release: %s" % ", ".join(bad[:5]))
    extra = [f for f in runtime_files(src) if f not in files]
    if extra:
        raise TrustError("files not covered by the signed release: %s" % ", ".join(extra[:5]))
    return man


def sign_release(src, key, version):
    """Maintainer only: write release.json and release.json.sig for the files in src."""
    src = Path(src)
    man = make_manifest(src, version)
    (src / "release.json").write_text(json.dumps(man, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    sig = src / "release.json.sig"
    if sig.exists():
        sig.unlink()
    r = subprocess.run([ssh_keygen(), "-Y", "sign", "-f", str(key), "-n", RELEASE_NAMESPACE, str(src / "release.json")],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise TrustError("signing failed: %s" % (r.stderr or r.stdout).strip()[:200])
    return man


def fetch_release(url, dest):
    """Download a release archive over HTTPS (zip or tar.gz) and return the folder that holds its signed .story-gate files.
    Nothing in it is trusted until verify_release() has checked the signature."""
    import io, tarfile, urllib.request, zipfile
    if not url.startswith("https://"):
        raise TrustError("upgrades are only downloaded over https://")
    data = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "story-gate"}), timeout=60).read()
    if len(data) > 50_000_000:
        raise TrustError("release archive is unexpectedly large")
    dest = Path(dest)
    def safe(name):
        p = (dest / name).resolve()
        if not str(p).startswith(str(dest.resolve()) + os.sep):
            raise TrustError("archive tries to write outside its folder: %s" % name)
        return p
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for m in z.infolist():
                if m.is_dir():
                    continue
                p = safe(m.filename); p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(z.read(m))
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as t:
            for m in t.getmembers():
                if not m.isfile():
                    continue  # links and devices are never extracted
                p = safe(m.name); p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(t.extractfile(m).read())
    hits = sorted(dest.rglob("release.json"), key=lambda x: len(x.parts))
    if not hits:
        raise TrustError("the archive has no signed release (release.json)")
    return hits[0].parent


# ------------------------------------------------------------------ runtime install / verify
def install_runtime(src, version, unsigned=False, signers=RELEASE_SIGNERS):
    """Copy the release in src into runtime/<version>, verify, then activate atomically. Returns the version dir."""
    src = Path(src)
    signed = None
    if not unsigned:
        signed = verify_release(src, signers)
        version = signed.get("version") or version
    root = runtime_root()
    root.mkdir(parents=True, exist_ok=True)
    G.lock_down(root.parent, directory=True)
    dest = root / ("%s%s" % (version, "-unsigned" if unsigned else ""))
    stage = root / (".staging-%d-%d" % (os.getpid(), int(time.time())))
    if stage.exists():
        shutil.rmtree(stage)
    for f in runtime_files(src):
        (stage / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src / f, stage / f)
    man = make_manifest(stage, version)
    if signed and man["files"] != {f: h for f, h in signed["files"].items() if f in man["files"]}:
        shutil.rmtree(stage)
        raise TrustError("copied files do not match the signed release")
    man.update({"signed": bool(signed), "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "signer": key_fingerprint(signers) if signed else None})
    (stage / "manifest.json").write_text(json.dumps(man, indent=2) + "\n", encoding="utf-8")
    if dest.exists():
        shutil.rmtree(dest)
    os.replace(stage, dest)
    old = read_json(active_path())
    launcher_path().write_text(LAUNCHER, encoding="utf-8")
    write_json_atomic(active_path(), {"version": version, "dir": str(dest), "previous": old.get("dir"),
                                      "previous_manifest_sha256": old.get("manifest_sha256"), "signed": bool(signed),
                                      "manifest_sha256": sha256(dest / "manifest.json"), "launcher_sha256": sha256(launcher_path()),
                                      "shim_python": old.get("shim_python")})
    return dest


def launcher_path():
    return runtime_root() / "launch.py"


def shim_dir():
    return runtime_root() / "bin"


def shim_files(py):
    """The `story-gate` command: a two-line wrapper that runs the verified runtime through the launcher, never a repository's copy."""
    py, launcher = str(py), str(launcher_path())
    return {"story-gate": '#!/bin/sh\n# story-gate: runs the verified story-gate installed on this computer\nexec "%s" -I "%s" "$@"\n'
            % (py.replace("\\", "/"), launcher.replace("\\", "/")),
            "story-gate.cmd": '@echo off\r\nrem story-gate: runs the verified story-gate installed on this computer\r\n"%s" -I "%s" %%*\r\n'
            % (py.replace("/", "\\") if os.name == "nt" else py, launcher.replace("/", "\\") if os.name == "nt" else launcher)}


def write_shims(py):
    d = shim_dir()
    d.mkdir(parents=True, exist_ok=True)
    for name, body in shim_files(py).items():
        f = d / name
        f.write_bytes(body.encode("utf-8"))
        if name == "story-gate" and os.name != "nt":
            f.chmod(0o755)
    act = read_json(active_path())
    if act:
        write_json_atomic(active_path(), dict(act, shim_python=str(py)))  # so doctor can check the wrappers byte for byte
    return d


def shim_problems(py=None):
    """Doctor's check: the wrappers say exactly what install wrote, and `story-gate` on PATH (if any) is ours."""
    py = py or read_json(active_path()).get("shim_python")
    if not py:
        return ["the story-gate command isn't installed yet (gate.py install --user adds it)"]
    probs = []
    for name, body in shim_files(py).items():
        f = shim_dir() / name
        if not f.is_file() or f.read_bytes() != body.encode("utf-8"):
            probs.append("%s was changed or is missing (gate.py install --user rewrites it)" % f)
    found = shutil.which("story-gate")
    if found and Path(found).resolve().parent != shim_dir().resolve() and not leads_to_runtime(found):
        probs.append("`story-gate` on PATH is %s, not story-gate's own command in %s" % (found, shim_dir()))
    return probs


def leads_to_runtime(exe):
    """True when this `story-gate` (e.g. the one `uv tool install` puts on PATH) is story-gate's own console script,
    which hands every command to the active runtime. Read-only: the file is inspected, never run."""
    try:
        with open(exe, "rb") as f:
            head = f.read(1 << 20)  # uv's Windows launcher embeds the script; on macOS/Linux it is a short Python file
        return bool(re.search(rb"(?m)^from story_gate\.cli import main\r?$", head))  # the console-script import, not a comment
    except OSError:
        return False


def launcher_ok():
    p = launcher_path()
    return p.is_file() and p.read_text(encoding="utf-8") == LAUNCHER and read_json(active_path()).get("launcher_sha256") == sha256(p)


# ------------------------------------------------------------------ install manifest (so uninstall can undo everything)
def manifest_path():
    return G.config_dir() / "install-manifest.json"


def record(kind, key, **data):
    """Remember one change we made (first write wins for 'before' state), so uninstall can put it back exactly."""
    m = read_json(manifest_path())
    entry = m.setdefault(kind, {}).setdefault(key, {})
    for k, v in data.items():
        if k.endswith("_before") and k in entry:
            continue
        entry[k] = v
    write_json_atomic(manifest_path(), m)


def forget(kind, key):
    m = read_json(manifest_path())
    if key in m.get(kind, {}):
        del m[kind][key]
        write_json_atomic(manifest_path(), m)


def runtime_problems(sd, manifest_sha256):
    """Integrity of one runtime folder: its manifest must have the recorded hash and every file must match the manifest."""
    sd = Path(sd).resolve()
    problems = []
    mp = sd / "manifest.json"
    if not manifest_sha256 or not mp.is_file() or sha256(mp) != manifest_sha256:
        problems.append("runtime manifest was changed after install")
    man = read_json(mp)
    for f, h in (man.get("files") or {}).items():
        p = sd / f
        if not p.is_file() or sha256(p) != h:
            problems.append("runtime file changed after install: %s" % f)
    extra = [f for f in runtime_files(sd) if f not in (man.get("files") or {})]
    if extra:
        problems.append("unexpected files in the runtime: %s" % ", ".join(extra[:5]))
    return problems


def verify_self(script_dir):
    """The running runtime must match its install-time manifest and be the active one. Returns a list of problems."""
    sd = Path(script_dir).resolve()
    problems = []
    act = read_json(active_path())
    if not act or Path(act.get("dir", "")).resolve() != sd:
        problems.append("this runtime is not the active story-gate runtime (%s)" % sd)
    if act.get("manifest_sha256"):
        problems += runtime_problems(sd, act["manifest_sha256"])
    else:
        problems += [x for x in runtime_problems(sd, None) if "manifest was changed" not in x]
    if act and not launcher_ok():
        problems.append("the launcher (%s) was changed after install" % launcher_path())
    return problems


def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:3]) or (0,)


# ------------------------------------------------------------------ repositories: enrollment and policy
GIT_TIMEOUT = 15  # seconds: a hung git (index lock, fsmonitor, slow network drive) must not outlast the client's hook timeout


def git_in(cwd, *args):
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return ""  # callers treat "" as unknown, so policy reads fail closed
    return r.stdout.strip() if r.returncode == 0 else ""


def repo_identity(cwd):
    """(toplevel, identity) for the repository containing cwd. Worktrees share one identity (the common git dir)."""
    top = git_in(cwd, "rev-parse", "--show-toplevel")
    if not top:
        return None, None
    common = git_in(cwd, "rev-parse", "--git-common-dir")
    common = os.path.realpath(os.path.join(cwd, common)) if common and not os.path.isabs(common) else os.path.realpath(common or top)  # git prints it relative to cwd
    return os.path.realpath(top), os.path.normcase(common)


def default_policy_ref(cwd, base_branch="main"):
    head = git_in(cwd, "symbolic-ref", "-q", "refs/remotes/origin/HEAD")
    if head:
        return head.replace("refs/remotes/", "", 1)
    for ref in ("origin/" + base_branch, base_branch, "origin/master", "master"):
        if git_in(cwd, "rev-parse", "--verify", "-q", ref + "^{commit}"):
            return ref
    return None


def enrollment(cwd):
    top, ident = repo_identity(cwd)
    if not ident:
        return None
    rec = read_json(enrolled_path()).get(ident)
    return dict(rec, toplevel=top, identity=ident) if rec else {"toplevel": top, "identity": ident, "enrolled": False}


LOCAL_POLICY_RISK = ("a local branch can be moved by anything running on this computer, including an AI agent, so it is a "
                     "weaker source of policy than a remote's branch")


def is_remote_ref(top, ref):
    """True when ref names a configured remote's branch (e.g. origin/main), which only a fetch from that remote moves."""
    return bool(ref) and "/" in ref and ref.split("/", 1)[0] in git_in(top, "remote").split()


def policy_source_problem(e):
    """Why an enrollment's policy source can't be trusted (None when it can). Fails closed for local refs, including
    enrollments made before this check, unless the human allowed a local policy explicitly."""
    if not e or is_remote_ref(e["toplevel"], e.get("policy_ref")) or e.get("allow_local_policy"):
        return None
    return ("policy comes from %s, which is not a remote's branch: %s. Re-enroll with a remote branch "
            "(gate.py enroll --policy-ref origin/main), or, for a repository with no remote, "
            "gate.py enroll --allow-local-policy" % (e.get("policy_ref"), LOCAL_POLICY_RISK))


def enroll(cwd, policy_ref=None, allow_local=False):
    """Persist repository enrollment with a remote policy ref unless local policy is explicitly allowed."""
    top, ident = repo_identity(cwd)
    if not ident:
        raise TrustError("not inside a git repository")
    ref = policy_ref or default_policy_ref(top)
    if not ref:
        raise TrustError("can't find the default branch (no origin/HEAD, main or master). Pass --policy-ref <branch>")
    if not is_remote_ref(top, ref) and not allow_local:
        raise TrustError("policy would come from %s, which is not a remote's branch: %s. Use --policy-ref <remote>/<branch>, "
                         "or, only if this repository has no remote, --allow-local-policy" % (ref, LOCAL_POLICY_RISK))
    data = read_json(enrolled_path())
    data[ident] = {"enrolled": True, "policy_ref": ref, "origin": git_in(top, "remote", "get-url", "origin"),
                   "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if not is_remote_ref(top, ref):
        data[ident]["allow_local_policy"] = True  # the human chose this; doctor keeps warning about it
    write_json_atomic(enrolled_path(), data)
    return data[ident]


def record_policy_seen(cwd, sha, config):
    """Remember the policy this computer last accepted, so doctor can say when the default branch makes it weaker."""
    _, ident = repo_identity(cwd)
    data = read_json(enrolled_path())
    if ident in data:
        data[ident]["policy_seen"] = {"sha": sha, "config": config, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        write_json_atomic(enrolled_path(), data)


def unenroll(cwd):
    """Remove repository enrollment and return whether a record was deleted."""
    _, ident = repo_identity(cwd)
    data = read_json(enrolled_path())
    if ident in data:
        del data[ident]
        write_json_atomic(enrolled_path(), data)
        return True
    return False


_POLICY_SHA = {}  # (top, ref) -> (sha, when): one hook call reads every policy file from the same commit


def policy_commit(top, ref):
    """The commit the policy ref points at right now. "origin/main" means the remote-tracking branch only: a local
    branch or tag with the same name (which git would otherwise prefer) can't stand in for it."""
    if not ref:
        return None
    hit = _POLICY_SHA.get((str(top), ref))
    if hit and time.time() - hit[1] < 5:  # a hook runs well under a second; long-running commands re-resolve
        return hit[0]
    sha = _resolve_policy_commit(top, ref)
    _POLICY_SHA[(str(top), ref)] = (sha, time.time())
    return sha


def _resolve_policy_commit(top, ref):
    if "/" in ref and ref.split("/", 1)[0] in git_in(top, "remote").split():
        candidates = ("refs/remotes/" + ref,)  # a remote's branch: if the tracking ref is gone, fail closed
    else:
        candidates = ("refs/heads/" + ref, "refs/tags/" + ref)  # no remote (e.g. "main" in a local-only repo)
    for full in candidates:
        try:
            found = subprocess.run(["git", "show-ref", "--verify", "-q", full], cwd=top, capture_output=True,
                                   timeout=GIT_TIMEOUT).returncode == 0
        except subprocess.TimeoutExpired:
            return None
        if found:
            return git_in(top, "rev-parse", "--verify", "-q", full + "^{commit}") or None
    return None


def policy_text(top, ref, name):
    """Return .story-gate/<name> from ref's resolved policy commit in repository top.

    Decode UTF-8 with an optional BOM, replacing invalid bytes. Return None when
    the ref cannot be resolved, the file is absent, or Git fails or times out.
    OSError from starting Git propagates.
    """
    sha = policy_commit(top, ref)
    if not sha:
        return None
    try:
        r = subprocess.run(["git", "show", "%s:.story-gate/%s" % (sha, name)], cwd=top, capture_output=True, timeout=GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None  # cfg() then fails closed
    return r.stdout.decode("utf-8-sig", "replace") if r.returncode == 0 else None  # a BOM (Notepad) is not an error


def tighten(policy, local):
    """Working-tree settings may only make the policy stricter."""
    out = json.loads(json.dumps(policy))
    if not isinstance(local, dict):
        return out
    if local.get("mode") == "enforce":
        out["mode"] = "enforce"
    out["enforce_points"] = sorted(set(out.get("enforce_points") or []) | set(x for x in (local.get("enforce_points") or []) if isinstance(x, str)))
    out["accept_concerns"] = bool(out.get("accept_concerns")) and bool(local.get("accept_concerns", out.get("accept_concerns")))
    th, lt = dict(out.get("thresholds") or {}), local.get("thresholds") or {}
    for k in ("pass", "concerns"):
        try:
            if k in lt:
                th[k] = max(float(th.get(k, 0)), float(lt[k]))
        except (TypeError, ValueError):
            pass
    out["thresholds"] = th
    j, lj = dict(out.get("judge") or {}), local.get("judge") or {}
    for k in ("emulated_allow_pass", "allow_self_judge_pass"):
        if k in lj:
            j[k] = bool(j.get(k)) and bool(lj[k])
    out["judge"] = j
    if local.get("require_independent_review"):
        out["require_independent_review"] = True
    w, lw = dict(out.get("writing") or {}), local.get("writing") or {}
    if isinstance(lw, dict):
        if lw.get("enforce") is True:
            w["enforce"] = True
        try:
            if "target" in lw:
                w["target"] = max(float(w.get("target", 0)), float(lw["target"]))
        except (TypeError, ValueError):
            pass
        try:
            if "diagram_min_files" in lw and int(lw["diagram_min_files"]) >= 1:
                w["diagram_min_files"] = min(int(w.get("diagram_min_files", 5)), int(lw["diagram_min_files"]))
        except (TypeError, ValueError):
            pass
    if w:
        out["writing"] = w
    lv = local.get("validation") or {}
    if isinstance(lv, dict) and lv.get("required") is True:
        out["validation"] = dict(out.get("validation") or {}, required=True)
    return out


def weaker(old, new):
    """Plain-English list of ways `new` policy is weaker than `old` (both full configs). Empty when it isn't."""
    out = []
    o, n = old or {}, new or {}
    if o.get("mode") == "enforce" and n.get("mode") != "enforce":
        out.append("mode went from enforce to %s" % n.get("mode"))
    gone = sorted(set(o.get("enforce_points") or []) - set(n.get("enforce_points") or []))
    if gone:
        out.append("no longer enforced at: %s" % ", ".join(gone))
    if not o.get("accept_concerns") and n.get("accept_concerns"):
        out.append("CONCERNS verdicts now pass")
    for k in ("pass", "concerns"):
        try:
            a, b = float((o.get("thresholds") or {}).get(k)), float((n.get("thresholds") or {}).get(k))
            if b < a:
                out.append("%s threshold lowered from %s to %s" % (k, a, b))
        except (TypeError, ValueError):
            pass
    for k in ("emulated_allow_pass", "allow_self_judge_pass"):
        if not (o.get("judge") or {}).get(k) and (n.get("judge") or {}).get(k):
            out.append("judge setting %s turned on" % k)
    if o.get("require_independent_review") and not n.get("require_independent_review"):
        out.append("independent review no longer required")
    ow, nw = o.get("writing") or {}, n.get("writing") or {}
    if ow.get("enforce") and not nw.get("enforce"):
        out.append("plain-writing check no longer enforced")
    try:
        if ow.get("enforce") and float(nw.get("target", 0)) < float(ow.get("target", 0)):
            out.append("plain-writing target lowered from %s to %s" % (ow.get("target"), nw.get("target")))
    except (TypeError, ValueError):
        pass
    try:
        if ow.get("enforce") and int(nw.get("diagram_min_files", 5)) > int(ow.get("diagram_min_files", 5)):
            out.append("diagram minimum raised from %s to %s files" % (ow.get("diagram_min_files", 5), nw.get("diagram_min_files", 5)))
    except (TypeError, ValueError):
        pass
    if (o.get("validation") or {}).get("required", True) and not (n.get("validation") or {}).get("required", True):
        out.append("validation (validation.md and scenario runs for every acceptance criterion) no longer required")
    more = sorted(set(n.get("exempt_globs") or []) - set(o.get("exempt_globs") or []))
    if more:
        out.append("more files exempt from the gate: %s" % ", ".join(more[:5]))
    removed = sorted(set(o.get("test_globs") or []) - set(n.get("test_globs") or []))
    if removed:  # treat replacements conservatively, even when patterns overlap
        out.append("test file patterns removed: %s" % ", ".join(removed[:5]))
    if (o.get("test_command") or "") != (n.get("test_command") or ""):  # can't tell if a new command is as strict: a human decides
        out.append("test command changed from %r to %r" % (o.get("test_command") or "", n.get("test_command") or ""))
    who = sorted(set(n.get("approvers") or []) - set(o.get("approvers") or []))
    if who:
        out.append("more people can accept work: %s" % ", ".join(who[:5]))
    norm = lambda xs: {str(x).lower().replace("[bot]", "") for x in (xs or [])}  # same normalisation as reviewed_by
    revs = sorted(norm(n.get("reviewers")) - norm(o.get("reviewers")))
    if revs:
        out.append("more reviewers count as independent review: %s" % ", ".join(revs[:5]))
    key = lambda e: json.dumps(e, sort_keys=True)
    added = sorted(set(map(key, n.get("project_hooks_allowed") or [])) - set(map(key, o.get("project_hooks_allowed") or [])))
    if added:
        out.append("project hooks newly allowed: %s" % "; ".join(added[:5]))
    return out


# ------------------------------------------------------------------ user-level client hooks
def user_hook_files():
    """Return the user-level hook configuration path for each supported client."""
    h = user_home()
    return {"claude": h / ".claude" / "settings.json", "codex": h / ".codex" / "hooks.json",
            "cursor": h / ".cursor" / "hooks.json", "gemini": h / ".gemini" / "settings.json",
            "windsurf": h / ".codeium" / "windsurf" / "hooks.json",
            "vscode": h / ".copilot" / "hooks" / "story-gate.json",  # VS Code agent hooks, user level (a file of our own)
            "hermes": hermes_home() / "config.yaml"}  # Hermes reads shell hooks only from the `hooks:` block of config.yaml


def hermes_extra_files():
    """Hermes files story-gate writes besides config.yaml: the skill, and the approval for our own hook commands."""
    return {"skill": hermes_home() / "skills" / "story-gate" / "SKILL.md",
            "allowlist": hermes_home() / "shell-hooks-allowlist.json"}


def user_protected_paths():
    """Places an agent must never write: the runtime, enrollment, agent key, and the user-level hook files."""
    return [G.config_dir()] + list(user_hook_files().values()) + list(hermes_extra_files().values())


def hook_entries(py, gate):
    """Build client hook registrations that invoke the isolated trusted launcher."""
    cmd = lambda cl, ev: '"%s" -I "%s" hook --client %s --event %s' % (py, gate, cl, ev)
    return {
        "claude": {"hooks": {
            "PreToolUse": [{"matcher": "Edit|Write|MultiEdit|NotebookEdit|Bash", "hooks": [{"type": "command", "command": cmd("claude", "pre"), "timeout": 15}]}],
            "PostToolUse": [{"matcher": "Edit|Write|MultiEdit|NotebookEdit", "hooks": [{"type": "command", "command": cmd("claude", "post"), "timeout": 180}]}],
            "Stop": [{"hooks": [{"type": "command", "command": cmd("claude", "stop"), "timeout": 60}]}],
            "SessionStart": [{"hooks": [{"type": "command", "command": cmd("claude", "session"), "timeout": 15}]}]}},
        "codex": {"hooks": {
            "PreToolUse": [{"matcher": "^(apply_patch|Edit|Write|Bash|shell|local_shell|exec_command)$", "hooks": [{"type": "command", "command": cmd("codex", "pre"), "timeout": 15}]}],
            "PostToolUse": [{"matcher": "^(apply_patch|Edit|Write)$", "hooks": [{"type": "command", "command": cmd("codex", "post"), "timeout": 180}]}],
            "Stop": [{"hooks": [{"type": "command", "command": cmd("codex", "stop"), "timeout": 60}]}],
            "SessionStart": [{"hooks": [{"type": "command", "command": cmd("codex", "session"), "timeout": 15}]}]}},
        "cursor": {"version": 1, "hooks": {  # failClosed: a crash or timeout blocks instead of letting the edit through
            "preToolUse": [{"command": cmd("cursor", "pre"), "matcher": "Write", "failClosed": True}],
            "beforeShellExecution": [{"command": cmd("cursor", "pre"), "failClosed": True}],
            "postToolUse": [{"command": cmd("cursor", "post"), "matcher": "Write"}],
            "stop": [{"command": cmd("cursor", "stop")}],
            "sessionStart": [{"command": cmd("cursor", "session")}]}},
        "gemini": {"hooks": {
            "BeforeTool": [{"matcher": "write_file|replace|run_shell_command", "hooks": [{"type": "command", "command": cmd("gemini", "pre"), "timeout": 15000}]}],
            "AfterTool": [{"matcher": "write_file|replace", "hooks": [{"type": "command", "command": cmd("gemini", "post"), "timeout": 180000}]}],
            "AfterAgent": [{"matcher": "*", "hooks": [{"type": "command", "command": cmd("gemini", "stop"), "timeout": 60000}]}],
            "SessionStart": [{"matcher": "*", "hooks": [{"type": "command", "command": cmd("gemini", "session"), "timeout": 15000}]}]}},
        "windsurf": {"hooks": {
            "pre_write_code": [{"command": cmd("windsurf", "pre"), "show_output": True}],
            "pre_run_command": [{"command": cmd("windsurf", "pre"), "show_output": True}],
            "post_cascade_response": [{"command": cmd("windsurf", "stop"), "show_output": True}]}},
        # VS Code ignores matchers (every tool call runs the hook), so gate.py skips tools that neither edit nor run commands.
        # Source: https://code.visualstudio.com/docs/agents/reference/hooks-reference ; tool names seen live: Read, Write, Edit,
        # Bash, Glob, AskUserQuestion (VS Code's Copilot agent, October 2026).
        "vscode": {"hooks": {
            "PreToolUse": [{"type": "command", "command": cmd("vscode", "pre"), "timeout": 15}],
            "PostToolUse": [{"type": "command", "command": cmd("vscode", "post"), "timeout": 180}],
            "Stop": [{"type": "command", "command": cmd("vscode", "stop"), "timeout": 60}],
            "SessionStart": [{"type": "command", "command": cmd("vscode", "session"), "timeout": 15}]}},
        # Hermes: pre_verify is its Stop (once per turn after edits). fail_closed: a crash or timeout blocks the edit.
        # Source: https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks
        "hermes": {"hooks": {
            "pre_tool_call": [{"matcher": "^(write_file|patch|terminal|execute_code)$", "command": cmd("hermes", "pre"), "timeout": 15,
                               "fail_closed": True}],
            "post_tool_call": [{"matcher": "^(write_file|patch)$", "command": cmd("hermes", "post"), "timeout": 180}],
            "pre_verify": [{"command": cmd("hermes", "stop"), "timeout": 60}]}},
    }


HERMES_BEGIN = "# >>> story-gate: managed block (story-gate uninstall --user removes it) >>>"
HERMES_END = "# <<< story-gate <<<"


def _yaml_str(v):
    return "'%s'" % str(v).replace("'", "''") if isinstance(v, str) else ("true" if v is True else "false" if v is False else str(v))


def hermes_block(entry, nl="\n"):
    """Our `hooks:` block for Hermes's config.yaml, as plain YAML text between markers (no YAML library needed)."""
    lines = [HERMES_BEGIN, "hooks:"]
    for ev, items in entry["hooks"].items():
        lines.append("  %s:" % ev)
        for it in items:
            keys = ["matcher", "command", "timeout", "fail_closed"]
            first = True
            for k in keys:
                if k in it:
                    lines.append("%s%s: %s" % ("    - " if first else "      ", k, _yaml_str(it[k])))
                    first = False
    return nl.join(lines + [HERMES_END]) + nl


def _strip_block(text):
    """Text without our managed block (and the blank line we added before it)."""
    out, skip = [], False
    for line in text.splitlines(True):
        if line.rstrip("\r\n") == HERMES_BEGIN:
            skip = True
            if out and not out[-1].strip():
                out.pop()
            continue
        if skip:
            if line.rstrip("\r\n") == HERMES_END:
                skip = False
            continue
        out.append(line)
    if skip:
        raise TrustError("the story-gate block in this file has no end marker; fix it by hand")
    return "".join(out)


def merged_hermes_yaml(text, entry):
    """config.yaml with our block replaced or appended. Refuses (TrustError) when the file already has its own top-level
    `hooks:` key: merging into someone's YAML without a YAML parser could break their config, so that is done by hand."""
    rest = _strip_block(text)
    # Any spelling of a top-level `hooks` key (plain, quoted, or YAML's explicit `? hooks`): a second one would replace it.
    if re.search(r"(?m)^(?:\?[ \t]+)?([\"']?)hooks\1[ \t]*(?::|$)", rest):
        raise TrustError("it already has a `hooks:` section; add the story-gate entries to it by hand")
    # A block appended after a document marker would land in a second YAML document, which Hermes never reads.
    content = [l for l in rest.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    if content and content[0].lstrip().startswith(("{", "[")):
        raise TrustError("it's written as one { } block; add the story-gate entries to it by hand")
    if any(re.match(r"\.\.\.\s*(#.*)?$", l) for l in content) or any(re.match(r"---(\s|$)", l) for l in content[1:]):
        raise TrustError("it uses YAML document markers (--- or ...); add the story-gate entries to it by hand")
    if content and re.match(r"[^#\s][^:]*:\s*[\[{][^\]}]*$", content[-1]):
        raise TrustError("it ends inside an unfinished [ ] or { } list; add the story-gate entries to it by hand")
    nl = "\r\n" if "\r\n" in rest else "\n"  # keep the file's own line endings
    if rest and not rest.endswith("\n"):
        rest += nl
    return rest + (nl if rest.strip() else "") + hermes_block(entry, nl)


def hermes_allowlist(text, entry, remove=False):
    """Hermes asks once per (event, command) before running a new shell hook, and skips the hook when nobody can answer
    (its desktop app, the gateway). The person running setup approves exactly story-gate's own commands here."""
    data = json.loads(text) if text.strip() else {}
    if not isinstance(data, dict):
        raise TrustError("expected a JSON object")
    ours_ = lambda e: isinstance(e, dict) and bool(HOOK_SIGNATURE.search(str(e.get("command", ""))))
    keep = [e for e in data.get("approvals", []) if not ours_(e)]
    if not remove:
        now_ = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        keep += [{"event": ev, "command": it["command"], "approved_at": now_, "approved_by": "story-gate install --user"}
                 for ev, items in entry["hooks"].items() for it in items]
    data["approvals"] = keep
    return json.dumps(data, indent=2) + "\n"


def ours(entry):
    return bool(HOOK_SIGNATURE.search(json.dumps(entry).replace('\\"', '"')))


def merged_hook_json(text, new):
    """Return the new JSON text for a hooks/settings file: our entries replaced, everything else untouched."""
    data = json.loads(text) if text.strip() else {}
    if not isinstance(data, dict):
        raise TrustError("expected a JSON object")
    for k, v in new.items():
        if k != "hooks":
            data.setdefault(k, v)
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise TrustError('"hooks" is not an object')
    for ev, entries in new.get("hooks", {}).items():
        cur = hooks.setdefault(ev, [])
        cur[:] = [e for e in cur if not ours(e)] + entries
    return json.dumps(data, indent=2) + "\n"


def removed_hook_json(text):
    data = json.loads(text) if text.strip() else {}
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if isinstance(hooks, dict):
        for ev in list(hooks):
            if isinstance(hooks[ev], list):
                hooks[ev] = [e for e in hooks[ev] if not ours(e)]
                if not hooks[ev]:
                    del hooks[ev]
    return json.dumps(data, indent=2) + "\n"


def apply_file(path, new_text, dry_run, out, check_json=True):
    """Write with a backup and an install-manifest entry; print a diff; never clobber a file we couldn't parse.
    Writes through symlinks (dotfile managers keep their link)."""
    path = Path(path)
    real = Path(os.path.realpath(path))
    existed = real.exists()
    old = real.read_text(encoding="utf-8") if existed else ""
    if old == new_text:
        out.append("  %s: already up to date" % path)
        return False
    if check_json:
        json.loads(new_text)  # round-trip check before anything touches disk
    out.extend("    " + l.rstrip("\n") for l in difflib.unified_diff(old.splitlines(True), new_text.splitlines(True),
                                                                      str(path), str(path) + " (new)", n=1))
    if dry_run:
        return True
    real.parent.mkdir(parents=True, exist_ok=True)
    prev = read_json(manifest_path()).get("files", {}).get(str(path)) or {}
    edited = bool(prev.get("user_edited") or (prev and existed and sha256(real) != prev.get("sha_after")))  # you changed it since
    backup = None
    if existed:
        stamp = time.strftime("%Y%m%d%H%M%S") + "-%06d" % (int(time.time() * 1e6) % 1000000)
        backup, n = real.with_name("%s.story-gate-backup-%s" % (real.name, stamp)), 0
        while backup.exists():  # never overwrite an earlier backup
            n += 1
            backup = real.with_name("%s.story-gate-backup-%s-%d" % (real.name, stamp, n))
        shutil.copy2(real, backup)
    tmp = real.with_name(real.name + ".tmp-%d" % os.getpid())
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, real)
    record("files", str(path), existed_before=existed, real_before=str(real), backup_before=str(backup) if backup else None,
           sha_after=sha256(real), user_edited=edited)
    return True


def restore_file(path, out, dry_run=False):
    """Put a file back exactly as it was before story-gate first touched it, if nobody has changed it since.
    If it was changed since (or the backup is gone), only story-gate's own entries are removed and your edits stay."""
    path = Path(path)
    e = read_json(manifest_path()).get("files", {}).get(str(path))
    if not e:
        return False
    real = Path(os.path.realpath(path))
    if e.get("real_before") and e["real_before"] != str(real):  # the link now points somewhere else: don't write into it
        out.append("  %s: now points to %s (it pointed to %s at install); not restored - remove the story-gate lines by hand"
                   % (path, real, e["real_before"]))
        return True
    if not e.get("real_before") and path.is_symlink():  # older install: link target not recorded, so fail closed
        out.append("  %s: is a link and this install didn't record its target; not restored - remove the story-gate lines by hand"
                   % path)
        return True
    backup = Path(e["backup_before"]) if e.get("backup_before") else None
    done = True
    if not real.exists():
        out.append("  %s: already gone" % path)
    elif sha256(real) == e.get("sha_after") and not e.get("user_edited") and (not e.get("existed_before") or (backup and backup.is_file())):
        if dry_run:
            out.append("  %s: would be restored exactly as before" % path)
            return True
        if e.get("existed_before"):
            shutil.copy2(backup, real)
            out.append("  %s: restored byte for byte from %s" % (path, backup))
        else:
            real.unlink()
            out.append("  %s: removed (story-gate created it)" % path)
    else:
        try:
            apply_file(path, removed_hook_json(real.read_text(encoding="utf-8")), dry_run, out)
            out.append("  %s: changed since install (or no backup), so only story-gate's entries were removed" % path)
        except (ValueError, TrustError) as ex:
            done = False
            out.append("  %s: NOT changed (%s) - remove the story-gate lines by hand" % (path, ex))
    if done and not dry_run:
        forget("files", str(path))
    return True


def register_user_hooks(py, gate, clients=USER_CLIENTS, dry_run=False, skill_src=None):
    out, files, entries = [], user_hook_files(), hook_entries(py, gate)
    for cl in clients:
        p = files[cl]
        try:
            text = p.read_text(encoding="utf-8") if p.exists() else ""
            if cl == "hermes":
                register_hermes(p, text, entries[cl], dry_run, out, skill_src)
            else:
                apply_file(p, merged_hook_json(text, entries[cl]), dry_run, out)
        except (ValueError, TrustError) as e:
            out.append("  %s: NOT changed (%s). Add the story-gate hooks by hand (docs/client-security.md)." % (p, e))
    return out


def register_hermes(p, text, entry, dry_run, out, skill_src):
    new = merged_hermes_yaml(text, entry)  # raises before anything is written when the file isn't ours to change
    ex = hermes_extra_files()
    allow = ex["allowlist"].read_text(encoding="utf-8-sig") if ex["allowlist"].exists() else ""
    new_allow = hermes_allowlist(allow, entry)  # also before any write: a broken approvals file leaves config.yaml alone
    apply_file(p, new, dry_run, out, check_json=False)
    apply_file(ex["allowlist"], new_allow, dry_run, out)
    out.append("  %s: approved story-gate's own Hermes hooks (you ran this setup; nothing else was approved)" % ex["allowlist"])
    others = hermes_profiles()
    if others:
        out.append("  Hermes profiles %s have their own settings and are NOT protected yet: run install --user with "
                   "HERMES_HOME set to each profile's folder (%s)" % (", ".join(others), hermes_home() / "profiles" / "<name>"))
    if skill_src and Path(skill_src).is_file():
        apply_file(ex["skill"], Path(skill_src).read_text(encoding="utf-8"), dry_run, out, check_json=False)


def unregister_user_hooks(dry_run=False):
    out = []
    unregister_hermes(dry_run, out)
    for cl, p in user_hook_files().items():
        if cl == "hermes":
            continue
        if restore_file(p, out, dry_run):
            continue
        if p.exists():
            try:
                apply_file(p, removed_hook_json(p.read_text(encoding="utf-8")), dry_run, out)
            except (ValueError, TrustError) as e:
                out.append("  %s: NOT changed (%s)" % (p, e))
    return out


def hermes_profiles():
    """Other Hermes profiles (each has its own config.yaml): story-gate protects only the main one, so it says so."""
    d = hermes_home() / "profiles"
    try:
        return sorted(p.name for p in d.iterdir() if p.is_dir() and (p / "config.yaml").is_file())
    except OSError:
        return []


def _hermes_commands(text):
    """(event, command) pairs inside our managed block, read back from the YAML we wrote ourselves."""
    pairs, ev, inside = [], None, False
    for line in text.splitlines():
        if line.rstrip() == HERMES_BEGIN:
            inside = True
            continue
        if line.rstrip() == HERMES_END:
            break
        m = re.match(r"  ([a-z_]+):\s*$", line)
        if inside and m:
            ev = m.group(1)
        m = re.match(r"\s+(?:- )?command: '(.*)'\s*$", line)
        if inside and m and ev:
            pairs.append((ev, m.group(1).replace("''", "'")))
    return pairs


def hermes_problems():
    """Problems that would leave Hermes's live checks silently off. Hermes skips a hook command it hasn't approved when
    nobody can answer its prompt, so each command in our block must have a matching approval (same event, same text)."""
    p, ex = user_hook_files()["hermes"], hermes_extra_files()
    if not p.is_file() or HERMES_BEGIN not in p.read_text(encoding="utf-8-sig", errors="ignore"):
        return []
    pairs = _hermes_commands(p.read_text(encoding="utf-8-sig", errors="ignore"))
    try:
        data = json.loads(ex["allowlist"].read_text(encoding="utf-8-sig"))
        approved = {(e.get("event"), e.get("command")) for e in data.get("approvals", []) if isinstance(e, dict)}
    except (OSError, AttributeError, ValueError):
        approved = set()
    out = []
    if not pairs:
        out.append("the story-gate block in %s is damaged; run install --user again" % p)
    missing = [ev for ev, cmd in pairs if (ev, cmd) not in approved]
    if missing:
        out.append("Hermes hasn't approved story-gate's hook for %s, so Hermes may skip it; run install --user again"
                   % ", ".join(missing))
    return out


def unregister_hermes(dry_run, out):
    """Hermes's config.yaml and allowlist are shared with Hermes itself, so only our block and our approvals come out;
    the skill folder story-gate created is put back as it was."""
    p, ex = user_hook_files()["hermes"], hermes_extra_files()
    for f, fix in ((p, _strip_block), (ex["allowlist"], lambda t: hermes_allowlist(t, {"hooks": {}}, remove=True))):
        if not f.exists():
            continue
        try:
            text = f.read_text(encoding="utf-8-sig")
            new = fix(text)
            created = not (read_json(manifest_path()).get("files", {}).get(str(f)) or {"existed_before": True}).get("existed_before")
            empty = (not new.strip() or json.loads(new) == {"approvals": []}) if f.suffix == ".json" else not new.strip()
            if created and empty:
                if not dry_run:
                    f.unlink()
                out.append("  %s: removed (story-gate created it)" % f)
            elif new != text:
                apply_file(f, new, dry_run, out, check_json=f.suffix == ".json")
        except (ValueError, TrustError) as e:
            out.append("  %s: NOT changed (%s) - remove the story-gate lines by hand" % (f, e))
        if not dry_run:
            forget("files", str(f))
    restore_file(ex["skill"], out, dry_run)


def registered_clients(gate):
    """Clients whose user-level hook file points at this runtime."""
    g = str(gate).replace("\\", "/")
    return [cl for cl, p in user_hook_files().items() if p.exists() and g in p.read_text(encoding="utf-8", errors="ignore").replace("\\\\", "/")]


LOCAL_ONLY = (".claude/settings.local.json",)  # your own per-computer file; it only counts when a branch commits it


def _branch_hook_files(top, hook_files):
    tracked = set(git_in(top, "ls-files", "--", *LOCAL_ONLY).splitlines())
    return [rel for rel in hook_files if rel not in LOCAL_ONLY or rel in tracked]


def _hook_commands(p):
    """Command strings under the `hooks` section of a hook file (JSON, or TOML where Python has tomllib).
    None when the file can't be parsed, so the caller can fail closed."""
    text = p.read_text(encoding="utf-8", errors="ignore")
    try:
        if p.suffix == ".toml":
            import tomllib
            data = tomllib.loads(text)
        else:
            data = json.loads(text) if text.strip() else {}
    except Exception:  # unparseable, or no tomllib (Python < 3.11)
        return None
    out = []

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k == "command" and isinstance(v, (str, list)):
                    out.append(v if isinstance(v, str) else " ".join(map(str, v)))
                else:
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(data.get("hooks") if isinstance(data, dict) else None)
    return out


def _runs_repo_gate(p):
    cmds = _hook_commands(p)
    if cmds is None:  # can't read it with certainty: treat as running repository code
        return ".story-gate/gate.py" in p.read_text(encoding="utf-8", errors="ignore").replace("\\", "/")
    return any(".story-gate/gate.py" in c.replace("\\", "/") for c in cmds)


def project_hook_findings(top, hook_files):
    """Project-level hook files whose hook commands still run story-gate code from the repository (a branch could swap it).
    Only commands under `hooks` count, so a permission rule such as Bash(python3 .story-gate/gate.py:*) is not a finding."""
    found = [rel for rel in _branch_hook_files(top, hook_files) if (Path(top) / rel).is_file() and _runs_repo_gate(Path(top) / rel)]
    hd = Path(top) / ".grok" / "hooks"
    if hd.is_dir():
        found += [str(f.relative_to(top)).replace("\\", "/") for f in hd.glob("*.json") if _runs_repo_gate(f)]
    return sorted(set(found))


def other_project_hooks(top, hook_files):
    """Commands from the repository's own project hook files (not story-gate's). story-gate can't vouch for them."""
    cmds = []
    paths = [Path(top) / r for r in _branch_hook_files(top, hook_files)] + (sorted((Path(top) / ".grok" / "hooks").glob("*.json")) if (Path(top) / ".grok" / "hooks").is_dir() else [])
    for p in paths:
        if not p.is_file() or p.suffix != ".json":
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        def walk(x):
            if isinstance(x, dict):
                if isinstance(x.get("command"), str) and not HOOK_SIGNATURE.search(x["command"]):
                    cmds.append((p.relative_to(top).as_posix(), x["command"]))
                for v in x.values():
                    walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)
        walk(data.get("hooks") if isinstance(data, dict) else None)
    return cmds


def remove_project_hooks(top, hook_files, dry_run=False):
    out = []
    for rel in project_hook_findings(top, hook_files):
        p = Path(top) / rel
        try:
            apply_file(p, removed_hook_json(p.read_text(encoding="utf-8")), dry_run, out)
        except (ValueError, TrustError) as e:
            out.append("  %s: NOT changed (%s) - remove the story-gate lines by hand" % (rel, e))
    return out
