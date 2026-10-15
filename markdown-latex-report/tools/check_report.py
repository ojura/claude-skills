#!/usr/bin/env python3
"""Check a report built by the markdown-latex-report skill, from its LaTeX log, its PDF
and its Markdown source. `make check` runs it; by hand:

    python3 tools/check_report.py --md REPORT.md --tex build/REPORT.tex \\
        --log build/REPORT.log --pdf build/REPORT.pdf --out build \\
        [--root DIR] [--charts DIR] [--render 60 | --no-render]

It writes OUT/check.md and prints it, renders the pages into OUT/render at the
given dpi, and exits 1 when the PDF has a defect that loses or garbles content
(an overfull vertical box, a missing character, an undefined reference, a
missing figure) and 0 otherwise. Every finding names a page or a source line,
so that the writer can go there.

What it checks:
  log      overfull and underfull boxes with their page and source line,
           missing characters, undefined references, rerun and label warnings,
           float warnings, the hyphenation rules in force, the skill's warnings;
  heads    the running head of each page against the first section that starts
           on it, or the section in force (from the .toc);
  pages    pages that hold only a figure, pages that end early and why, pages
           that end in a heading, headings followed by fewer than three lines,
           the pages on which a table continues, a doubled rule at a table break;
  code     inline code broken at a line end inside a name, by kind;
  source   quoted section titles against their headings, cited repository
           paths that are missing or untracked, images that are missing or have
           no .pdf sibling, captions longer than three sentences.

Needs PyMuPDF (pip install pymupdf) and pdftoppm (poppler-utils) for the renders.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from collections import Counter, defaultdict

try:
    import pymupdf
except ImportError:  # older releases
    import fitz as pymupdf

# Geometry of header.tex: margin 0.9in, top 0.85in, bottom 1.0in (points).
MARGIN_X = 0.9 * 72
MARGIN_TOP = 0.85 * 72
MARGIN_BOTTOM = 1.0 * 72
SHORT_PAGE_PT = 100          # a page that ends more than this early is reported
FILE_EXT = ("py", "cpp", "hpp", "cc", "c", "h", "yaml", "yml", "json", "md", "txt", "log", "csv",
            "sh", "launch", "xml", "msg", "srv", "html", "cmake", "toml", "ini", "cfg", "lua",
            "tex", "rs", "go", "js", "ts", "pdf", "png")


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def norm(s):
    """Letters and digits only, lower case: for matching log snippets, page text and headings."""
    s = unicodedata.normalize("NFKC", s).replace("\u00ad", "")
    return re.sub(r"[^0-9a-z]", "", s.lower())


def strip_tex(s):
    """Plain text of a toc entry: commands and braces removed."""
    s = re.sub(r"\\numberline\s*\{([^}]*)\}", r"\1 ", s)
    s = s.replace("\\textquotesingle", "'").replace("\\textendash", "-").replace("\\textemdash", "-")
    s = re.sub(r"\\[A-Za-z@]+\s*", "", s)
    s = s.replace("{", "").replace("}", "").replace("~", " ")
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------------------
# The log


def parse_log(log_text):
    """Box warnings with the page they were being set for, and the other warnings."""
    lines = log_text.split("\n")
    ship_re = re.compile(r"(?:^|(?<=[\s)]))\[(\d+)(?=[\]<{\s]|$)")
    warn_re = re.compile(r"^(Overfull|Underfull) \\([hv])box \(([^)]*)\)(.*)$")
    font_re = re.compile(r"\\TU/[A-Za-z]+\(\d+\)/[a-z]+/[a-z]+/[\d.]+ ?")
    boxes = []
    shipped = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        m = warn_re.match(line)
        if m:
            kind, hv, amount, rest = m.groups()
            dump = []
            j = i + 1
            while j < len(lines) and lines[j].strip() not in ("[]", ""):
                dump.append(lines[j])
                j += 1
            text = font_re.sub("", "".join(dump)).replace("[]", "")
            badness = None
            mb = re.match(r"badness (\d+)", amount)
            if mb:
                badness = int(mb.group(1))
            mt = re.search(r"at lines (\d+)--(\d+)", rest)
            boxes.append({
                "kind": kind, "hv": hv, "amount": amount, "badness": badness,
                "rest": rest.strip(), "text": text.strip(), "log_line": i + 1,
                "page_guess": shipped + 1,
                "tex_lines": (int(mt.group(1)), int(mt.group(2))) if mt else None,
            })
            i = j
            continue
        for ms in ship_re.finditer(line):
            n = int(ms.group(1))
            if n == shipped + 1:
                shipped = n
        i += 1
    other = {
        "missing_character": [l for l in lines if "Missing character" in l],
        "undefined": [l for l in lines if re.search(r"(Reference|Citation) .* undefined", l)],
        "rerun": [l for l in lines if "Rerun to get" in l or "Label(s) may have changed" in l],
        "float": [l for l in lines if re.search(r"Float too large|float\(s\) lost|contains only floats", l)],
        "font": [l for l in lines if "Font Warning" in l or ("Font shape" in l and "undefined" in l)],
        "skill": [l for l in lines if "markdown-latex-report" in l and "Warning" in l],
        "hyphen": [l for l in lines if "Hyphen rules for" in l],
        "latex_warnings": [l for l in lines if re.match(r"(LaTeX|Package \S+) Warning", l)],
    }
    return boxes, other


# ---------------------------------------------------------------------------
# The toc: sections with pages


def parse_toc(toc_path):
    entries = []
    if not os.path.exists(toc_path):
        return entries
    rx = re.compile(r"\\contentsline \{(\w+)\}\{(.*)\}\{(\d+)\}\{[^}]*\}%?\s*$")
    for line in open(toc_path, encoding="utf-8", errors="replace"):
        m = rx.match(line.strip())
        if not m:
            continue
        level, title_tex, page = m.group(1), m.group(2), int(m.group(3))
        mn = re.match(r"\\numberline\s*\{([^}]*)\}", title_tex)
        entries.append({"level": level, "number": mn.group(1) if mn else None,
                        "title": strip_tex(re.sub(r"^\\numberline\s*\{[^}]*\}", "", title_tex)),
                        "page": page})
    return entries


# ---------------------------------------------------------------------------
# The PDF


def is_bold(span):
    return bool(span["flags"] & 16) or "Bold" in span["font"]


def is_mono(span):
    return bool(span["flags"] & 8) or "Mono" in span["font"]


def analyse_page(page):
    """Running head, body lines (sorted), content bottom, drawings' horizontal rules, figure area."""
    W, H = page.rect.width, page.rect.height
    top, bottom = MARGIN_TOP, H - MARGIN_BOTTOM
    d = page.get_text("dict")
    head_spans, lines, blocks = [], [], []
    content_bottom = top
    for b in d["blocks"]:
        if b["type"] != 0:
            if b["bbox"][1] >= top - 5:
                content_bottom = max(content_bottom, min(b["bbox"][3], bottom + 2))
            continue
        blines = []
        for l in b["lines"]:
            spans = [s for s in l["spans"] if s["text"].strip()]
            if not spans:
                continue
            x0, y0, x1, y1 = l["bbox"]
            text = "".join(s["text"] for s in spans)
            if y1 < top - 5:
                head_spans.append((x0, text.strip()))
                continue
            if y0 > bottom + 5:
                continue
            rec = {"y0": y0, "y1": y1, "x0": x0, "x1": x1, "text": text, "spans": spans}
            lines.append(rec)
            blines.append(rec)
            content_bottom = max(content_bottom, min(y1, bottom + 2))
        if blines:
            blocks.append(blines)
    lines.sort(key=lambda l: (round(l["y0"], 1), l["x0"]))
    # a numbered heading comes as two text objects at one height, the number and the title
    merged = []
    for l in lines:
        if merged and abs(merged[-1]["y0"] - l["y0"]) < 1.0 and l["x0"] - merged[-1]["x1"] < 30 \
                and all(is_bold(s) for s in merged[-1]["spans"] + l["spans"]):
            m = merged[-1]
            m["text"] = m["text"].rstrip() + " " + l["text"].lstrip()
            m["x1"] = max(m["x1"], l["x1"])
            m["y1"] = max(m["y1"], l["y1"])
            m["spans"] = m["spans"] + l["spans"]
        else:
            merged.append(l)
    lines = merged
    head_spans.sort()
    if head_spans and re.fullmatch(r"\d+", head_spans[-1][1]):
        head_spans = head_spans[:-1]
    head = re.sub(r"\s+", " ", " ".join(t for _, t in head_spans)).strip()
    rules = []
    figures = []        # bounding boxes of drawn or embedded figures
    for dr in page.get_drawings():
        r = dr.get("rect")
        if r is None:
            continue
        if r.y1 >= top - 5:
            content_bottom = max(content_bottom, min(r.y1, bottom + 2))
        for it in dr["items"]:
            if it[0] == "l":
                p1, p2 = it[1], it[2]
                if abs(p1.y - p2.y) < 0.5 and abs(p2.x - p1.x) > 100:
                    rules.append(round(p1.y, 1))
            elif it[0] == "re":
                rr = it[1]
                if rr.height < 1.5 and rr.width > 100:
                    rules.append(round(rr.y0, 1))
                elif rr.height > 20 and rr.width > 100:
                    figures.append(pymupdf.Rect(rr))
    for info in page.get_image_info():
        figures.append(pymupdf.Rect(info["bbox"]))
    # merge the figure boxes into regions; a text line inside a region belongs to the figure
    regions = []
    for f in figures:
        for g in regions:
            if g.intersects(f) or (abs(g.y0 - f.y0) < 40 and abs(g.y1 - f.y1) < 40):
                g.include_rect(f)
                break
        else:
            regions.append(pymupdf.Rect(f))
    for l in lines:
        lb = pymupdf.Rect(l["x0"], l["y0"], l["x1"], l["y1"])
        l["in_figure"] = any(g.contains(lb) or (g.intersects(lb) and lb.get_area() and g.intersect(lb).get_area() > 0.6 * lb.get_area()) for g in regions)
    return {
        "width": W, "height": H, "top": top, "bottom": bottom,
        "head": head, "lines": lines, "blocks": blocks,
        "content_bottom": content_bottom, "rules": sorted(set(rules)),
        "figure_area": sum(g.get_area() for g in regions),
    }


def is_heading_line(l, margin_x):
    sp = l["spans"]
    return (all(is_bold(s) and s["color"] != 0 and s["size"] >= 9.5 for s in sp)
            and l["x0"] < margin_x + 15 and not l["text"].strip().startswith("Table "))


DESIGNED_BREAKS = ("after an underscore", "after ::", "after a slash", "after a comma")


def classify_code_break(wa, wb, src_flat):
    """The kind of break between two lines of inline code, or None. The kinds in
    DESIGNED_BREAKS are the filter's intended break points and are only counted."""
    joined = wa + wb
    if wa.endswith("-") and len(wa) > 1:
        if re.search(r"\d-$", wa) and re.match(r"\d", wb):
            return "line range split after its hyphen"
        if joined in src_flat:
            return "name split after its hyphen"
    elif wa.endswith("::"):
        return "after ::"
    elif wa.endswith("/") and len(wa) > 1 and wa not in ("~/",):
        return "after a slash"
    elif wa.endswith(","):
        return "after a comma"
    elif wa.endswith(".") and re.match(r"[A-Za-z]{1,5}\b", wb) and joined in src_flat:
        return "name split before its extension"
    elif wa in ("/", "~/"):
        return "lone slash at the line end"
    elif wa.endswith(":") and joined in src_flat:
        return "split after a colon"
    elif re.search(r"\d\.$", wa) and re.match(r"\d", wb):
        return "number split at its decimal point"
    elif wa.endswith("_") and joined in src_flat:
        return "after an underscore"
    elif re.search(r"[a-z]$", wa) and re.match(r"[A-Z]", wb) and joined in src_flat:
        return "camelCase name split"
    return None


# ---------------------------------------------------------------------------
# The Markdown source


def md_body(md_text):
    """(line number, text) of the Markdown outside YAML blocks, fenced code and HTML comments."""
    md_text = re.sub(r"<!--.*?-->", lambda m: "\n" * m.group(0).count("\n"), md_text, flags=re.S)
    lines = md_text.split("\n")
    body = []
    in_yaml = in_fence = False
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
        body.append((i, l))
    return body


def md_headings(body):
    heads = []
    for i, l in body:
        m = re.match(r"^(#+)\s+(.*?)\s*(\{([^}]*)\})?\s*$", l)
        if m:
            attrs = m.group(4) or ""
            ml = re.search(r"#([^\s}]+)", attrs)
            heads.append({"line": i, "level": len(m.group(1)), "title": m.group(2).strip(),
                          "label": ml.group(1) if ml else None})
    return heads


def quoted_titles(body, heads):
    labels = {h["label"]: h["title"] for h in heads if h["label"]}
    rx = re.compile(r"(?:Sections?|Appendix|Appendices|Chapters?)\\ \\ref\{([^}]+)\}"
                    r"(?: \(([^)]*)\)|, ([^),;]*(?:\([^)]*\)[^),;]*)*)\))")
    out = []
    for i, l in body:
        for m in rx.finditer(l):
            label = m.group(1)
            title = m.group(2) if m.group(2) is not None else m.group(3)
            if label in labels and title.strip() != labels[label].strip():
                out.append((i, label, title.strip(), labels[label]))
    return out


def cited_paths(body, root):
    """Code spans that name files under root: the missing ones and the untracked ones."""
    if not root or not os.path.isdir(root):
        return [], [], 0
    tracked = None
    res = run(["git", "-C", root, "ls-files", "--full-name", "--", "."])
    if res.returncode == 0:
        top = run(["git", "-C", root, "rev-parse", "--show-toplevel"]).stdout.strip()
        rel_root = os.path.relpath(os.path.abspath(root), top)
        rel_root = "" if rel_root == "." else rel_root + "/"
        tracked = {f[len(rel_root):] for f in res.stdout.split("\n") if f.startswith(rel_root)}
    by_name = defaultdict(set)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for fn in filenames:
            by_name[fn].add(os.path.relpath(os.path.join(dirpath, fn), root))
    tops = set(os.listdir(root))
    missing, untracked, bare, checked, seen = [], [], [], 0, set()
    for i, l in body:
        for span in re.findall(r"`([^`\n]+)`", l):
            s = re.sub(r":\d[\d,\-]*$", "", span.strip())
            if " " in s or not s or s.startswith(("/", "~", "$", "-")):
                continue
            if "/" in s:
                if s.split("/", 1)[0] not in tops:
                    continue
                checked += 1
                if s in seen:
                    continue
                seen.add(s)
                p = os.path.join(root, s)
                if not os.path.exists(p):
                    missing.append((i, s))
                elif tracked is not None:
                    rel = s.rstrip("/")
                    if os.path.isdir(p):
                        if not any(t.startswith(rel + "/") for t in tracked):
                            untracked.append((i, s))
                    elif rel not in tracked:
                        untracked.append((i, s))
            elif "." in s and s.rsplit(".", 1)[-1] in FILE_EXT and re.match(r"^[\w.\-]+$", s):
                checked += 1
                if s in seen:
                    continue
                seen.add(s)
                if s not in by_name:
                    bare.append(s)
                elif tracked is not None and len(by_name[s]) == 1:
                    rel = next(iter(by_name[s]))
                    if rel not in tracked:
                        untracked.append((i, rel))
    return missing, untracked, bare, checked


def images(body, md_dir, charts_dir):
    missing, raster = [], []
    for i, l in body:
        for m in re.finditer(r"!\[[^\]]*\]\(([^)\s]+)", l):
            src = m.group(1)
            cands = [os.path.join(md_dir, src)]
            if charts_dir:
                cands.append(os.path.join(charts_dir, os.path.basename(src)))
            found = next((c for c in cands if os.path.exists(c)), None)
            if not found:
                missing.append((i, src))
            elif found.endswith(".png") and not os.path.exists(found[:-4] + ".pdf"):
                raster.append((i, src))
    return missing, raster


def long_captions(body):
    out = []
    abbrev = ("e.g.", "i.e.", "etc.", "vs.", "cf.", "approx.", "Fig.", "No.")
    for i, l in body:
        m = re.match(r"^!\[(.*)\]\(", l)
        cap = m.group(1) if m else (l[6:] if l.startswith("Table:") else None)
        if cap is None:
            continue
        t = re.sub(r"`[^`]*`", "x", cap)
        t = re.sub(r"\\(ref|label)\{[^}]*\}", "x", t)
        for a in abbrev:
            t = t.replace(a, a.replace(".", ""))
        t = re.sub(r"(\d)\.(\d)", r"\1\2", t)
        n = len([s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z(\\$])", t.strip()) if s.strip()])
        if n > 3:
            out.append((i, n, cap[:80]))
    return out


# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--md", required=True)
    ap.add_argument("--tex", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--root", help="directory the cited repository paths are relative to (default: the report's)")
    ap.add_argument("--charts", help="charts directory, as the Makefile's CHARTS_DIR")
    ap.add_argument("--render", type=int, default=60, help="dpi of the page renders (default 60)")
    ap.add_argument("--no-render", action="store_true")
    args = ap.parse_args()
    for p in (args.md, args.tex, args.log, args.pdf):
        if not os.path.exists(p):
            sys.exit("check_report: missing %s" % p)
    os.makedirs(args.out, exist_ok=True)
    md_text = open(args.md, encoding="utf-8").read()
    md_dir = os.path.dirname(os.path.abspath(args.md))
    root = os.path.abspath(args.root) if args.root else md_dir
    log_text = open(args.log, encoding="utf-8", errors="replace").read()
    tex_lines = open(args.tex, encoding="utf-8", errors="replace").read().split("\n")
    src_flat = re.sub(r"\s+", " ", md_text)

    doc = pymupdf.open(args.pdf)
    n_pages = len(doc)
    P = [None] + [analyse_page(doc[i]) for i in range(n_pages)]      # 1-based
    pages_norm = [None] + [norm(" ".join(l["text"] for l in P[p]["lines"])) for p in range(1, n_pages + 1)]
    body = md_body(md_text)
    body_norm = [(i, norm(l)) for i, l in body]
    R, hard = [], []

    boxes, other = parse_log(log_text)
    over_h = [b for b in boxes if b["kind"] == "Overfull" and b["hv"] == "h"]
    over_v = [b for b in boxes if b["kind"] == "Overfull" and b["hv"] == "v"]
    under_h = [b for b in boxes if b["kind"] == "Underfull" and b["hv"] == "h"]
    under_v = [b for b in boxes if b["kind"] == "Underfull" and b["hv"] == "v"]

    def locate(b):
        key = norm(b["text"])
        page = None
        for cut in (0, 6, 12, 20, 30):
            k = key[cut:len(key) - cut] if cut and len(key) > 2 * cut + 10 else key
            if len(k) < 8:
                break
            hits = [p for p in range(1, n_pages + 1) if k in pages_norm[p]]
            if len(hits) == 1:
                page = hits[0]
                break
            if len(hits) > 1:
                near = [h for h in hits if abs(h - b["page_guess"]) <= 1]
                if len(near) == 1:
                    page = near[0]
                    break
        src = None
        for start in range(0, max(1, len(key) - 24), 6):
            probe = key[start:start + 24]
            if len(probe) < 16:
                break
            hits = [i for i, t in body_norm if probe in t]
            if len(hits) == 1:
                src = hits[0]
                break
        para = ""
        if b["tex_lines"]:
            a = b["tex_lines"][0] - 1
            para = tex_lines[a].strip()[:70] if 0 <= a < len(tex_lines) else ""
        return page, src, para

    W, H = P[1]["width"], P[1]["height"]
    undef = other["undefined"]
    R.append("# Check of %s" % os.path.basename(args.md))
    R.append("")
    R.append("Pages %d, %.0f x %.0f pt. Overfull boxes %d (%d horizontal, %d vertical). "
             "Underfull lines %d (%d above badness 1000, %d above 3000). Missing characters %d. "
             "Undefined references %d." % (
                 n_pages, W, H, len(over_h) + len(over_v), len(over_h), len(over_v), len(under_h),
                 sum(1 for b in under_h if (b["badness"] or 0) > 1000),
                 sum(1 for b in under_h if (b["badness"] or 0) > 3000),
                 len(other["missing_character"]), len(undef)))
    R.append("")

    # ---- log ----
    R.append("## Log")
    R.append("")
    said = False
    for l in other["hyphen"]:
        m = re.search(r"Hyphen rules for '([^']+)' set to (\S+)", l)
        if m:
            said = True
            if "l@nil" in m.group(2):
                R.append("- Hyphenation rules for %s: none, so nothing is hyphenated; the language has no patterns on this installation (see bblopts.cfg in the skill)." % m.group(1))
            else:
                R.append("- Hyphenation rules for %s: %s." % (m.group(1), m.group(2).rstrip(".")))
    for b in over_v:
        hard.append("overfull vertical box")
        said = True
        R.append("- Overfull vertical box (%s), page about %d: text is pushed past the page edge and lost. %s" % (b["amount"], b["page_guess"], b["rest"]))
    for l in other["missing_character"]:
        hard.append("missing character")
        said = True
        R.append("- %s (the font lacks the glyph; a Unicode superscript or a symbol outside the text font: write it in math mode)" % l.strip())
    for l in undef:
        hard.append("undefined reference")
        said = True
        R.append("- %s" % l.strip())
    for l in other["rerun"]:
        said = True
        R.append("- %s (after three LaTeX runs; a label that changes on every run, for example a Unicode Greek letter in a caption, which belongs in math mode)" % l.strip())
    for grp in ("float", "skill", "font"):
        for l in other[grp]:
            said = True
            R.append("- %s" % l.strip())
    seen_lines = set(other["rerun"] + other["float"] + other["skill"] + other["font"] + undef)
    for l in other["latex_warnings"]:
        if l not in seen_lines:
            said = True
            R.append("- %s" % l.strip())
    if not said:
        R.append("- The log has no warning.")
    R.append("")

    def box_report(title, items, note):
        R.append("## %s" % title)
        R.append("")
        R.append(note)
        R.append("")
        if not items:
            R.append("- none")
        for b in sorted(items, key=lambda b: -(b["badness"] or 0)):
            page, src, para = locate(b)
            where = "page %d" % page if page else "page about %d" % b["page_guess"]
            srcs = ", source line %d" % src if src else ""
            amount = "badness %d" % b["badness"] if b["badness"] is not None else b["amount"]
            R.append("- %s, %s%s: %s" % (where, amount, srcs, (b["text"][:110] or para)))
        R.append("")

    box_report("Overfull lines", over_h,
               "A line that runs into the margin: a long token without a break point, usually a path or an identifier outside inline code.")
    box_report("Underfull lines", [b for b in under_h if (b["badness"] or 0) >= 1000],
               "Lines with stretched spaces (badness 1000 and above; 10000 is the worst). A long token in inline code left TeX no good break: move it earlier in the sentence, or name a directory once and cite the files by name.")
    if under_v:
        R.append("## Underfull pages")
        R.append("")
        for b in under_v:
            R.append("- page about %d: %s %s" % (b["page_guess"], b["amount"], b["rest"]))
        R.append("")

    # ---- running heads ----
    R.append("## Running heads")
    R.append("")
    sections = [e for e in parse_toc(os.path.splitext(args.tex)[0] + ".toc") if e["level"] == "section"]
    mismatches = []
    if sections:
        by_page = defaultdict(list)
        for e in sections:
            by_page[e["page"]].append(e)
        first_page = min(e["page"] for e in sections)
        in_force = None
        for p in range(1, n_pages + 1):
            starts = by_page.get(p, [])
            expect = starts[0] if starts else in_force
            if p >= max(first_page, 2) and expect is not None:      # page 1 is the title page, without a head
                exp_text = ("%s %s" % (expect["number"] or "", expect["title"])).strip()
                head, exp = norm(P[p]["head"]), norm(exp_text)
                if head != exp and not (head and exp.startswith(head)) and not (exp and head.startswith(exp)):
                    mismatches.append((p, P[p]["head"], exp_text))
            if starts:
                in_force = starts[-1]
        if mismatches:
            for p, got, exp in mismatches:
                R.append("- page %d: the head is '%s'; expected '%s'." % (p, got, exp))
        else:
            R.append("- Every page from %d on names the first section that starts on it, or the section in force (%d sections)." % (max(first_page, 2), len(sections)))
    else:
        R.append("- No section entries in the .toc, so the heads were not checked.")
    R.append("")

    # ---- pages ----
    R.append("## Pages")
    R.append("")
    float_pages, cont_pages, doubled = [], [], []
    for p in range(1, n_pages + 1):
        pg = P[p]
        ls = pg["lines"]
        outside = [l for l in ls if not l.get("in_figure")]
        if outside and re.match(r"Figure \S+[.:]", outside[0]["text"].strip()) and pg["figure_area"] > 0.15 * pg["width"] * pg["height"]:
            # outside the figures only one caption block: each later line follows the previous one closely
            if all(outside[k]["y0"] - outside[k - 1]["y1"] < 6 for k in range(1, len(outside))):
                float_pages.append(p)
        cont_line = next((l for l in ls if re.match(r"Table \S+ \(continued\)", l["text"].strip())), None)
        if cont_line:
            cont_pages.append(p)
            near = [y for y in pg["rules"] if cont_line["y1"] - 2 <= y <= cont_line["y1"] + 40]
            for a, b2 in zip(near, near[1:]):
                if b2 - a < 1.5:
                    doubled.append((p, a))
    short = []
    for p in range(1, n_pages):
        pg = P[p]
        gap = pg["bottom"] - pg["content_bottom"]
        if gap > SHORT_PAGE_PT:
            nxt = P[p + 1]["lines"]
            first = nxt[0]["text"].strip() if nxt else ""
            why = "unknown"
            if p in float_pages:
                why = "the page holds only a figure"
            elif re.match(r"Table \S+ \(continued\)", first):
                why = "a table row did not fit; the table continues on the next page"
            elif nxt and is_heading_line(nxt[0], MARGIN_X):
                why = "the next page starts with the heading '%s' (a page break before it, or its heading reserve)" % first[:50]
            elif re.match(r"Table \S+[.:]", first):
                why = "the next page starts with a table; its caption, header and first row did not fit here"
            elif nxt and all(is_mono(s) for s in nxt[0]["spans"]) and nxt[0]["spans"][0]["size"] < 9:
                why = "the next page starts with a code block that did not fit here"
            elif re.match(r"Figure \S+[.:]", first) or (p + 1) in float_pages or (nxt and nxt[0].get("in_figure")):
                why = "the next page starts with a figure"
            short.append((p, int(round(gap)), why))
    R.append("- Pages that hold only a figure: %s." % (", ".join(str(p) for p in float_pages) if float_pages else "none"))
    R.append("- Pages on which a table continues: %s." % (", ".join(str(p) for p in cont_pages) if cont_pages else "none"))
    for p, y in doubled:
        R.append("- page %d: two rules within 1.5 pt of each other at y = %.1f (a doubled rule at the table break)." % (p, y))
    if short:
        R.append("- Pages that end more than %d pt early (the last page excepted):" % SHORT_PAGE_PT)
        for p, g, why in short:
            R.append("  - page %d, %d pt empty: %s." % (p, g, why))
    else:
        R.append("- Every page but the last ends within %d pt of the foot of the text area." % SHORT_PAGE_PT)
    ending, few = [], []
    for p in range(1, n_pages + 1):
        ls = P[p]["lines"]
        for k, l in enumerate(ls):
            if not is_heading_line(l, MARGIN_X):
                continue
            # a heading may span lines; count the text lines after its last line
            j = k
            while j + 1 < len(ls) and is_heading_line(ls[j + 1], MARGIN_X) and ls[j + 1]["y0"] - ls[j]["y1"] < 6:
                j += 1
            after = [x for x in ls[j + 1:] if not is_heading_line(x, MARGIN_X)]
            if not ls[j + 1:]:
                ending.append((p, l["text"].strip()))
            elif len(after) < 3 and p < n_pages and not any(is_heading_line(x, MARGIN_X) for x in ls[j + 1:]):
                few.append((p, l["text"].strip(), len(after)))
    R.append("- Pages that end in a heading: %s." % ("; ".join("page %d ('%s')" % (p, t[:60]) for p, t in ending) if ending else "none"))
    for p, t, a in few:
        R.append("- page %d: the heading '%s' has only %d line(s) after it on the page." % (p, t[:60], a))
    R.append("")

    # ---- code breaks ----
    R.append("## Inline code broken at line ends")
    R.append("")
    breaks, designed = [], Counter()
    for p in range(1, n_pages + 1):
        for bl in P[p]["blocks"]:
            listing = len(bl) >= 3 and all(all(is_mono(s) for s in l["spans"]) for l in bl)
            if listing:
                continue        # a code block: its lines are the author's
            for a, b2 in zip(bl, bl[1:]):
                sa, sb = a["spans"][-1], b2["spans"][0]
                ta, tb = sa["text"].rstrip(), sb["text"].lstrip()
                if not ta or not tb or not is_mono(sa) or not is_mono(sb):
                    continue
                wa = re.split(r"[\s(\[]", ta)[-1]
                wb = re.split(r"[\s,;)\]]", tb)[0]
                if not wa or not wb:
                    continue
                kind = classify_code_break(wa, wb, src_flat)
                if kind in DESIGNED_BREAKS:
                    designed[kind] += 1
                elif kind:
                    breaks.append((p, kind, wa + "|" + wb))
    if designed:
        R.append("- Breaks at the filter's intended points: %s." % ", ".join("%d %s" % (n, k) for k, n in designed.most_common()))
    if not breaks:
        R.append("- No inline code is broken inside a name.")
    else:
        for kind, n in Counter(k for _, k, _ in breaks).most_common():
            pg = sorted({p for p, k, _ in breaks if k == kind})
            R.append("- %s: %d (page%s %s)" % (kind, n, "s" if len(pg) > 1 else "", ", ".join(str(p) for p in pg)))
        R.append("")
        R.append("A break inside a name is the filter's last resort in a narrow table cell or a justified line: move the token, cite a shorter form, or widen the column.")
        for p, kind, s in breaks:
            R.append("  - page %d: %s (%s)" % (p, s, kind))
    R.append("")

    # ---- source ----
    R.append("## Source")
    R.append("")
    heads = md_headings(body)
    qt = quoted_titles(body, heads)
    for i, label, title, actual in qt:
        R.append("- line %d: the reference to %s quotes '%s'; the heading is '%s'." % (i, label, title, actual))
    tilde = [(i, l) for i, l in body if re.search(r"(Section|Figure|Table|Appendix)~\\ref", l)]
    for i, l in tilde:
        R.append("- line %d: 'Section~\\ref' prints a literal tilde in pandoc Markdown; write 'Section\\ \\ref{...}'." % i)
    missing, untracked, bare, checked = cited_paths(body, root)
    if checked:
        R.append("- Cited paths under %s: %d checked%s" % (root, checked, "." if not (missing or untracked) else ":"))
        for i, s in missing:
            R.append("  - line %d: %s does not exist." % (i, s))
        for i, s in untracked:
            R.append("  - line %d: %s is not tracked in git; commit it with the report." % (i, s))
        if bare:
            R.append("- File names cited without a path and not found under the root (fine when they are source files of another repository): %s." % ", ".join(bare))
    img_missing, img_raster = images(body, md_dir, args.charts)
    for i, s in img_missing:
        hard.append("missing image")
        R.append("- line %d: the image %s was not found." % (i, s))
    for i, s in img_raster:
        R.append("- line %d: the image %s has no .pdf sibling, so the PDF embeds the raster." % (i, s))
    for i, n, cap in long_captions(body):
        R.append("- line %d: a caption of %d sentences (three at most): %s..." % (i, n, cap))
    if not (qt or tilde or missing or untracked or bare or img_missing or img_raster or long_captions(body)):
        R.append("- Quoted titles match their headings; every cited path and image exists.")
    R.append("")

    if not args.no_render:
        rd = os.path.join(args.out, "render")
        shutil.rmtree(rd, ignore_errors=True)
        os.makedirs(rd)
        run(["pdftoppm", "-r", str(args.render), "-png", args.pdf, os.path.join(rd, "p")])
        R.append("Pages rendered at %d dpi: %s/p-NN.png. Look at every page this report names, and at a sample of the others." % (args.render, rd))
        R.append("")

    report = "\n".join(R)
    with open(os.path.join(args.out, "check.md"), "w", encoding="utf-8") as f:
        f.write(report + "\n")
    print(report)
    if hard:
        print("check_report: defects that lose or garble content: " + ", ".join(sorted(set(hard))), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
