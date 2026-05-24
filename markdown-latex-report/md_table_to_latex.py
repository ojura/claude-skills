#!/usr/bin/env python3
"""Convert pipe tables in a markdown file to raw-LaTeX longtables with
explicit column widths. Usage:

    md_table_to_latex_general.py FILE --anchor TEXT --widths W1,W2,... [--caption STR]

The first pipe-table after the line matching --anchor (exact strip equality)
is converted in-place. Widths are fractions of \\textwidth; '-' means right-aligned
fixed-width column (use 'r').
"""
import re
import sys
import argparse
from pathlib import Path


def md_to_tex(s):
    out = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "`":
            try:
                j = s.index("`", i + 1)
            except ValueError:
                out.append("`")
                i += 1
                continue
            code = s[i+1:j]
            code = code.replace("\\", "\\textbackslash{}")\
                       .replace("_", "\\_")\
                       .replace("&", "\\&")\
                       .replace("%", "\\%")\
                       .replace("#", "\\#")\
                       .replace("{", "\\{")\
                       .replace("}", "\\}")\
                       .replace("<", "\\textless{}")\
                       .replace(">", "\\textgreater{}")
            out.append("\\texttt{" + code + "}")
            i = j + 1
            continue
        if s.startswith("**", i):
            try:
                j = s.index("**", i + 2)
            except ValueError:
                out.append("**")
                i += 2
                continue
            inner = md_to_tex(s[i+2:j])
            out.append("\\textbf{" + inner + "}")
            i = j + 2
            continue
        if ch == "[":
            m = re.match(r"\[([^\]]*)\]\(([^)]+)\)", s[i:])
            if m:
                text_part = m.group(1)
                url = m.group(2)
                if url.startswith("file://"):
                    url = url[len("file://"):]
                url_esc = url.replace("%", "\\%").replace("#", "\\#")
                inner = md_to_tex(text_part)
                out.append("\\href{" + url_esc + "}{" + inner + "}")
                i += m.end()
                continue
        if ch in "&%#_$":
            out.append("\\" + ch)
            i += 1
            continue
        replacements = {
            "×": "$\\times$",
            "→": "$\\to$",
            "≈": "$\\approx$",
            "≥": "$\\ge$",
            "≤": "$\\le$",
            "−": "--",
            "—": "---",
            "–": "--",
            "…": "\\ldots{}",
        }
        if ch in replacements:
            out.append(replacements[ch])
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def is_sep(cells):
    return all(re.fullmatch(r":?-+:?", c) for c in cells if c)


def _strip_markdown(s):
    """Approximate visual length: strip ` ` markers, [text](url) -> text,
    **bold** -> bold, etc. Used for content-proportional column widths."""
    import re as _re
    s = _re.sub(r"\[([^\]]*)\]\([^)]+\)", r"\1", s)
    s = _re.sub(r"`([^`]+)`", r"\1", s)
    s = _re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
    return s


def auto_widths(header, body, n_cols, total=0.95):
    """Pick fraction-of-textwidth widths proportional to max content length
    per column, normalised so the sum equals `total`."""
    max_len = [0] * n_cols
    for row in [header] + body:
        for i, cell in enumerate(row[:n_cols]):
            ml = len(_strip_markdown(cell))
            if ml > max_len[i]:
                max_len[i] = ml
    # Minimum length to avoid hairline columns
    max_len = [max(m, 3) for m in max_len]
    s = sum(max_len)
    return [f"{(m / s) * total:.4f}" for m in max_len]


def convert_table(path: Path, anchor: str, widths: list, caption: str):
    text = path.read_text()
    lines = text.splitlines(keepends=True)
    table_start = None
    for i, line in enumerate(lines):
        if line.strip() == anchor:
            j = i + 1
            while j < len(lines) and not lines[j].lstrip().startswith("|"):
                j += 1
            if j < len(lines):
                table_start = j
            break
    if table_start is None:
        print(f"  anchor '{anchor}' not found, skipping", file=sys.stderr)
        return
    table_end = table_start
    while table_end < len(lines) and lines[table_end].lstrip().startswith("|"):
        table_end += 1
    raw = "".join(lines[table_start:table_end])
    rows = []
    for line in raw.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        rows.append(cells)
    rows = [r for r in rows if not is_sep(r)]
    if not rows:
        print(f"  empty table at '{anchor}', skipping", file=sys.stderr)
        return
    header, *body = rows
    n_cols = len(header)
    # `auto` => derive proportional widths from content lengths.
    if widths == ["auto"]:
        widths = auto_widths(header, body, n_cols)
    # Pad widths if too short
    while len(widths) < n_cols:
        widths.append(None)
    col_spec_parts = []
    for w in widths[:n_cols]:
        if w in (None, "", "-"):
            col_spec_parts.append("r")
        elif w == "l":
            col_spec_parts.append("l")
        elif w == "c":
            col_spec_parts.append("c")
        else:
            # raggedright: prevents \hbox-padding hyphenation when content fits.
            # \arraybackslash restores \\ for row terminators.
            col_spec_parts.append(f">{{\\raggedright\\arraybackslash}}p{{{w}\\textwidth}}")
    col_spec = "@{}" + " ".join(col_spec_parts) + "@{}"

    out = []
    out.append("```{=latex}\n")
    out.append("\\begin{small}\n")
    out.append("\\begin{longtable}{" + col_spec + "}\n")
    if caption:
        out.append(f"\\caption{{{caption}}}\\\\\n")
    out.append("\\toprule\n")
    out.append(" & ".join("\\textbf{" + md_to_tex(c) + "}" for c in header) + " \\\\\n")
    out.append("\\midrule\n")
    out.append("\\endfirsthead\n")
    out.append("\\toprule\n")
    out.append(" & ".join("\\textbf{" + md_to_tex(c) + "}" for c in header) + " \\\\\n")
    out.append("\\midrule\n")
    out.append("\\endhead\n")
    body_row_count = 0
    for row in body:
        # Handle markdown "section" rows that have a single bold cell + empty rest
        # by emitting a multi-row spanning bold line.
        non_empty = [c for c in row if c]
        if len(non_empty) == 1 and non_empty[0].startswith("**") and non_empty[0].endswith("**"):
            label = md_to_tex(non_empty[0])
            out.append(f"\\multicolumn{{{n_cols}}}{{@{{}}l@{{}}}}{{\\textit{{{label}}}}} \\\\\n")
            out.append("\\addlinespace\n")
            body_row_count = 0   # reset; next body row won't be preceded by an hline
            continue
        # Faint inter-row rule (color: ruleline!35 from header.tex) before every
        # non-first body row. Helps when cells wrap across multiple lines.
        if body_row_count > 0:
            out.append("\\hline\n")
        row = row[:n_cols] + [""] * (n_cols - len(row))
        cells = [md_to_tex(c) for c in row]
        out.append(" & ".join(cells) + " \\\\\n")
        body_row_count += 1
    out.append("\\bottomrule\n")
    out.append("\\end{longtable}\n")
    out.append("\\end{small}\n")
    out.append("```\n")
    new_text = "".join(lines[:table_start] + out + lines[table_end:])
    path.write_text(new_text)
    print(f"  converted {len(body)} rows at lines {table_start+1}..{table_end} after '{anchor}'")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("file")
    p.add_argument("--anchor", required=True)
    p.add_argument("--widths", required=True, help="comma-separated; each entry: fraction of \\textwidth, or 'r'/'l'/'c'")
    p.add_argument("--caption", default="")
    args = p.parse_args()
    widths = [w.strip() for w in args.widths.split(",")]
    convert_table(Path(args.file), args.anchor, widths, args.caption)
