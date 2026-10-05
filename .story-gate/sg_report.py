"""The validation page: one self-contained HTML page per story that shows the owner whether it works.

It is a derived view. validation.md, scenarios.json, scenario_results.json, test_results.json and the verdicts stay the
records; this page only shows them. Rules it keeps:
  - Everything the agent wrote is escaped. No scripts, no raw HTML, no links that load anything, and a strict CSP.
  - Every fact is labelled: "checked" (story-gate or CI ran it) or "reported" (the agent says so).
  - Screenshots are PNG or JPEG only, checked by their first bytes, size- and count-capped, and read only from the
    story's evidence folder (no symlinks). They are always "reported": a picture is not proof.
Stdlib only.
"""
import base64, hashlib, html, json, re, struct, time
from pathlib import Path

MAX_IMAGE, MAX_IMAGES_TOTAL, MAX_IMAGES, MAX_SIDE = 300_000, 2_000_000, 10, 4000
MAX_TEXT = 20_000


def image_info(data):
    """('png'|'jpg', width, height) for a PNG or JPEG, else ValueError. Reads headers only; never decodes pixels."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR" and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
        return "png", w, h
    if data[:3] == b"\xff\xd8\xff":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg = struct.unpack(">H", data[i + 2:i + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return "jpg", w, h
            i += 2 + seg
        raise ValueError("a JPEG without a size header")
    raise ValueError("not a PNG or JPEG file")


def check_image(data):
    """Return image type and dimensions; raise ValueError for invalid headers or exceeded size limits."""
    kind, w, h = image_info(data)
    if len(data) > MAX_IMAGE:
        raise ValueError("%d KB is over the %d KB limit; crop it or save it as JPEG" % (len(data) // 1000, MAX_IMAGE // 1000))
    if not (0 < w <= MAX_SIDE and 0 < h <= MAX_SIDE):
        raise ValueError("%dx%d pixels; the limit is %d on each side" % (w, h, MAX_SIDE))
    return kind, w, h


def evidence_list(doc):
    """Return dictionary entries from an evidence list, or an empty list for a malformed document."""
    e = doc.get("evidence") if isinstance(doc, dict) else None
    return [x for x in e if isinstance(x, dict)] if isinstance(e, list) else []


def add_evidence(sd, src, scenario, caption, scenario_names):
    """Copy a screenshot into stories/<ID>/evidence/ and list it in evidence.json. Returns the record."""
    p = Path(src)
    if p.is_symlink() or not p.is_file():
        raise ValueError("%s is not a regular file" % src)
    if scenario not in scenario_names:
        raise ValueError("no recorded scenario named %r (record the scenario first)" % scenario)
    with open(p, "rb") as fh:
        data = fh.read(MAX_IMAGE + 1)
    kind, w, h = check_image(data)
    doc = json.loads((sd / "evidence.json").read_text(encoding="utf-8")) if (sd / "evidence.json").is_file() else {}
    items = evidence_list(doc)
    digest = hashlib.sha256(data).hexdigest()
    items = [x for x in items if (x.get("sha256"), x.get("scenario")) != (digest, scenario)]  # same picture, other scenario: kept
    if len(items) >= MAX_IMAGES:
        raise ValueError("a story can have at most %d screenshots" % MAX_IMAGES)
    if sum(x["bytes"] for x in items if isinstance(x.get("bytes"), int)) + len(data) > MAX_IMAGES_TOTAL:
        raise ValueError("screenshots for one story are limited to %d KB in total" % (MAX_IMAGES_TOTAL // 1000))
    name = "%s.%s" % (digest[:16], kind)
    (sd / "evidence").mkdir(exist_ok=True)
    (sd / "evidence" / name).write_bytes(data)
    rec = {"file": name, "sha256": digest, "bytes": len(data), "width": w, "height": h, "scenario": scenario,
           "caption": (caption or "")[:300], "source": "local", "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    items.append(rec)
    (sd / "evidence.json").write_text(json.dumps({"evidence": items}, indent=1), encoding="utf-8")
    return rec


def load_images(sd, doc):
    """[(record, data-URI)] for evidence that still passes every check; a reason string for any that doesn't."""
    out, skipped, total = [], [], 0
    base = (sd / "evidence").resolve()
    for rec in evidence_list(doc)[:MAX_IMAGES]:
        name = str(rec.get("file") or "")
        if not re.fullmatch(r"[0-9a-f]{16}\.(png|jpg)", name):
            skipped.append("%s: not a story-gate evidence name" % name[:40]); continue
        f = sd / "evidence" / name
        try:
            if f.is_symlink() or f.resolve().parent != base or not f.is_file():
                raise ValueError("not a regular file in the evidence folder")
            data = f.read_bytes()[:MAX_IMAGE + 1]
            kind, _, _ = check_image(data)
            if hashlib.sha256(data).hexdigest() != rec.get("sha256"):
                raise ValueError("the file changed after it was recorded")
            total += len(data)
            if total > MAX_IMAGES_TOTAL:
                raise ValueError("over the total size limit")
        except (OSError, ValueError) as e:
            skipped.append("%s: %s" % (name, e)); continue
        mime = "image/png" if kind == "png" else "image/jpeg"
        out.append((rec, "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))))
    return out, skipped


# ------------------------------------------------------------------ safe text
def esc(s, n=2000):
    """Convert a value to text, remove control characters, truncate it, and escape it for HTML."""
    s = s if isinstance(s, str) else ("" if s is None else str(s))
    return html.escape(re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", s)[:n], quote=True)


def inline(s):
    """Escaped text with `code` and **bold**; links become plain text (nothing on this page loads or navigates)."""
    s = re.sub(r"\[([^\]]*)\]\(([^)]*)\)", r"\1 (\2)", s)
    s = html.escape(s, quote=True)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)


def md_html(text):
    """A small, safe Markdown subset: headings, paragraphs, lists, tables and code blocks. Everything else is text."""
    import sg_writing as W
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", (text or "").replace("\r\n", "\n"))[:MAX_TEXT]
    out = []
    for kind, info, lines in W.blocks(text):
        if kind != "text":
            if info == "mermaid":
                out.append('<div class="text-secondary small">Diagram (Mermaid source). GitHub draws it in validation.md.</div>')
            out.append("<pre class=\"sg-code\">%s</pre>" % html.escape("\n".join(lines)))
            continue
        para, lst, i = [], None, 0

        def flush():
            """Render and clear the pending paragraph and list into the Markdown output."""
            nonlocal para, lst
            if para:
                out.append("<p>%s</p>" % inline(" ".join(para))); para = []
            if lst:
                out.append("<%s>%s</%s>" % (lst[0], "".join("<li>%s</li>" % inline(x) for x in lst[1]), lst[0])); lst = None
        while i < len(lines):
            s = lines[i].strip()
            m = re.match(r"^(#{1,6})\s+(.*)$", s)
            b = re.match(r"^(?:([-*+])|(\d+)[.)])\s+(.*)$", s)
            if not s:
                flush()
            elif m:
                flush()
                lvl = min(len(m.group(1)) + 1, 4)
                out.append("<h%d>%s</h%d>" % (lvl, inline(m.group(2)), lvl))
            elif "|" in s and i + 1 < len(lines) and re.match(r"^\s*\|?\s*:?-{3,}", lines[i + 1]):
                flush()
                rows = [s]
                i += 2
                while i < len(lines) and "|" in lines[i] and lines[i].strip():
                    rows.append(lines[i].strip()); i += 1
                cells = [[c.strip() for c in r.strip("|").split("|")] for r in rows]
                out.append('<div class="table-responsive"><table class="table card-table"><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>' % (
                    "".join("<th>%s</th>" % inline(c) for c in cells[0]),
                    "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % inline(c) for c in r) for r in cells[1:])))
                continue
            elif b:
                tag = "ul" if b.group(1) else "ol"
                if para or (lst and lst[0] != tag):
                    flush()
                lst = lst or (tag, [])
                lst[1].append(b.group(3))
            elif s.startswith(">"):
                flush()
                out.append("<blockquote>%s</blockquote>" % inline(s.lstrip("> ")))
            else:
                if lst:
                    lst[1][-1] += " " + s
                else:
                    para.append(s)
            i += 1
        flush()
    return "\n".join(out)


# ------------------------------------------------------------------ the page
CSS = """
.sg-code{color:var(--sg-ink);background:var(--sg-card);border:1px solid var(--sg-line);padding:.75rem;border-radius:6px;white-space:pre-wrap;word-break:break-word;font-size:.85rem}
.sg-tag{font-size:.75rem;text-transform:uppercase;letter-spacing:.05em;padding:.1rem .45rem;border-radius:4px;border:1px solid var(--sg-line)}
.sg-tag.checked{border-color:#2E7D4F} .sg-tag.reported{border-color:var(--sg-accent-text)}
figure img{max-width:100%;height:auto;border:1px solid var(--sg-line);border-radius:6px}
.sg-doc h2,.sg-doc h3{margin-top:1.25rem} .sg-nowrap{white-space:nowrap} code{color:var(--sg-ink)} details summary{cursor:pointer}
"""


def tag(kind):
    """Return an HTML badge for a 'checked' or 'reported' fact; reject other kinds with KeyError."""
    return '<span class="sg-tag %s">%s</span>' % (kind, {"checked": "checked", "reported": "reported"}[kind])


def to_html(f):
    """f: the facts gate.py gathers (see gate.report_facts). Returns the page."""
    import sg_dashboard as D
    css = D.TABLER_FILE.read_text(encoding="utf-8") if D.TABLER_FILE.is_file() else ""
    mark = D.WORDMARK_FILE.read_text(encoding="utf-8") if D.WORDMARK_FILE.is_file() else ""
    mark = re.sub(r'fill="#[0-9A-Fa-f]{6}"', 'fill="currentColor"', re.sub(r"<title>.*?</title>", "", mark)) if mark else ""
    cov, res = f["coverage"], f["results"]
    proved = len([a for a, s in cov.items() if s == "ok"])

    def verdict_cell(label, v):
        """Render a labelled verdict card, marking missing and stale verdicts explicitly."""
        st = (v or {}).get("overall")
        stale = v and not v.get("fresh")
        return ('<div class="col-6 col-md-3"><div class="card sg-kpi"><div class="card-body"><div class="subheader">%s</div>'
                '<div class="h1">%s</div><div class="sg-est">%s</div></div></div></div>' % (
                    esc(label), D.pill(st if not stale else None) if st else D.pill(None),
                    "out of date: the work changed since" if stale else ("not run yet" if not st else "on this exact code")))
    tr = f.get("tests") or {}
    tests_line = ("no recorded test run" if not tr else "%s · exit %s%s" % (
        "CI's own run" if tr.get("source") == "ci" else "the agent's run", esc(tr.get("exit_code"), 10),
        "" if tr.get("fresh") else " · out of date"))
    kpis = (verdict_cell("READY gate", f.get("ready")) + verdict_cell("DONE gate", f.get("done"))
            + '<div class="col-6 col-md-3"><div class="card sg-kpi"><div class="card-body"><div class="subheader">Criteria shown working</div>'
              '<div class="h1">%d/%d</div><div class="sg-est">by a scenario run on this code</div></div></div></div>' % (proved, len(f["acs"]))
            + '<div class="col-6 col-md-3"><div class="card sg-kpi"><div class="card-body"><div class="subheader">Tests</div>'
              '<div class="h1">%s</div><div class="sg-est">%s</div></div></div></div>' % (
                  D.pill("PASS" if tr.get("exit_code") == 0 and tr.get("fresh") else ("FAIL" if tr else None)), tests_line))
    by_ac = {}
    for s in f["scenarios"]:
        for a in [a for a in (s.get("acs") if isinstance(s.get("acs"), list) else []) if isinstance(a, str)]:
            by_ac.setdefault(a, []).append(s)

    def run_of(s):
        """Return a scenario's result, freshness, source label, and current pass/fail status."""
        r = res.get(s.get("name")) if isinstance(s.get("name"), str) else None
        r = r or {}
        fresh = r.get("spec") == f["spec_hash"](s) and r.get("fingerprint") == f["fingerprint"]
        who = tag("checked") + " CI" if r.get("source") == "ci" else (tag("reported") + " agent's computer" if r else "")
        st = ("PASS" if r.get("passed") else "FAIL") if r and fresh else None
        return r, fresh, who, st
    ac_rows = ""
    for aid, text, refs in f["acs"]:
        sc = by_ac.get(aid, [])
        cells = "<br>".join("%s %s %s" % (D.pill(run_of(s)[3]), esc(s.get("name"), 80), run_of(s)[2]) for s in sc) or '<span class="text-secondary">none recorded</span>'
        ac_rows += "<tr><td class=\"sg-nowrap\"><strong>%s</strong></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            esc(aid, 20), esc(text, 300), cells, esc(", ".join(refs), 300) or "—",
            {"ok": D.pill("PASS"), "local": D.pill("CONCERNS"), "missing": D.pill("FAIL")}.get(cov.get(aid), D.pill(None)))
    sc_rows = ""
    for s in f["scenarios"]:
        r, fresh, who, st = run_of(s)
        argv = s.get("argv") if isinstance(s.get("argv"), list) else []
        sc_rows += ("<tr><td><strong>%s</strong>%s</td><td>%s</td><td><code>%s</code></td><td>exit %s, output contains <code>%s</code></td>"
                    "<td>%s %s%s</td></tr>") % (
            esc(s.get("name"), 80), ('<div class="text-secondary small">local only: %s</div>' % esc(s.get("local_only"), 200)) if s.get("local_only") else "",
            esc(", ".join(map(str, s.get("acs") or [])) if isinstance(s.get("acs"), list) else "", 100), esc(" ".join(map(str, argv)), 500),
            esc(s.get("expect_exit"), 10), esc(s.get("expect_output"), 300), D.pill(st), who,
            "" if not r else ('<details><summary class="small">output%s</summary><pre class="sg-code">%s</pre></details>' % (
                (" · " + esc(r.get("note"), 300)) if r.get("note") else ("" if fresh else " · not run on this code"),
                esc(r.get("output_tail"), 4000))))
    imgs = "".join('<figure class="col-md-6"><img alt="%s" src="%s"><figcaption class="small text-secondary">%s · scenario: %s · %s</figcaption></figure>' % (
        esc(rec.get("caption") or "screenshot", 300), uri, esc(rec.get("caption"), 300), esc(rec.get("scenario"), 80), tag("reported"))
        for rec, uri in f["images"])
    if f.get("images_skipped"):
        imgs += '<div class="alert alert-warning">Not shown: %s</div>' % esc("; ".join(f["images_skipped"]), 1000)
    card = lambda title, body, note="": ('<div class="card mb-3"><div class="card-header"><h3 class="card-title">%s</h3>%s</div>%s</div>'
                                         % (title, ('<div class="card-actions">%s</div>' % note) if note else "", body))
    return """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:">
<title>Validation %s</title><style>%s</style><style>%s</style><style>%s</style></head><body>
<header class="sg-head py-3 mb-3"><div class="container-xl"><h1 class="m-0">%s: %s</h1>
<div class="text-secondary">Validation page · made %s · %s at %s</div></div></header>
<main class="container-xl">
<p class="text-secondary">%s: CI ran it, or story-gate worked it out from its own records. %s: the AI wrote it, or ran it on its own computer. Read it as a claim.</p>
<div class="row row-cards mb-3">%s</div>
%s%s%s%s%s
</main>
<footer class="container-xl py-3"><span class="sg-brand text-secondary">story-gate · by <span aria-label="Viaknox">%s</span></span></footer>
</body></html>""" % (
        esc(f["id"], 40), css, D.BRAND_CSS, CSS, esc(f["id"], 40), esc(f.get("title"), 200), esc(f.get("generated_at"), 30),
        esc(f.get("branch"), 100), esc((f.get("commit") or "")[:12], 12), tag("checked"), tag("reported"), kpis,
        card("What this story does", '<div class="card-body">%s</div>' % (md_html(f.get("summary")) or '<span class="text-secondary">No plain summary in story.md.</span>'), tag("reported")),
        card("Acceptance criteria", '<div class="table-responsive"><table class="table card-table"><thead><tr><th>AC</th><th>Criterion</th>'
             '<th>Scenarios</th><th>Automated tests</th><th>Shown working</th></tr></thead><tbody>%s</tbody></table></div>' % (
                 ac_rows or '<tr><td colspan="5" class="text-secondary">No acceptance criteria in tests.json.</td></tr>')),
        card("Scenarios run", '<div class="table-responsive"><table class="table card-table"><thead><tr><th>Scenario</th><th>Covers</th>'
             '<th>Command</th><th>Expected</th><th>Result</th></tr></thead><tbody>%s</tbody></table></div>' % (
                 sc_rows or '<tr><td colspan="5" class="text-secondary">No scenarios recorded.</td></tr>')),
        card("Screenshots", '<div class="card-body"><div class="row">%s</div></div>' % imgs, tag("reported")) if imgs else "",
        card("Validation summary", '<div class="card-body sg-doc">%s</div>' % (md_html(f.get("validation_md")) or
             '<span class="text-secondary">validation.md is missing.</span>'), tag("reported")),
        mark)
