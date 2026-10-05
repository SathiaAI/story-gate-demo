"""story-gate init: guided setup in the browser.

The AI (or the person) runs `story-gate init` inside the project folder. A page opens on this computer
(127.0.0.1 only) and walks the person through the only steps that must be theirs:

  1. sign in to GitHub            (GitHub's device sign-in, or an existing `gh` login)
  2. create the AI's own login    (GitHub App manifest: one "Create" click, then "Install")
  3. add the judge key            (typed into the page, encrypted straight into a GitHub secret)
  4. merge the setup pull request (opened for them; branch rules are applied after the merge)

Everything else (files, workflows, CODEOWNERS, branch rules, hooks on this computer) is done for them.
Stdlib only. Nothing here runs from a repository branch: it runs from the installed package or a repo copy
of story-gate that the person chose to run.
"""
import html, http.server, json, os, secrets, shutil, subprocess, sys, tempfile, threading, time, urllib.parse, webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sg_github as G  # noqa: E402

# story-gate's own OAuth app (SathiaAI, device flow only; a client ID is public, there is no secret). Empty: fall back to `gh`.
OAUTH_CLIENT_ID = os.environ.get("STORY_GATE_OAUTH_CLIENT_ID", "Ov23liAhA19MDXrS9a4S")
STEPS = ("signin", "agent", "key", "merge", "done")
# AI tools story-gate can check live on this computer (user-level hooks), in the order the page lists them.
HOOKED_TOOLS = (("claude", "Claude Code"), ("codex", "Codex"), ("cursor", "Cursor"), ("vscode", "VS Code (Copilot agent)"),
                ("hermes", "Hermes"), ("gemini", "Gemini CLI"), ("windsurf", "Windsurf"))
# Tools with no verified live checks yet: they follow the rules in AGENTS.md / the skill, and GitHub checks their pull requests.
OTHER_TOOLS = (("Antigravity", "rules + skill; live checks once we've verified its hooks"), ("pi", "rules + skill"),
               ("Roo Code", "rules + skill"), ("ChatGPT", "GitHub checks its pull requests"),
               ("Google AI Studio", "GitHub checks its pull requests"))
GH_USER = __import__("re").compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")


def git(cwd, *a):
    r = subprocess.run(["git", *a], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    return r.stdout.strip() if r.returncode == 0 else ""


def github_repo(top):
    """owner/name from the origin remote, or None when origin isn't on github.com."""
    url = git(top, "config", "--get", "remote.origin.url")  # as written, before any url.insteadOf rewriting
    for pre in ("https://github.com/", "git@github.com:", "ssh://git@github.com/"):
        if url.startswith(pre):
            name = url[len(pre):].rstrip("/")
            return name[:-4] if name.endswith(".git") else name
    return None


def has_agent():
    try:
        G.agent_record()
        return True
    except Exception:
        return False


def setup_files(top, base, py):
    """The files the setup pull request adds: story-gate installed into a clean copy of the default branch."""
    tmp = Path(tempfile.mkdtemp(prefix="sg-setup-"))
    wt = tmp / "wt"
    try:
        if subprocess.run(["git", "worktree", "add", "--detach", str(wt), "origin/" + base], cwd=str(top), capture_output=True).returncode:
            raise RuntimeError("could not read origin/%s - run `git fetch` and try again" % base)
        dest = wt / ".story-gate"
        dest.mkdir(exist_ok=True)
        import sg_trust as T
        for f in T.runtime_files(HERE):  # the same files the runtime is made of, plus the repo's own settings
            (dest / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(HERE / f, dest / f)
        env = dict(os.environ, STORY_GATE_ROOT=str(wt))
        r = subprocess.run([sys.executable, str(dest / "gate.py"), "install", "--python", py], cwd=str(wt), env=env,
                           capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError("repository setup failed: %s" % (r.stderr or r.stdout)[-400:])
        out = {}
        for line in git(wt, "status", "--porcelain", "--untracked-files=all").splitlines():
            p = line[3:].strip().strip('"')
            if p and (wt / p).is_file() and "__pycache__" not in p:
                out[p] = (wt / p).read_bytes()
        return out
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=str(top), capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)


class Wizard:
    """State machine behind the page. Each step records ok / error / waiting; the page polls /state."""

    def __init__(self, top, repo, py, token=None, open_browser=True):
        self.top, self.repo, self.py, self.open_browser = Path(top), repo, py, open_browser
        self.secret = secrets.token_urlsafe(24)
        self.agent_state = secrets.token_urlsafe(16)
        self.token = token
        self.login = None
        self.st = {s: {"status": "todo", "msg": ""} for s in STEPS}
        self.device = None
        self.pr = None
        self.private_free = False
        self.private = False
        import sg_trust as T
        self.found = T.detected_clients()
        self.clients = [cl for cl, _ in HOOKED_TOOLS if cl in self.found] or ["claude", "codex", "cursor"]
        self.approvers = []  # other people who accept work (CODEOWNERS), besides the person signing in
        self.already = False  # the repository already has story-gate on GitHub (a teammate's computer)
        self.base = "main"
        self.lock = threading.Lock()

    # ---- state helpers
    def set(self, step, status, msg=""):
        with self.lock:
            self.st[step] = {"status": status, "msg": msg}

    def snapshot(self):
        with self.lock:
            return {"steps": json.loads(json.dumps(self.st)), "login": self.login, "repo": self.repo, "device": self.device,
                    "pr": self.pr, "private_free": self.private_free, "clients": list(self.clients),
                    "approvers": list(self.approvers)}

    def set_clients(self, text):
        """The AI tools to protect on this computer, as ticked on the page (only tools story-gate can check live)."""
        want = [x for x in (text or "").split(",") if x]
        known = [cl for cl, _ in HOOKED_TOOLS]
        if not want or any(x not in known for x in want):
            return False
        with self.lock:
            self.clients = [cl for cl in known if cl in want]
        return True

    def background(self, fn):
        threading.Thread(target=self._guard, args=(fn,), daemon=True).start()

    def _guard(self, fn):
        try:
            fn()
        except Exception as e:  # every failure becomes a plain message on the page
            step = next((s for s in STEPS if self.st[s]["status"] == "working"), "signin")
            self.set(step, "error", str(e)[:400])

    # ---- step 1: sign in
    def start_signin(self):
        tok = self.token or G.human_token()
        if tok and G.whoami(tok):
            return self._signed_in(tok)
        if not OAUTH_CLIENT_ID:
            self.set("signin", "error", "Sign in with the GitHub CLI first: run `gh auth login` in a terminal, then press Start again.")
            return
        self.set("signin", "working", "Waiting for you to enter the code on GitHub")
        self.background(lambda: self._signed_in(G.device_flow(OAUTH_CLIENT_ID, on_code=self._show_code)))

    def _show_code(self, code, uri):
        with self.lock:
            self.device = {"code": code, "uri": uri}
        if self.open_browser:
            webbrowser.open(uri)

    def _signed_in(self, tok):
        self.token = tok
        self.login = G.whoami(tok)
        st, info, _ = G.call("GET", "/repos/%s" % self.repo, tok)
        if st != 200:
            self.set("signin", "error", "%s can't see %s (HTTP %s). Sign in with the account that owns it." % (self.login, self.repo, st))
            return
        self.base = info.get("default_branch") or "main"
        cst, _, _ = G.call("GET", "/repos/%s/contents/.story-gate/config.json?ref=%s" % (self.repo, self.base), tok)
        self.already = cst == 200
        if self.already:  # set up on GitHub already: this computer only needs the AI's login and local protection
            self.set("key", "ok", "Already set up on GitHub")
            self.set("merge", "ok", "Already set up on GitHub")
        elif not (info.get("permissions") or {}).get("admin"):
            self.set("signin", "error", "%s isn't an admin of %s, so story-gate can't set its rules. Ask the owner to run setup." % (self.login, self.repo))
            return
        self.private = bool(info.get("private"))
        self.private_free = self.private and self._paid(tok, info.get("owner") or {}) is False
        self.set("signin", "ok", "Signed in as %s" % self.login)

    def _paid(self, tok, owner):
        """True/False when GitHub shows the owner's plan; None when it doesn't (the sign-in token has no `user` scope and
        orgs show their plan only to owners). Unknown is not "free": the ruleset result after the merge settles it."""
        path = "/user" if owner.get("type") == "User" else "/orgs/%s" % owner.get("login")
        st, o, _ = G.call("GET", path, tok)
        name = (o.get("plan") or {}).get("name") if st == 200 and isinstance(o, dict) else None
        return None if not name else name != "free"

    # ---- step 2: the AI's own GitHub login (agent App)
    def agent_installed(self):
        try:
            G.agent_token(self.repo)
            return True
        except Exception:
            return False

    def agent_form(self, port):
        name = ("story-gate-agent-%s-%s" % ((self.login or "me").lower(), secrets.token_hex(2)))[:34]
        mf = json.dumps(G.manifest(name, "http://127.0.0.1:%d/agent/callback" % port))
        return ('<form id="f" method="post" action="%s/settings/apps/new?state=%s"><input type="hidden" name="manifest" value="%s">'
                '</form><script>document.getElementById("f").submit()</script>' % (G.WEB, self.agent_state, html.escape(mf, quote=True)))

    def agent_callback(self, state, code):
        if state != self.agent_state or not code:
            return False
        rec = G.setup_agent(code=code, open_browser=False)
        self.set("agent", "working", "Now click Install and choose %s" % self.repo)
        if self.open_browser:
            webbrowser.open(rec["install_url"])
        self.background(self._wait_install)
        return True

    def _wait_install(self):
        for _ in range(600):
            if self.agent_installed():
                self.set("agent", "ok", "Your AI has its own GitHub login, installed on %s" % self.repo)
                if self.already:
                    self.finish(self.base)
                return
            time.sleep(2)
        self.set("agent", "error", "Didn't see the app installed on %s yet. Install it, then press the button again." % self.repo)

    # ---- step 3: judge key
    def save_key(self, key):
        key = (key or "").strip()
        if len(key) < 20 or any(c.isspace() for c in key):
            self.set("key", "error", "That doesn't look like an OpenRouter key (it starts with sk-or-).")
            return
        G.set_repo_secret(self.token, self.repo, "OPENROUTER_API_KEY", key)
        p = G.config_dir() / "judge.env"
        G.write_private(p, "OPENROUTER_API_KEY=%s\n" % key)
        self.set("key", "ok", "Saved as a GitHub secret and on this computer (never in the repository)")

    # ---- step 4: setup pull request, then branch rules after the merge
    def check_approvers(self, text):
        """Other GitHub users who may approve work. Each must exist and have write access, or GitHub ignores them as owners."""
        names, bad = [], []
        for raw in (text or "").replace(",", " ").split():
            n = raw.strip().lstrip("@")
            if not n or n.lower() == (self.login or "").lower() or n.lower() in (x.lower() for x in names):
                continue
            if not GH_USER.match(n):
                bad.append("'%s' isn't a GitHub username" % raw)
                continue
            st, info, _ = G.call("GET", "/repos/%s/collaborators/%s/permission" % (self.repo, n), self.token)
            perm = (info or {}).get("permission") if st == 200 and isinstance(info, dict) else None
            if perm not in ("admin", "maintain", "write"):
                bad.append("%s can't approve yet: they need write access to %s (Settings > Collaborators)" % (n, self.repo))
                continue
            names.append(n)
        if bad:
            raise RuntimeError("; ".join(bad) + ". Fix the list, then press the button again.")
        return names

    def open_pr(self, approvers_text=""):
        self.set("merge", "working", "Preparing the setup pull request")
        self.approvers = self.check_approvers(approvers_text)
        st, info, _ = G.call("GET", "/repos/%s" % self.repo, self.token)
        if st != 200 or not isinstance(info, dict) or not info.get("default_branch"):
            raise RuntimeError("couldn't read %s from GitHub (HTTP %s). Check you're signed in with the right account, then press "
                               "the button again." % (self.repo, st))
        base = info["default_branch"]
        git(self.top, "fetch", "--quiet", "origin", base)
        files = setup_files(self.top, base, self.py)
        owners = [self.login] + self.approvers
        co = (".github/CODEOWNERS", "# Code owners: the humans who accept work. Bots and apps cannot be code owners.\n* %s\n"
              % " ".join("@" + o for o in owners))
        files.setdefault(co[0], co[1].encode())
        body = ("This adds story-gate: the settings, the agent instructions and three workflows (the PR check, the audit and the "
                "dashboard). It was prepared by `story-gate init`. Merge it to finish setup; branch rules are applied right after.")
        self.pr = G.open_setup_pr(self.token, self.repo, files, body=body, base=base)
        self.set("merge", "working", "Waiting for you to merge the setup pull request")
        self.background(lambda: self._wait_merge(base))

    def _wait_merge(self, base):
        for _ in range(1800):
            if G.pr_merged(self.token, self.repo, self.pr["number"]):
                break
            time.sleep(2)
        else:
            self.set("merge", "error", "The setup pull request isn't merged yet. Merge it, then press the button again.")
            return
        tmp = Path(tempfile.mkdtemp(prefix="sg-rules-"))
        (tmp / ".github").mkdir()
        owners = [self.login] + self.approvers
        (tmp / ".github" / "CODEOWNERS").write_text("* %s\n" % " ".join("@" + o for o in owners), encoding="utf-8")
        notes = G.setup_repo(tmp, self.repo, owners, self.token)  # CODEOWNERS already merged; this sets the rules
        shutil.rmtree(tmp, ignore_errors=True)
        if self.private and any(n.startswith("Ruleset: NOT created (HTTP 403") for n in notes):
            self.private_free = True  # GitHub refused the rules: a private repository on the free plan
        self.set("merge", "ok", "Merged. " + " ".join(n for n in notes if n.startswith(("Ruleset", "Actions"))))
        self.finish(base)

    # ---- last: protect this computer and check everything
    def finish(self, base):
        self.set("done", "working", "Turning on protection on this computer")
        git(self.top, "fetch", "--quiet", "origin", base)
        if git(self.top, "rev-parse", "--abbrev-ref", "HEAD") == base and not git(self.top, "status", "--porcelain"):
            git(self.top, "merge", "--ff-only", "--quiet", "origin/" + base)
        r = subprocess.run([sys.executable, str(HERE / "gate.py"), "install", "--user", "--unsigned", "--python", self.py,
                            "--clients", ",".join(self.clients)],
                           cwd=str(self.top), capture_output=True, text=True, env=dict(os.environ, STORY_GATE_ROOT=str(self.top)))
        if r.returncode:
            self.set("done", "error", "Couldn't turn on protection on this computer: %s" % (r.stdout + r.stderr)[-400:])
            return
        d = subprocess.run([sys.executable, str(HERE / "gate.py"), "doctor"], cwd=str(self.top), capture_output=True, text=True)
        fails = [l.strip() for l in d.stdout.splitlines() if "FAIL" in l]
        self.set("done", "ok" if not fails else "error", "story-gate is protecting %s" % self.repo if not fails else "; ".join(fails[:3]))


# ------------------------------------------------------------------ the page
def make_handler(wz):
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _host_ok(self):  # DNS-rebinding guard: only our own loopback address
            return self.headers.get("Host", "") == "127.0.0.1:%d" % self.server.server_address[1]

        def _send(self, code, body, ctype="text/html; charset=utf-8"):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'; form-action 'self' https://github.com")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(u.query)
            if not self._host_ok():
                return self._send(403, "forbidden")
            if u.path.startswith("/font/") and u.path[6:] in FONTS:  # brand fonts, served from the installed package
                data = (HERE / "vendor" / u.path[6:]).read_bytes()
                self.send_response(200); self.send_header("Content-Type", "font/woff2"); self.send_header("Content-Length", str(len(data)))
                self.end_headers(); self.wfile.write(data)
                return
            if u.path == "/agent/callback":  # GitHub redirects here after "Create"; state proves it's our request
                ok = wz.agent_callback(qs.get("state", [""])[0], qs.get("code", [""])[0])
                return self._send(200 if ok else 400, page_shell("<h1>%s</h1><p>You can close this tab.</p>" %
                                                                 ("Created. Now click Install." if ok else "Unexpected request.")))
            if qs.get("t", [""])[0] != wz.secret:
                return self._send(403, "forbidden")
            if u.path == "/":
                return self._send(200, page(wz))
            if u.path == "/state":
                return self._send(200, json.dumps(wz.snapshot()), "application/json")
            if u.path == "/agent/start":
                if has_agent() and wz.agent_installed():
                    wz.set("agent", "ok", "Your AI already has its own GitHub login on %s" % wz.repo)
                    if wz.already:
                        wz.background(lambda: wz.finish(wz.base))
                    return self._send(200, page_shell("<h1>Already done.</h1><p>Go back to the setup page.</p>"))
                wz.set("agent", "working", "Click Create on GitHub")
                return self._send(200, page_shell(wz.agent_form(self.server.server_address[1])))
            return self._send(404, "not found")

        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(u.query)
            origin = self.headers.get("Origin", "")
            if not self._host_ok() or qs.get("t", [""])[0] != wz.secret or origin not in ("", "http://127.0.0.1:%d" % self.server.server_address[1]):
                return self._send(403, "forbidden")
            n = min(int(self.headers.get("Content-Length") or 0), 4096)
            form = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
            act = u.path.strip("/")
            if act == "signin":
                wz.start_signin()
            elif act == "key" and wz.token:
                wz.set("key", "working", "Saving")
                wz.background(lambda: wz.save_key(form.get("key", [""])[0]))
            elif act == "merge" and wz.token:
                wz.background(lambda: wz.open_pr(form.get("approvers", [""])[0]))
            elif act == "tools":
                if not wz.set_clients(form.get("clients", [""])[0]):
                    return self._send(400, json.dumps({"error": "Tick at least one tool."}), "application/json")
            return self._send(200, json.dumps(wz.snapshot()), "application/json")
    return H


def page_shell(inner):
    return ("<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>story-gate setup</title><style>" + CSS + "</style><body><main>" + inner + "</main></body></html>")


FONTS = ("ClashDisplay-600.woff2", "Switzer-400.woff2", "Switzer-600.woff2")


def wordmark():
    """The Viaknox wordmark artwork (never typed), for the 'by Viaknox' line."""
    try:
        return (HERE / "vendor" / "viaknox-wordmark.svg").read_text(encoding="utf-8")
    except OSError:
        return ""


CSS = """
@font-face{font-family:"Clash Display";font-weight:600;src:url(/font/ClashDisplay-600.woff2) format("woff2")}
@font-face{font-family:Switzer;font-weight:400;src:url(/font/Switzer-400.woff2) format("woff2")}
@font-face{font-family:Switzer;font-weight:600;src:url(/font/Switzer-600.woff2) format("woff2")}
:root{--yellow:#FFD84D;--yellow-text:#8A6A00;--graphite:#1F2327;--paper:#FFF8F2;--muted:#5B6168;--line:#E4E1DB;--ok:#1F7A4D;--error:#B42318}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--graphite);font:16px/1.5 Switzer,"Segoe UI",system-ui,sans-serif}
main{max-width:760px;margin:6vh auto;padding:0 20px}h1{font:600 40px/1.1 "Clash Display",Switzer,system-ui,sans-serif;margin:.2em 0 .3em}
.eyebrow{color:var(--yellow-text);letter-spacing:.2em;font-size:13px;font-weight:600;text-transform:uppercase}
.lead{color:var(--muted);margin:0 0 28px}.step{background:#fff;border:1px solid var(--line);border-radius:14px;padding:18px 20px;margin:12px 0;display:flex;gap:16px}
.n{font:600 28px/1 "Clash Display",system-ui;color:var(--yellow-text);min-width:28px}.step h2{font-size:18px;margin:0 0 4px}.step p{margin:0;color:var(--muted)}
.step.ok{border-color:#BFE3CF}.step.ok .act{display:none}a{color:var(--graphite)}.step.ok .n{color:var(--ok)}.step.error{border-color:var(--error)}.msg{margin-top:8px;font-size:14px}.hint{margin:8px 0 0;font-size:14px;color:var(--muted)}
button,.btn{background:var(--yellow);color:var(--graphite);border:2px solid var(--graphite);border-radius:10px;padding:10px 16px;font-weight:600;font-size:15px;cursor:pointer;text-decoration:none;display:inline-block;margin-top:10px}
button:disabled{opacity:.4;cursor:default}input{font:inherit;padding:9px 12px;border:1px solid var(--line);border-radius:10px;width:min(420px,100%)}
.lbl{display:block;margin-top:10px;font-weight:600}.lbl span{font-weight:400;color:var(--muted);font-size:14px}.lbl input{display:block;margin-top:6px}
.tool{display:block;margin:6px 0}.tool input{width:auto;margin-right:6px}.tool span{color:var(--ok);font-size:13px;margin-left:6px}.tool em{color:var(--muted);font-size:13px;font-style:normal;margin-left:6px}.other{margin:4px 0 0;padding-left:20px;color:var(--muted);font-size:14px}
.code{font:600 28px/1 ui-monospace,monospace;letter-spacing:.15em;background:var(--paper);padding:8px 12px;border-radius:8px;display:inline-block;margin-top:8px}
.by svg{height:14px;width:auto;vertical-align:-2px}.note{background:var(--yellow);color:var(--graphite);border:2px solid var(--graphite);border-radius:14px;padding:14px 18px;margin-top:18px}.foot{margin-top:28px;color:var(--muted);font-size:13px}
"""


def tools_html(wz):
    """Tickboxes for the tools story-gate can check live (found ones pre-ticked), then the ones covered another way."""
    rows = "".join("<label class=tool><input type=checkbox name=c value=%s%s> %s%s</label>"
                   % (cl, " checked" if cl in wz.clients else "", html.escape(name), (" <span>found on this computer</span>" if cl in wz.found else "")
                      + (" <em>(story-gate approves its own check inside Hermes for you)</em>" if cl == "hermes" else ""))
                   for cl, name in HOOKED_TOOLS)
    other = "".join("<li><b>%s</b>: %s</li>" % (html.escape(n), html.escape(d)) for n, d in OTHER_TOOLS)
    return ("<form id=tools onchange=\"tools(this)\">%s</form><p class=msg id=toolmsg></p><p class=hint>Also covered, without live checks: </p><ul class=other>%s</ul>"
            % (rows, other))


def page(wz):
    t = wz.secret
    steps = [
        ("signin", "Sign in to GitHub", "So story-gate can set up this repository for you.",
         "<button onclick=\"go('signin')\">Start</button><div id=dev></div>"),
        ("agent", "Give your AI its own GitHub login", "Click <b>Create</b>, then <b>Install</b> on this repository. Your AI never uses your login.",
         "<a class=btn id=agentbtn href='/agent/start?t=%s' target=_blank>Open GitHub</a>" % t),
        ("key", "Add the judge key", "Make a key at <a href='https://openrouter.ai/keys' target=_blank>openrouter.ai/keys</a> and paste it here. "
         "It goes straight into a GitHub secret; your AI never sees it.",
         "<form onsubmit=\"event.preventDefault();go('key',new URLSearchParams(new FormData(this)))\"><input name=key type=password "
         "autocomplete=off placeholder='sk-or-...'> <button>Save</button></form>"),
        ("merge", "Approve the setup", "We open a pull request with everything story-gate needs. You merge it on GitHub.",
         "<form onsubmit=\"event.preventDefault();go('merge',new URLSearchParams(new FormData(this)))\">"
         "<label class=lbl>Who else can approve work? <span>(optional: GitHub usernames with write access to this repository. "
         "Any one of you can approve.)</span>"
         "<input name=approvers autocomplete=off placeholder='e.g. alex, sam'></label>"
         "<button>Open the pull request</button></form><div id=pr></div>"),
        ("done", "Protect your AI tools", "Live checks turn on for the tools ticked below, then story-gate checks itself. "
         "Change the ticks before you merge.", tools_html(wz)),
    ]
    cards = "".join("<section class=step id=s-%s><div class=n>%s</div><div><h2>%s</h2><p>%s</p><div class=act>%s</div><div class=msg></div></div></section>"
                    % (k, i + 2, h, p, a) for i, (k, h, p, a) in enumerate(steps))
    js = """<script>
const T=%s;async function go(a,body){await fetch('/'+a+'?t='+T,{method:'POST',body:body||''});tick()}
async function tools(f){const c=[...f.querySelectorAll('input:checked')].map(i=>i.value).join(',');const r=await fetch('/tools?t='+T,{method:'POST',body:new URLSearchParams({clients:c})});
document.getElementById('toolmsg').textContent=r.ok?'':'Tick at least one tool.'}
async function tick(){const s=await (await fetch('/state?t='+T)).json();
for(const [k,v] of Object.entries(s.steps)){const e=document.getElementById('s-'+k);if(!e)continue;e.className='step '+v.status;e.querySelector('.msg').textContent=v.status=='ok'?'✓ '+v.msg:v.msg}
const d=document.getElementById('dev');if(s.device&&s.steps.signin.status!='ok'){if(d.dataset.code!==s.device.code){d.dataset.code=s.device.code;d.innerHTML='Enter this code at <a target=_blank rel=noopener></a><br><span class=code></span>';const a=d.querySelector('a');a.href=a.textContent=s.device.uri;d.querySelector('.code').textContent=s.device.code}}else{d.innerHTML='';d.dataset.code=''}
const p=document.getElementById('pr');if(s.pr&&p.dataset.url!==s.pr.url){p.dataset.url=s.pr.url;p.innerHTML='<a class=btn target=_blank rel=noopener>Review and merge on GitHub</a><p class=hint>Opens GitHub in a new tab. Click the green <b>Merge pull request</b> button there, then come back to this page.</p>';p.querySelector('a').href=s.pr.url;p.previousElementSibling.style.display='none'}
document.getElementById('free').style.display=s.private_free?'block':'none'}
tick();setInterval(tick,2000)</script>""" % json.dumps(t)
    return page_shell(
        "<div class=eyebrow>story-gate setup · %s</div><h1>Four clicks and you're protected.</h1>"
        "<p class=lead>Step 1 is done: you asked your AI to set up story-gate. Do the steps below in order; this page updates by itself.</p>%s"
        "<div class=note id=free style=display:none><b>Free GitHub plan, private repository:</b> GitHub won't enforce the "
        "'must be approved' rule here. story-gate still checks every pull request and marks it ADVISORY - NOT ENFORCED.</div>"
        "<div class=foot>Runs only on this computer (127.0.0.1). story-gate · MIT · <span class=by>by %s</span></div>%s"
        % (html.escape(wz.repo), cards, wordmark(), js))


def run(top, py, open_browser=True, port=0, serve_seconds=3600):
    repo = github_repo(top)
    if not repo:
        print("story-gate init: this folder's 'origin' isn't a GitHub repository. Open your project folder (the one you push "
              "to GitHub) and run it there.")
        return 1
    wz = Wizard(top, repo, py, open_browser=open_browser)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), make_handler(wz))
    url = "http://127.0.0.1:%d/?t=%s" % (srv.server_address[1], wz.secret)
    print("story-gate setup for %s\nOpen this page to continue (it should open by itself):\n  %s\n"
          "Steps there need you, not the AI: sign in, create the AI's login, add the judge key, merge one pull request." % (repo, url))
    sys.stdout.flush()
    if open_browser:
        webbrowser.open(url)
    t0 = time.time()
    srv.timeout = 1
    while time.time() - t0 < serve_seconds and wz.st["done"]["status"] not in ("ok",):
        srv.handle_request()
    for _ in range(5):  # let the page fetch the final state
        srv.handle_request()
    srv.server_close()
    print("story-gate: %s" % wz.st["done"]["msg"] if wz.st["done"]["status"] == "ok" else "story-gate setup stopped before the end. Run `story-gate init` again to continue.")
    return 0 if wz.st["done"]["status"] == "ok" else 1
