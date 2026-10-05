"""Plain-writing score: an STE-style proxy, not ASD-STE100 compliance.

story-gate asks agents to write in short, plain English that a non-coder can read (rules in PROTOCOL.md, "Writing").
This module measures a small, published subset of those rules on text the agent wrote itself:

  - a sentence that starts with an instruction verb has at most 20 words; any other sentence at most 25
  - a paragraph has at most 6 sentences (sentences after the sixth fail)

score = sentences that pass / sentences checked. Only prose is checked: front matter, fenced code (Mermaid too),
tables, block quotes, headings, HTML comments, inline code and URLs are removed first. Passive voice and long words
are reported as hints; they never change the score. The ASD-STE100 dictionary is not used or shipped.
Stdlib only.
"""
import re

INSTRUCTION_VERBS = frozenset(
    "add apply ask build call change check choose click close commit configure copy create delete deploy do don't "
    "download edit enable disable enter fill find fix get give go install keep let list log make merge move open "
    "paste pick push put read record remove rename replace restart run save see select send set show start stop "
    "test try turn type update upgrade use verify wait write".split())
ABBREVIATIONS = ("e.g.", "i.e.", "etc.", "vs.", "approx.", "no.", "dr.", "mr.", "mrs.", "ms.", "u.s.", "fig.", "cf.", "incl.")
MERMAID_KINDS = ("flowchart", "graph", "sequenceDiagram", "classDiagram", "stateDiagram", "stateDiagram-v2", "erDiagram",
                 "journey", "gantt", "pie", "mindmap", "timeline", "gitGraph", "quadrantChart")
PASSIVE = re.compile(r"\b(?:is|are|was|were|be|been|being)\s+(?:\w+ly\s+)?\w+(?:ed|en)\b", re.I)
WORD = re.compile(r"[A-Za-zÀ-ɏ0-9][\w'’-]*")


FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")


def blocks(text):
    """Split Markdown lines into (kind, lines): 'text' outside fences, or ('fence', info, lines) for a fenced block.
    A fence closes only on the same character, at least as long, with nothing but spaces after it (CommonMark)."""
    out, cur, fence = [], [], None
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        if fence is None:
            m = FENCE_OPEN.match(line)
            if m and m.group(1)[0] == "`" and "`" in m.group(2):
                m = None  # ```a`b``` is inline code, not a fence (CommonMark: no backtick in a backtick fence's info)
            if m:
                if cur:
                    out.append(("text", None, cur)); cur = []
                info = m.group(2).strip()
                fence = (m.group(1)[0], len(m.group(1)), info.split()[0].lower() if info else "")
                continue
            cur.append(line)
        else:
            m = re.match(r"^ {0,3}(%s{%d,})\s*$" % (re.escape(fence[0]), fence[1]), line)
            if m:
                out.append(("fence", fence[2], cur)); cur, fence = [], None
                continue
            cur.append(line)
    if fence is not None:
        out.append(("open_fence", fence[2], cur))  # never closed: not prose, and not a diagram
    elif cur:
        out.append(("text", None, cur))
    return out


def prose(text):
    """Paragraphs of prose (a list item is its own paragraph), with everything that isn't prose removed."""
    text = (text or "").replace("\r\n", "\n")
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        text = text[end + 4:] if end != -1 else ""
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    lines = []
    for kind, _, ls in blocks(text):  # fenced blocks (Mermaid included) are not prose
        lines += ls if kind == "text" else [""]
    skip = set()
    for i, line in enumerate(lines):  # tables, with or without leading pipes: the rule row, its header and its rows
        if TABLE_RULE.match(line) and "|" in line:
            skip.add(i)
            if i and "|" in lines[i - 1]:
                skip.add(i - 1)
            j = i + 1
            while j < len(lines) and lines[j].strip() and "|" in lines[j]:
                skip.add(j); j += 1
    paras, cur = [], []
    for i, line in enumerate(lines):
        s = "" if i in skip else line.strip()
        bullet = re.match(r"^(?:[-*+]|\d+[.)])\s+", s)
        if not s or s.startswith(("#", ">", "|")) or re.match(r"^[-=*_]{3,}$", s):
            if cur:
                paras.append(" ".join(cur)); cur = []
            continue
        if bullet:
            if cur:
                paras.append(" ".join(cur)); cur = []
            s = s[bullet.end():]
        cur.append(s)
    if cur:
        paras.append(" ".join(cur))
    out = []
    for p in paras:
        p = re.sub(r"`[^`]*`", " CODE ", p)
        p = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", p)
        p = re.sub(r"https?://\S+", " LINK ", p)
        p = re.sub(r"[*_]{1,3}", "", p).strip()
        if WORD.search(p):
            out.append(p)
    return out


def sentences(paragraph):
    """Split at . ! ? followed by a space and a capital (any script), digit or quote. Abbreviations, decimals, versions and
    file names don't split because the dot isn't followed by a space."""
    p = paragraph
    for i, a in enumerate(ABBREVIATIONS):
        p = re.sub(re.escape(a), "\x00%d\x00" % i, p, flags=re.I)
    parts = []
    for piece in re.split(r"(?<=[.!?])\s+", p):  # a new sentence starts with a capital (any script), digit, quote or bracket
        if parts and not (piece[:1].isupper() or piece[:1].isdigit() or piece[:1] in "\"'\u201c("):
            parts[-1] += " " + piece
        else:
            parts.append(piece)
    out = []
    for s in parts:
        s = re.sub(r"\x00(\d+)\x00", lambda m: ABBREVIATIONS[int(m.group(1))], s).strip()
        if WORD.search(s):
            out.append(s)
    return out


def words(sentence):
    return WORD.findall(sentence)


def limit(sentence):
    w = words(sentence)
    return 20 if w and w[0].lower() in INSTRUCTION_VERBS else 25


def score_text(text):
    """{'checked', 'passed', 'score' (None when there is no prose), 'long_sentences', 'long_paragraphs', 'passive'}."""
    checked = passed = 0
    long_s, long_p, passive = [], 0, 0
    for para in prose(text):
        ss = sentences(para)
        if len(ss) > 6:
            long_p += 1
        for i, s in enumerate(ss):
            checked += 1
            n, lim = len(words(s)), limit(s)
            ok = n <= lim and i < 6
            passed += ok
            if n > lim:
                long_s.append((n, lim, s))
            passive += len(PASSIVE.findall(s))
    return {"checked": checked, "passed": passed, "score": round(passed / checked, 3) if checked else None,
            "long_sentences": sorted(long_s, key=lambda x: -x[0]), "long_paragraphs": long_p, "passive": passive}


def has_diagram(text):
    """A fenced ```mermaid block that is closed and starts with a known diagram type."""
    for kind, info, ls in blocks(text):
        if kind == "fence" and info == "mermaid":
            first = next((l.strip() for l in ls if l.strip() and not l.strip().startswith("%%")), "")
            if first.split(" ")[0] in MERMAID_KINDS:
                return True
    return False


def section(text, heading):
    """Body of a '## heading' section, or ''."""
    m = re.search(r"(?ms)^##\s+%s\s*$(.*?)(?=^##\s|\Z)" % re.escape(heading), text or "")
    return m.group(1) if m else ""


def report(parts, target):
    """parts: {label: text}. Combined score over every checked sentence, plus per-part detail."""
    per = {k: score_text(v) for k, v in parts.items()}
    checked = sum(r["checked"] for r in per.values())
    passed_ = sum(r["passed"] for r in per.values())
    exact = passed_ / checked if checked else None  # compare the exact share; round only for display
    return {"score": round(exact, 3) if exact is not None else None, "target": target, "checked": checked, "passed": passed_,
            "status": "not_applicable" if exact is None else ("ok" if exact >= target else "below_target"),
            "parts": {k: {"score": r["score"], "checked": r["checked"], "long_paragraphs": r["long_paragraphs"],
                          "passive": r["passive"],
                          "examples": ["%d words (max %d): %s" % (n, lim, s[:160]) for n, lim, s in r["long_sentences"][:3]]}
                      for k, r in per.items()}}
