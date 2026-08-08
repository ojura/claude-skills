#!/usr/bin/env python3
"""Build a self-contained HTML decision board from branch review findings."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
from pathlib import Path
from typing import Any


DECISION_SCHEMA_VERSION = 2
DECISIONS = {"undecided", "resolved", "fix", "discuss", "defer", "reject"}
SEVERITY_ALIASES = {
    "critical": "critical",
    "blocker": "critical",
    "high": "high",
    "major": "high",
    "medium-high": "medium-high",
    "medium high": "medium-high",
    "medium_high": "medium-high",
    "medium": "medium",
    "moderate": "medium",
    "low-medium": "low-medium",
    "low medium": "low-medium",
    "low_medium": "low-medium",
    "low": "low",
    "minor": "low",
    "info": "low",
    "informational": "low",
}
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
LINE_PATTERN = re.compile(r"^(.*):(\d+(?:-\d+)?)$")
TOKEN_PATTERN = re.compile(r"@@[A-Z0-9_]+@@")


def slugify(value: str, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or fallback


def display_name(value: str) -> str:
    words = re.sub(r"[-_]+", " ", value).strip().split()
    return " ".join(word.upper() if len(word) <= 3 else word.capitalize() for word in words)


def category_display_name(value: str) -> str:
    if re.fullmatch(r"[a-z0-9_-]+", value):
        return display_name(value)
    return value


def string_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a string or an array of strings")
    result = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{field} must contain only strings")
        if item.strip():
            result.append(item.strip())
    return result


def first_text(record: dict[str, Any], names: tuple[str, ...], default: str = "") -> str:
    for name in names:
        value = record.get(name)
        if value is not None:
            if not isinstance(value, str):
                raise ValueError(f"{name} must be a string")
            if value.strip():
                return value.strip()
    return default


def normalize_location(value: Any) -> dict[str, str]:
    if isinstance(value, str):
        match = LINE_PATTERN.match(value.strip())
        if match:
            return {"file": match.group(1).strip(), "line": match.group(2)}
        if not value.strip():
            raise ValueError("location strings cannot be empty")
        return {"file": value.strip(), "line": ""}

    if not isinstance(value, dict):
        raise ValueError("each location must be a string or object")

    file_name = value.get("file", value.get("path"))
    if not isinstance(file_name, str) or not file_name.strip():
        raise ValueError("each location object needs a non-empty file or path")
    line = value.get("line", value.get("lines", ""))
    if line is None:
        line = ""
    if not isinstance(line, (str, int)):
        raise ValueError("location line must be a string or integer")
    return {"file": file_name.strip(), "line": str(line).strip()}


def normalize_severity(value: Any) -> str:
    if value is None:
        return "medium"
    if not isinstance(value, str):
        raise ValueError("severity must be a string")
    key = value.strip().lower()
    try:
        return SEVERITY_ALIASES[key]
    except KeyError as error:
        allowed = ", ".join(sorted(set(SEVERITY_ALIASES.values())))
        raise ValueError(f"unknown severity {value!r}; expected one of {allowed}") from error


def normalize_finding(record: Any, index: int) -> tuple[dict[str, Any], str, str]:
    if not isinstance(record, dict):
        raise ValueError(f"finding {index + 1} must be an object")

    finding_id = record["id"] if "id" in record else f"F{index + 1:03d}"
    if not isinstance(finding_id, str) or not ID_PATTERN.fullmatch(finding_id):
        raise ValueError(
            f"finding {index + 1} id must match {ID_PATTERN.pattern}; got {finding_id!r}"
        )

    title = first_text(record, ("title", "summary"))
    if not title:
        raise ValueError(f"finding {finding_id} needs title or summary")

    category = first_text(record, ("category", "type"), "uncategorized")
    locations_source = record.get("locations", record.get("location", []))
    if isinstance(locations_source, (str, dict)):
        locations_source = [locations_source]
    if not isinstance(locations_source, list):
        raise ValueError(f"finding {finding_id} locations must be an array")
    locations = [normalize_location(item) for item in locations_source]
    if not locations:
        locations = [{"file": "Location not supplied", "line": ""}]

    reviewers = string_list(
        record.get("agents", record.get("reviewers", record.get("reported_by"))),
        f"finding {finding_id} reviewers",
    )
    if not reviewers:
        reviewers = ["review"]

    description = first_text(
        record,
        ("description", "details"),
        title,
    )
    evidence = first_text(
        record,
        ("evidence", "failure_scenario", "rationale"),
        "No separate evidence was supplied.",
    )
    proposed_resolution = first_text(
        record,
        ("proposed_resolution", "suggested_fix", "resolution"),
        "No proposed resolution was supplied.",
    )

    decision = record.get("decision", "undecided")
    if not isinstance(decision, str) or decision not in DECISIONS:
        raise ValueError(
            f"finding {finding_id} decision must be one of {', '.join(sorted(DECISIONS))}"
        )
    note = record.get("note", "")
    if not isinstance(note, str):
        raise ValueError(f"finding {finding_id} note must be a string")

    finding = {
        "id": finding_id,
        "severity": normalize_severity(record.get("severity")),
        "category": category,
        "title": title,
        "locations": locations,
        "agents": reviewers,
        "description": description,
        "evidence": evidence,
        "proposed_resolution": proposed_resolution,
    }
    return finding, decision, note


def normalize_payload(raw: Any, input_file: Path, title_override: str | None) -> dict[str, Any]:
    if isinstance(raw, list):
        source_object: dict[str, Any] = {"findings": raw}
    elif isinstance(raw, dict):
        source_object = raw
    else:
        raise ValueError("input JSON must be an object or an array of findings")

    raw_findings = source_object.get("findings")
    if not isinstance(raw_findings, list) or not raw_findings:
        raise ValueError("input must contain a non-empty findings array")

    findings: list[dict[str, Any]] = []
    initial_decisions: dict[str, str] = {}
    initial_notes: dict[str, str] = {}
    seen_ids: set[str] = set()
    for index, record in enumerate(raw_findings):
        finding, decision, note = normalize_finding(record, index)
        finding_id = finding["id"]
        if finding_id in seen_ids:
            raise ValueError(f"duplicate finding id {finding_id}")
        seen_ids.add(finding_id)
        findings.append(finding)
        if decision != "undecided":
            initial_decisions[finding_id] = decision
        if note:
            initial_notes[finding_id] = note

    generated_from = string_list(
        source_object.get("generated_from", source_object.get("reviewers")),
        "generated_from",
    )
    if not generated_from:
        generated_from = sorted({reviewer for finding in findings for reviewer in finding["agents"]})

    source = source_object.get("source", input_file.name)
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source must be a non-empty string")
    source = source.strip()

    title = title_override or source_object.get("title") or f"{input_file.stem} findings"
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title must be a non-empty string")
    title = title.strip()

    export_basename = source_object.get("export_basename") or slugify(title, "branch-review")
    if not isinstance(export_basename, str) or not export_basename.strip():
        raise ValueError("export_basename must be a non-empty string")
    export_basename = slugify(export_basename, "branch-review")

    signature_input = json.dumps(
        {"decision_schema_version": DECISION_SCHEMA_VERSION, "findings": findings},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    dataset_signature = hashlib.sha256(signature_input.encode("utf-8")).hexdigest()[:16]

    return {
        "source": source,
        "title": title,
        "export_basename": export_basename,
        "generated_from": generated_from,
        "dataset_signature": dataset_signature,
        "decision_schema_version": DECISION_SCHEMA_VERSION,
        "findings": findings,
        "initial_decisions": initial_decisions,
        "initial_notes": initial_notes,
    }


def render_location(location: dict[str, str]) -> str:
    file_name = html.escape(location["file"], quote=True)
    line = html.escape(location["line"], quote=True)
    title = f"{file_name} line {line}" if line else file_name
    if line:
        body = f"{file_name}:<strong>{line}</strong>"
    else:
        body = file_name
    return f'<code class="location" title="{title}">{body}</code>'


def render_decisions(finding_id: str) -> str:
    labels = (
        ("undecided", "Undecided"),
        ("resolved", "Resolved"),
        ("fix", "Fix"),
        ("discuss", "Discuss"),
        ("defer", "Defer"),
        ("reject", "Reject"),
    )
    controls = []
    for value, label in labels:
        checked = " checked" if value == "undecided" else ""
        controls.append(
            f'<label class="decision-option decision-{value}">'
            f'<input type="radio" name="decision-{finding_id}" value="{value}" '
            f'data-finding-id="{finding_id}"{checked}><span>{label}</span></label>'
        )
    return "".join(controls)


def render_row(finding: dict[str, Any], order: int) -> str:
    finding_id = html.escape(finding["id"], quote=True)
    severity = html.escape(finding["severity"], quote=True)
    category = html.escape(finding["category"], quote=True)
    agents_value = html.escape("|".join(finding["agents"]), quote=True)
    primary_file = html.escape(finding["locations"][0]["file"], quote=True)
    locations = "".join(render_location(item) for item in finding["locations"])
    agent_badges = "".join(
        f'<span class="agent-badge" title="{html.escape(agent, quote=True)}">'
        f'{html.escape(display_name(agent))}</span>'
        for agent in finding["agents"]
    )

    return f'''      <tr class="finding-row" id="finding-{finding_id}"
          data-finding-id="{finding_id}"
          data-severity="{severity}"
          data-category="{category}"
          data-agents="{agents_value}"
          data-file="{primary_file}"
          data-order="{order}"
          data-decision="undecided">
        <th scope="row" class="reference-cell">
          <a class="finding-id" href="#finding-{finding_id}" aria-label="Link to finding {finding_id}">{finding_id}</a>
          <span class="severity-badge severity-{severity}">{html.escape(display_name(finding["severity"]))}</span>
          <span class="category-label">{html.escape(category_display_name(finding["category"]))}</span>
        </th>
        <td class="finding-cell">
          <h2>{html.escape(finding["title"])}</h2>
          <div class="locations" aria-label="Locations">{locations}</div>
          <details class="finding-details">
            <summary>Full description, evidence, and resolution</summary>
            <div class="detail-grid">
              <section>
                <h3>Description</h3>
                <p>{html.escape(finding["description"])}</p>
              </section>
              <section>
                <h3>Evidence</h3>
                <p>{html.escape(finding["evidence"])}</p>
              </section>
              <section class="resolution-panel">
                <h3>Proposed resolution</h3>
                <p>{html.escape(finding["proposed_resolution"])}</p>
              </section>
            </div>
          </details>
        </td>
        <td class="agents-cell">
          <div class="agent-list" aria-label="Reporting reviewers">{agent_badges}</div>
        </td>
        <td class="decision-cell">
          <fieldset>
            <legend class="visually-hidden">Decision for {finding_id}</legend>
            <div class="decision-options">{render_decisions(finding["id"])}</div>
          </fieldset>
        </td>
        <td class="note-cell">
          <label class="visually-hidden" for="note-{finding_id}">Note for {finding_id}</label>
          <textarea id="note-{finding_id}" data-note-id="{finding_id}" rows="2"
                    placeholder="Reason, caveat, owner, or follow-up..." spellcheck="true"></textarea>
          <div class="note-actions"><span class="note-hint" data-state="idle">Not saved yet</span><button class="row-advance" type="button">Save and next</button></div>
        </td>
      </tr>'''


def option_list(values: list[str], display) -> str:
    return "".join(
        f'<option value="{html.escape(value, quote=True)}">{html.escape(display(value))}</option>'
        for value in values
    )


def safe_json_for_script(value: Any) -> str:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return (
        serialized.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace(chr(0x2028), "\\u2028")
        .replace(chr(0x2029), "\\u2029")
    )


def render_board(payload: dict[str, Any], template_file: Path) -> str:
    template = template_file.read_text(encoding="utf-8")
    findings = payload["findings"]
    reviewers = sorted({reviewer for finding in findings for reviewer in finding["agents"]})
    categories = sorted({finding["category"] for finding in findings})
    reviewer_summary = "one reviewer" if len(reviewers) == 1 else f"{len(reviewers)} reviewers"

    replacements = {
        "@@BOARD_TITLE@@": html.escape(payload["title"]),
        "@@FINDING_COUNT@@": str(len(findings)),
        "@@REVIEWER_SUMMARY@@": reviewer_summary,
        "@@CATEGORY_OPTIONS@@": option_list(categories, category_display_name),
        "@@AGENT_OPTIONS@@": option_list(reviewers, display_name),
        "@@FINDING_ROWS@@": "\n\n".join(render_row(item, index) for index, item in enumerate(findings)),
        "@@SOURCE_NAME@@": html.escape(payload["source"]),
        "@@FINDINGS_DATA@@": safe_json_for_script(payload),
    }
    template_tokens = set(TOKEN_PATTERN.findall(template))
    expected_tokens = set(replacements)
    if template_tokens != expected_tokens:
        missing = sorted(expected_tokens - template_tokens)
        unknown = sorted(template_tokens - expected_tokens)
        details = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if unknown:
            details.append(f"unknown: {', '.join(unknown)}")
        raise RuntimeError(f"template token mismatch ({'; '.join(details)})")

    # Substitute in one pass so token-shaped source text is never interpreted as a template token.
    return TOKEN_PATTERN.sub(lambda match: replacements[match.group(0)], template)


def parse_args() -> argparse.Namespace:
    package_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description="Build a self-contained branch review findings decision board."
    )
    parser.add_argument("input", type=Path, help="JSON findings file")
    parser.add_argument("output", type=Path, help="HTML file to write")
    parser.add_argument("--title", help="board title; defaults to input title or filename")
    parser.add_argument(
        "--template",
        type=Path,
        default=package_root / "assets" / "decision-board-template.html",
        help="decision board template",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        raw = json.loads(args.input.read_text(encoding="utf-8"))
        payload = normalize_payload(raw, args.input, args.title)
        board = render_board(payload, args.template)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(board, encoding="utf-8")
    except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as error:
        print(f"build_decision_board: {error}", file=sys.stderr)
        return 1

    print(
        f"wrote {args.output} with {len(payload['findings'])} findings "
        f"(signature {payload['dataset_signature']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
