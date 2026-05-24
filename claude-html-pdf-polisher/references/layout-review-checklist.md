# Layout review checklist

Use this checklist after every render. Do not rely only on the contact sheet when a page contains a known risk area.

## Required review loop

1. Render the PDF with `scripts/iterate_layout.sh`.
2. Inspect the contact sheet for global rhythm, inner-panel proportions, section starts, and obvious blank/orphan pages.
3. Open every page PNG, not only the pages likely affected by the edit.
4. Check flagged moments at full page size.
5. Make the smallest HTML/CSS change that addresses the issue and render again.

## Known issues to catch

- Orphaned conversational interjections: keep key interjects atomic with `break-inside: avoid`, `page-break-inside: avoid`, and targeted page-break adjustments.
- Split punchlines: do not split around climactic moments such as a key interject, final pullquote, or other user-identified high-stakes paragraph.
- Bad first words at page top: avoid page starts such as "Too" separated from "actionable", isolated conjunctions, or one-line paragraph fragments.
- Pullquote splits: keep pullquote body, attribution, and coda together unless the user explicitly accepts the split.
- Section marker separation: keep marker, heading, rule, and first lede paragraph visually attached.
- Excessive bottom white holes: tolerate deliberate dramatic gaps only when the user prefers them; otherwise rebalance with margins, quote spacing, or targeted breaks.
- Inner-panel proportions: preserve the requested page-within-page geometry before changing text scale or content.
- Full-bleed special pages: cover and colophon should remain full-bleed when requested.

## Common low-cost controls

Use targeted CSS classes before broad redesigns:

```css
.keep-together {
  break-inside: avoid;
  page-break-inside: avoid;
}

.force-before {
  break-before: page;
  page-break-before: always;
}

.avoid-before {
  break-before: avoid;
  page-break-before: avoid;
}
```

For one-off print-layout fixes, add semantic classes to the relevant block rather than inline random margins everywhere.

## Review language

When reporting back, keep it operational:

- say what changed in layout terms
- say which pages were inspected
- link the PDF, HTML, and contact sheet
- do not narrate failed alternate rendering approaches unless the fixed pipeline itself failed with a concrete new error

## Font preflight

Before judging layout, verify fonts are deterministic:

- No active `<link href="https://fonts.googleapis.com/...">` remains in the working HTML.
- No `fonts.gstatic.com` URL remains in CSS unless it is being intentionally localized in a separate preflight step.
- `document.fonts.ready` is still awaited during Playwright rendering.
- If the final HTML references local font files, return or preserve the required sibling `fonts/` directory in the artifact set when appropriate. Do not expose proprietary or platform font files to the user.
- Do not diagnose font rendering by switching engines. Fix the asset dependency and rerender with Playwright.
