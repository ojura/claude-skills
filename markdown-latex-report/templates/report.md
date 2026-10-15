---
title: Review of the X integration into Y
author:
  - Main author
  - Co-author
date: 1 January 2026
subject: One line that a PDF viewer shows as the document's subject
keywords: [one, two, three]
lang: en-GB
papersize: a4
numbersections: true
block-headings: true
graphics: true
hyphenation: [look-ahead]
---

<!--
A starter for a technical report or review built with markdown-latex-report.
Copy it, keep the front matter, replace the text. The HTML comments explain
each part and do not reach the PDF. Conventions this file follows:

- One idea per sentence. Short paragraphs: one finding each, under 200 words.
- Verdict and decisive numbers first; findings before evidence; the
  recommendations before the evidence chapters; limits last.
- One name per concept, defined in the terms appendix; the same symbol for the
  same quantity, in math mode ($t_r$, $d_{\mathrm{int}}$).
- A fact carries its citation: `repository/path/file.cpp:118-129` at a commit.
  Cite a file in full once in a parenthesis; inside one sentence the short form
  `:139-149` may follow. Name a directory once and list its files by name
  when a sentence cites several.
- A heading is a plain label of its section's content, with its label:
  `## Served-pose age {#sec:age}`. Refer to a section as
  `Section\ \ref{sec:age} (Served-pose age)`; the backslash-space is a
  no-break space, and a tilde would print literally.
- A figure: `![Caption of at most three sentences.](charts/name.png){#fig:name width=90%}`,
  referred to as `Figure\ \ref{fig:name}`. A table: pipe table, then
  `Table: Caption. {#tbl:name}`, referred to as `Table\ \ref{tbl:name}`.
  Captions say what is plotted, from which data, and what the figure does not
  show; the legend uses the words of the text.
- Numbers: thousands separators (24,436), three significant figures for a
  measurement, the unit after a space (the filter makes it a no-break space),
  the degree sign on both ends of a range (0.05° to 0.08°), the minus sign
  U+2212, exponents in math ($10^{-5}$), "percent" or "%" but not both.
- Nothing from the authoring process in the text: no scratch paths, worker
  names, pass numbers or "now", "no longer", "earlier version".
- Run `make check REPORT=...` and `python3 tools/prose_check.py ...` before
  you call the document finished, and look at the rendered pages it names.
-->

# About this report {#sec:about}

<!-- What the document reviews or reports, at which revisions, and what it is
not. A sentence on who wrote which part. Where the evidence files live. -->

This document reviews ... at the revisions in Table\ \ref{tbl:revisions}. It states the authors' findings, the requirements they accepted and their recommendations; decisions taken afterwards by the project owner are recorded elsewhere and are not tracked here.

| Repository | Baseline | Reviewed revision |
|---|---|---|
| `component-a` | `0123abcd` | `4567ef01` |
| `component-b` | `89ab2345` | `cdef6789` |

Table: Revisions reviewed. {#tbl:revisions}

## What the review did {#sec:did}

- It read ... at the reviewed commits.
- It ran ... and compared ...
- It measured ...

## What the review did not do {#sec:did-not}

- It did not change anything in the repositories.
- It did not implement or test the changes it recommends.

## How statements are qualified {#sec:qualified}

Statements in this document are of three kinds, and the text says which: facts established from source (cited as `repository/path:lines` at a commit) or from measurements; recommendations, written in the imperative; and decisions left to the project owner.

# Summary {#sec:summary}

<!-- The verdict in the first sentence. Then the numbers the verdict rests on,
in a table with their units and their source section. Then one bullet per
finding, each ending with the section that holds its evidence. -->

The integration at the reviewed commits is not fit for ... until the changes listed at the end of this section are made, because ...

| Measured | Configuration A | Configuration B |
|---|---:|---:|
| Served-pose age at the reply, median, ms | 106 | 28 |
| Lag of the served pose while moving, median and maximum, mm | 32.8 and 79.0 | 8.2 and 21.5 |

Table: The measurements this summary uses; Section\ \ref{sec:results} gives the definitions and the complete tables. {#tbl:summary}

- Finding one, in one sentence, with its number (Section\ \ref{sec:finding-one}).
- Finding two (Section\ \ref{sec:finding-two}).

The changes required before ... are: ...

# Results {#sec:results}

<!-- The measurements, with method and limits, before the findings that use
them. Figures sit here; each caption says what the figure does not show. -->

# Finding one {#sec:finding-one}

<!-- One chapter per finding: what the code or the data does, the evidence
with citations, the consequence, the recommendation in the imperative. -->

The function `name` does ... (`repository/path/file.cpp:118-129` at `4567ef01`). In the recordings this ran at each of the 24,436 corrections (Section\ \ref{sec:results}).

Do ... instead; test ... with ...

# Finding two {#sec:finding-two}

# Recommendations and open decisions {#sec:recommendations}

<!-- Grouped by component, imperative; then the decisions the authors leave
to the project owner, each with the authors' recommendation where they have
one and the section that gives the reasons. -->

## Component A {#sec:rec-a}

## Decisions left to the project owner {#sec:rec-decisions}

- Decision D1: the review recommends ... (Section\ \ref{sec:finding-one}); the owner sets ...

# Verification performed {#sec:verification}

<!-- The checks that support the findings: builds, tests, replays, with the
numbers and the files under the evidence directory. -->

# Limits of this review {#sec:limits}

## Limits of the evidence {#sec:limits-evidence}

## Open facts {#sec:limits-open}

## Procedural limits {#sec:limits-procedural}

# Evidence index {.appendix #sec:evidence}

<!-- The first appendix heading carries the class appendix: from here the
headings are lettered A, B, C and figures and tables are numbered A.1, A.2.
The index lists each file the text cites by name, its path and the section
that cites it; `make check` reports cited paths that are missing or untracked. -->

| File | Path | Cited in |
|---|---|---|
| `results.json` | `evidence/analysis/` | \ref{sec:results} |

Table: Files cited by name and their paths. {#tbl:evidence}

# Terms used in this document {.appendix #sec:terms}

<!-- One row per term, in the order a reader meets them. The text uses the
term exactly as spelled here and no synonym for it. -->

::: {.widths-from-dashes}

| Term | Meaning in this document |
|--------------------|------------------------------------------------------------|
| Served-pose age | The reply time minus the served pose's timestamp, on software timestamps. |

:::

Table: Definitions of the terms. {#tbl:terms}
