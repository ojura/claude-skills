#!/usr/bin/env python3
"""Scan a report's Markdown for the countable prose faults that reviews of
reports built with this skill keep finding. Read-only; prints a report with
line numbers and exits 0, or 1 with --strict when a finding of the "defect"
kind exists (a character the text font lacks, a tilde cross-reference).

    python3 tools/prose_check.py REPORT.md [--terms FILE] [--banned FILE] [--strict]

Checks, each a section of the output:
  defects     Unicode superscripts the text font lacks (write 10^{-5} in math),
              `Section~\\ref` (prints a tilde; write `Section\\ \\ref`),
              a number alone in inline code (write it as a number).
  dashes      em and en dashes, `--` outside code, a spaced hyphen as a dash.
  whose       every "whose" clause (state the fact directly instead).
  negation    negation moved onto a noun: "no X", "nothing", "none", "neither"
              (negate the verb: "does not", "every X is kept").
  quantifiers all, every, always, never, whole, entire: name the set.
  headings    headings with a comma, a colon or a question mark, or a stock
              phrase (headings are plain labels).
  length      sentences over 60 words, paragraphs over 200 words, captions over
              three sentences, table cells over 60 words.
  numbers     mixed "percent" and "%", five or more digits without a thousands
              separator, a hyphen-minus before a digit where the minus sign
              belongs, an angle range with the degree sign on one end only.
  spelling    -ise and -ize families both present, British and American pairs.
  code        a sentence or paragraph that opens with a code span; identifiers,
              paths and file names in plain text.
  compounds   hyphenated compounds of three or more parts, which are often
              coined for one sentence.
  words       vocabulary the prose standard bans or that usually stands for a
              plainer word (surface, stranded, gated, load-bearing, blast
              radius, carve-out, seam, landed, cheap, pragmatic, ...), and
              words of the authoring process (worker, subagent, pass, /tmp).
  terms       with --terms FILE: one term per line as "preferred: variant,
              variant"; reports where each variant occurs.
  refs        cross-references not in the form "Section\\ \\ref{} (Title)" or
              "(Section\\ \\ref{}, Title)": "the X chapter (Section N)" forms.

The checks read prose only: YAML blocks, fenced code and inline code spans are
left out, except where a check is about code spans.
"""
import argparse
import re
import sys
from collections import Counter, defaultdict

CODE_RE = re.compile(r"`[^`\n]*`")
MATH_RE = re.compile(r"\$[^$\n]*\$")
REF_RE = re.compile(r"\\(ref|label|eqref)\{[^}]*\}")

BANNED = [
    # the prose standard's list: decoration, loose technical terms, cost words
    r"\bsurfac(e|es|ed|ing)\b", r"\bstranded\b", r"\bgat(ed|ing)\b", r"\bsidecars?\b", r"\bload-bearing\b", r"\bblast radius\b",
    r"\bcarve-?outs?\b", r"\bbelt-and-(suspenders|braces)\b", r"\bbolt-on\b", r"\bwired up\b", r"\bfirst-class\b", r"\bguardrails?\b",
    r"\bharden(ed|ing)\b", r"\bescape hatch(es)?\b", r"\borphan(ed|s)?\b", r"\bcanonicali[sz]\w*", r"\bfootguns?\b", r"\bknobs?\b",
    r"\bseams?\b", r"\blanded\b", r"\blands\b", r"\bembryos?\b", r"\bdisappears?\b", r"\bshaped\b", r"\bcontracts?\b",
    r"\bcheap(er|ly)?\b", r"\bexpensive\b", r"\bpragmatic\b", r"good enough", r"\bfor now\b", r"quick (check|look)",
    r"diminishing returns", r"the same (mechanism|failure|trap|problem|move)", r"\brests? on\b",
    r"\bdelve\b", r"\btapestry\b", r"\brobust(ly|ness)?\b", r"\bseamless(ly)?\b", r"\bleverag\w+",
    r"\bcrucial(ly)?\b", r"\bsimply\b", r"\bjust\b", r"\bactually\b", r"\bbasically\b", r"\bessentially\b",
]
PROCESS = [r"\bworkers?\b", r"\bsubagents?\b", r"\bteammates?\b", r"/tmp/", r"\bscratch\b", r"\bpass [12]\b",
           r"\bsecond pass\b", r"\bOpus\b", r"\bClaude\b", r"\bFable\b", r"\bsession\b", r"\bsnapshot\b",
           r"\bearlier (version|draft|revision)\b", r"\bno longer\b", r"\bstill\b", r"\bnow\b"]
STOCK_HEADINGS = [r"^the short version", r"^in short", r"^tl;?dr", r"^bottom line", r"^a note on", r"^why .* matters",
                  r"^what moves where", r"^putting it together", r"^the big picture"]
UNITS = r"(?:ms|µs|us|ns|s|min|h|Hz|kHz|MHz|mm|cm|m|km|mm/s|m/s|rad|rad/s|deg|°|percent|%|pt|px|dpi|kB|MB|GB|V|A|W|g|kg)"


def body_lines(text):
    """(line number, text) outside YAML blocks and fenced code."""
    text = re.sub(r"<!--.*?-->", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    lines = text.split("\n")
    out, in_yaml, in_fence = [], False, False
    for i, l in enumerate(lines, 1):
        s = l.strip()
        if in_yaml:
            if s in ("---", "..."):
                in_yaml = False
            continue
        if s == "---" and i < len(lines) and re.match(r"^\s*[\w-]+:", lines[i]):
            in_yaml = True
            continue
        if s.startswith("```") or s.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        out.append((i, l))
    return out


def prose(l):
    """The line with code spans, math and references blanked (same length)."""
    s = CODE_RE.sub(lambda m: "\x01" * len(m.group(0)), l)
    s = MATH_RE.sub(lambda m: "\x03" * len(m.group(0)), s)
    s = REF_RE.sub(lambda m: "\x02" * len(m.group(0)), s)
    return s


def ctx(l, a, b, w=60):
    return "..." + l[max(0, a - w):min(len(l), b + w)].replace("\n", " ") + "..."


def is_table_rule(l):
    return re.fullmatch(r"\|?(\s*:?-+:?\s*\|)+\s*:?-*:?\s*\|?", l.strip()) is not None and "-" in l


def sentences(text):
    t = CODE_RE.sub(lambda m: m.group(0).replace(".", "\x04"), text)
    t = REF_RE.sub(lambda m: m.group(0).replace(".", "\x04"), t)
    for a in ("e.g.", "i.e.", "etc.", "vs.", "cf.", "approx.", "Fig.", "No.", "al."):
        t = t.replace(a, a.replace(".", "\x04"))
    t = re.sub(r"(\d)\.(\d)", lambda m: m.group(1) + "\x04" + m.group(2), t)
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z`\"(\\$])", t.strip())
    return [p.replace("\x04", ".") for p in parts if p.strip()]


def words(s):
    return len(re.findall(r"\S+", s))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("md")
    ap.add_argument("--terms", help="file of 'preferred: variant, variant' lines")
    ap.add_argument("--banned", help="file of extra regular expressions, one per line")
    ap.add_argument("--strict", action="store_true", help="exit 1 when a defect was found")
    args = ap.parse_args()
    text = open(args.md, encoding="utf-8").read()
    body = body_lines(text)
    R = []
    counts = Counter()
    defects = 0

    def section(name, items, note=None):
        R.append("## %s (%d)" % (name, len(items)))
        if note:
            R.append(note)
        for it in items:
            R.append("- " + it)
        R.append("")
        counts[name] = len(items)

    # ---- defects ----
    items = []
    for i, l in body:
        for m in re.finditer(r"[⁰⁴⁵⁶⁷⁸⁹⁺⁻ᵀ]", l):
            items.append("line %d: superscript %s, which TeX Gyre Pagella lacks; write the exponent in math: %s" % (i, m.group(0), ctx(l, m.start(), m.end(), 30)))
        for m in re.finditer(r"(Section|Sections|Figure|Table|Appendix|Chapter)~\\ref", l):
            items.append("line %d: '%s' prints a literal tilde; write '%s\\ \\ref'" % (i, m.group(0), m.group(1)))
    defects += len(items)
    section("defects", items)

    # ---- dashes ----
    items = []
    names = {"\u2014": "em dash", "\u2013": "en dash", "\u2012": "figure dash", "\u2015": "horizontal bar", "\u2e3a": "two-em dash"}
    for i, l in body:
        for ch, name in names.items():
            for m in re.finditer(re.escape(ch), l):
                items.append("line %d: %s %s" % (i, name, ctx(l, m.start(), m.end(), 40)))
        if is_table_rule(l):
            continue
        s = prose(l)
        for m in re.finditer(r"(?<![-\x01])-{2,3}(?![-\x01])", s):
            items.append("line %d: '%s' (pandoc's smart typography turns it into a dash) %s" % (i, m.group(0), ctx(l, m.start(), m.end(), 40)))
        for m in re.finditer(r"(?<=\S) - (?=\S)", s):
            items.append("line %d: a spaced hyphen used as a dash %s" % (i, ctx(l, m.start(), m.end(), 40)))
    section("dashes", items)

    # ---- whose ----
    items = []
    for i, l in body:
        s = prose(l)
        for m in re.finditer(r"\bwhose\b", s, re.I):
            items.append("line %d: %s" % (i, ctx(l, m.start(), m.end())))
    section("whose", items, "State the fact as a statement or a condition, with the thing found as the verb's subject or object.")

    # ---- negation on a noun ----
    items = []
    for i, l in body:
        s = prose(l)
        for m in re.finditer(r"\b(no(?![-\u00a0]?\d)(?! longer\b)(?! one\b)|nothing|none|nobody|no one|nowhere|neither)\b", s, re.I):
            cell = re.fullmatch(r"\s*(No|Yes|None)\s*", s[s.rfind("|", 0, m.start()) + 1:(s.find("|", m.end()) if s.find("|", m.end()) >= 0 else len(s))]) if "|" in s else None
            if cell:
                continue
            items.append("line %d: '%s' %s" % (i, m.group(0), ctx(l, m.start(), m.end(), 50)))
    section("negation", items, "Negate the verb instead ('does not write', 'every commit is kept'); a quoted protocol phrase such as 'no data available' is fine.")

    # ---- quantifiers ----
    items = []
    for i, l in body:
        s = prose(l)
        for m in re.finditer(r"\b(all|every|everything|always|never|whole|entire(ly)?|completely|fully)\b", s, re.I):
            items.append("line %d: '%s' %s" % (i, m.group(0), ctx(l, m.start(), m.end(), 50)))
    section("quantifiers", items, "Each must name its set ('every cited path', not 'everything').")

    # ---- headings ----
    items = []
    for i, l in body:
        if not l.startswith("#"):
            continue
        t = re.sub(r"\s*\{[^}]*\}\s*$", "", l.lstrip("#").strip())
        flags = []
        if "," in t:
            flags.append("comma")
        if ":" in t:
            flags.append("colon")
        if "?" in t:
            flags.append("question")
        if any(re.search(p, t, re.I) for p in STOCK_HEADINGS):
            flags.append("stock phrase")
        if flags:
            items.append("line %d: %s: %s" % (i, ", ".join(flags), t))
    section("headings", items, "A heading is a plain label of its section's content; a comma that lists the topics is fine, a comma or colon that withholds the point is not.")

    # ---- length ----
    items = []
    for i, l in body:
        if not l.strip() or l.startswith("#") or l.startswith("|"):
            continue
        cap = None
        m = re.match(r"^!\[(.*)\]\(", l)
        if m:
            cap = m.group(1)
        elif l.startswith("Table:"):
            cap = l[6:]
        if cap is not None:
            n = len(sentences(cap))
            if n > 3:
                items.append("line %d: a caption of %d sentences (three at most)" % (i, n))
            continue
        txt = re.sub(r"^\s*(?:[-*+]|\d+\.)\s+", "", l)
        for s in sentences(txt):
            n = words(s)
            if n > 60:
                items.append("line %d: a sentence of %d words: %s..." % (i, n, s[:80]))
        n = words(l)
        if n > 200:
            items.append("line %d: a paragraph of %d words and %d sentences" % (i, n, len(sentences(l))))
    for i, l in body:
        if l.startswith("|") and not is_table_rule(l):
            for ci, c in enumerate([c.strip() for c in l.strip().strip("|").split("|")], 1):
                if words(c) > 60:
                    items.append("line %d: table cell %d of %d words" % (i, ci, words(c)))
    section("length", items)

    # ---- numbers ----
    items = []
    pct_word = pct_sign = 0
    for i, l in body:
        s = prose(l)
        pct_word += len(re.findall(r"\d\s*percent\b", s))
        pct_sign += len(re.findall(r"\d\s*%", s))
        for m in re.finditer(r"(?<![\w.,:\u00a0])\d{5,}(?![\w.:])", s):
            if not re.match(r"(19|20)\d\d\d?$", m.group(0)):
                items.append("line %d: %s without a thousands separator %s" % (i, m.group(0), ctx(l, m.start(), m.end(), 30)))
        for m in re.finditer(r"(?:(?<=[\s(\[=,;:])|^)-(?=\d)", s):
            items.append("line %d: a hyphen-minus before a digit; the minus sign is U+2212 %s" % (i, ctx(l, m.start(), m.end(), 30)))
        for m in re.finditer(r"\b\d+(?:\.\d+)? to \d+(?:\.\d+)?°", s):
            if "°" not in m.group(0).split(" to ")[0]:
                items.append("line %d: '%s': the degree sign goes on both numbers of a range" % (i, m.group(0)))
    if pct_word and pct_sign:
        items.append("both 'percent' (%d) and '%%' (%d) occur in the prose; use one form" % (pct_word, pct_sign))
    section("numbers", items)

    # ---- spelling ----
    items = []
    ise, ize = Counter(), Counter()
    exclude = set("""otherwise precise rise rises arise arises noise advise advised revise revised exercise comprise promise
        enterprise surprise concise wise raise praise cruise bruise poise premise expertise compromise devise disguise supervise
        improvise franchise merchandise despise excise apprise demise guise likewise clockwise pairwise stepwise piecewise
        bitwise size sizes sized resize prize seize""".split())
    where = defaultdict(list)
    for i, l in body:
        s = prose(l)
        for m in re.finditer(r"\b([A-Za-z]+(?:is|iz)(?:e|es|ed|ing|ation|ations|er|ers))\b", s):
            w = m.group(1).lower()
            base = re.sub(r"(?:e|es|ed|ing|ation|ations|er|ers)$", "e", w)        # the dictionary form: exercises -> exercise
            if base in exclude or base.replace("ize", "ise") in exclude or base.endswith(("wise", "rise", "vise", "prise", "mise", "cise", "guise", "tise")):
                continue
            (ise if re.search(r"is(e|es|ed|ing|ation|ations|er|ers)$", w) else ize)[w] += 1
            where[w].append(i)
    if ise and ize:
        items.append("-ise words: %s" % ", ".join("%s (%d)" % kv for kv in sorted(ise.items())))
        items.append("-ize words: %s" % ", ".join("%s (lines %s)" % (k, ", ".join(map(str, where[k][:6]))) for k in sorted(ize)))
    pairs = [r"behaviou?rs?", r"colou?rs?", r"centre|center", r"metres?|meters?", r"labell?ed", r"modell?ed", r"licen[cs]e",
             r"artefacts?|artifacts?", r"judge?ments?", r"acknowledge?ments?", r"grey|gray", r"analog(ue)?", r"catalog(ue)?"]
    for p in pairs:
        cnt = Counter()
        for i, l in body:
            for m in re.finditer(r"\b(" + p + r")\b", prose(l), re.I):
                cnt[re.sub(r"s$", "", m.group(1).lower())] += 1
        if len(cnt) > 1:
            items.append("both forms: " + ", ".join("%s (%d)" % kv for kv in sorted(cnt.items())))
    section("spelling", items)

    # ---- code ----
    items = []
    plain_tokens = defaultdict(list)
    for i, l in body:
        for m in re.finditer(r"`([−-]?\d[\d.,]*(?:e-?\d+)?)`", l):
            items.append("line %d: the number %s alone in code font (right for a recorded field or log value; otherwise write it as a number, or in math for an exponent)" % (i, m.group(1)))
        if l.startswith("`"):
            items.append("line %d: the paragraph opens with a code span: %s" % (i, l[:70]))
        elif re.match(r"^\s*(?:[-*+]|\d+\.)\s+`", l):
            items.append("line %d: the list item opens with a code span: %s" % (i, l.strip()[:70]))
        for m in re.finditer(r"(?<=[.!?] )`[^`]+`", l):
            items.append("line %d: a sentence opens with a code span %s" % (i, ctx(l, m.start(), m.end(), 30)))
        if l.startswith("#"):
            continue
        s = prose(l)
        for m in re.finditer(r"(?<![\w`/.\-])(?:[A-Za-z0-9_.\-]+/)+[A-Za-z0-9_.\-]+(?::\d+(?:[-,]\d+)*)?", s):
            tok = m.group(0)
            if re.search(r"\d+/\d+", tok) or tok.count("/") == 1 and re.fullmatch(r"[A-Za-z]+/[A-Za-z]+", tok):
                continue      # fractions, "and/or", "m/s"
            items.append("line %d: the path %s is in plain text" % (i, tok))
        for m in re.finditer(r"\b[a-z]+(?:_[a-z0-9]+)+\b|\b[a-z]+[A-Z][A-Za-z0-9]{2,}\b|\b[\w\-]+\.(?:cpp|hpp|py|yaml|yml|json|msg|launch|sh|csv|md|txt|log|toml)\b", s):
            plain_tokens[m.group(0)].append(i)
    for tok, ls in sorted(plain_tokens.items()):
        items.append("the identifier or file name %s is in plain text on %d line%s (%s%s)" % (tok, len(ls), "s" if len(ls) > 1 else "", ", ".join(map(str, ls[:8])), "..." if len(ls) > 8 else ""))
    section("code", items, "Identifiers, file names and paths go in code font (a repository name used as a name may stay roman); a sentence does not open with one.")

    # ---- compounds ----
    items = []
    seen = Counter()
    for i, l in body:
        s = prose(l)
        for m in re.finditer(r"\b[A-Za-z0-9]+(?:-[A-Za-z0-9]+){2,}\b", s):
            w = m.group(0)
            if re.fullmatch(r"[A-Za-z]+-[A-Za-z]+-[A-Za-z]+", w) and w.split("-")[1] in ("minus", "to", "and", "or", "by", "per", "of", "in", "the", "a"):
                continue
            seen[w] += 1
            if seen[w] == 1:
                items.append("line %d: %s" % (i, w))
    section("compounds", items, "A compound of three or more parts is usually coined for one sentence; say what it means.")

    # ---- words ----
    items = []
    banned = list(BANNED)
    if args.banned:
        banned += [x.strip() for x in open(args.banned, encoding="utf-8") if x.strip() and not x.startswith("#")]
    for p in banned + PROCESS:
        for i, l in body:
            s = prose(l)
            for m in re.finditer(p, s, re.I):
                items.append("line %d: '%s' %s" % (i, m.group(0), ctx(l, m.start(), m.end(), 45)))
    section("words", items, "Banned or decorative vocabulary, cost words, and words of the authoring process; each needs the plain fact or must go.")

    # ---- terms ----
    items = []
    if args.terms:
        for spec in open(args.terms, encoding="utf-8"):
            spec = spec.strip()
            if not spec or spec.startswith("#") or ":" not in spec:
                continue
            preferred, variants = spec.split(":", 1)
            for v in [x.strip() for x in variants.split(",") if x.strip()]:
                hits = [i for i, l in body if re.search(r"\b" + re.escape(v) + r"\b", prose(l), re.I)]
                if hits:
                    items.append("'%s' for '%s': %d lines (%s%s)" % (v, preferred.strip(), len(hits), ", ".join(map(str, hits[:10])), "..." if len(hits) > 10 else ""))
        section("terms", items, "One name per concept; the preferred form is the first in each line of the terms file.")

    # ---- refs ----
    items = []
    for i, l in body:
        for m in re.finditer(r"\bthe [\w\- ]+ (chapter|section) \(Section", l):
            items.append("line %d: '%s': write 'Section\\ \\ref{...} (Title)'" % (i, m.group(0)))
        for m in re.finditer(r"\bthe (chapter|section) on [\w\- ]+ \(Section", l):
            items.append("line %d: '%s': write 'Section\\ \\ref{...} (Title)'" % (i, m.group(0)))
    section("refs", items)

    total = sum(counts.values())
    print("# Prose check of %s: %d findings (%s)" % (args.md, total, ", ".join("%s %d" % kv for kv in counts.items() if kv[1])))
    print()
    print("\n".join(R))
    if args.strict and defects:
        sys.exit(1)


if __name__ == "__main__":
    main()
