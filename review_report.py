"""
review_report.py

Shared logic for the migration review report -- both the raw JSON (for
scripts) and a human-readable markdown version (for anyone who isn't
going to read a JSON file: a content strategist, a project manager, a
client). Kept in one place so pipeline.py, summarize_review.py, and the
markdown writer all group rows the same way and never drift apart.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def categorize_reason(row: dict) -> str:
    """Collapse a row into a coarse, human-readable bucket. Checks the
    STRUCTURED status field first (fetch_failed, http_xxx, etc. are exact
    values set by pipeline.py, not text to pattern-match), then falls back
    to scanning the free-text reason for the less-structured cases. Order
    matters within the reason-text checks -- more specific patterns
    checked first."""
    status = (row.get("status") or "")
    reason = row.get("reason")

    if status == "fetch_failed":
        return "Fetch failed (network/DNS/timeout)"
    if status.startswith("http_"):
        return f"Non-200 HTTP response ({status})"
    if status == "failed_validation":
        return "Extraction failed validation (too short or mostly links)"

    if reason is None:
        return "(No reason given)"
    r = reason.lower()
    if "block_level_flags" in r:
        return "Page-builder block flagged (dynamic feed excluded, card grid, etc.)"
    if "fell_through_from_more_specific_rule" in r:
        return "Selector fell through to a more generic rule than expected"
    if "matched_generic_fallback_rule" in r:
        return "Matched the generic fallback rule (no site-specific rule fired)"
    if "unconfirmed" in r:
        return "Matched a rule not yet confirmed against real markup"
    if "not_resolvable" in r:
        return "Image/link URL could not be resolved to absolute"
    if "low_word_count" in r:
        return "Extracted content too short (failed validation)"
    if "high_link_text_ratio" in r:
        return "Extracted content mostly links (failed validation)"
    if "multi_source_synthesis_needed" in r:
        return "Multiple source URLs -- needs manual synthesis"
    if "blank source url" in r:
        return "Worksheet: blank source URL, no explanation"
    if "cross-link" in r or "crosslink" in r:
        return "Worksheet: cross-link reference, not a real page"
    if "department tag" in r:
        return "Worksheet: blank URL but has a department tag"
    return "(Other / uncategorized)"


# Categories that represent something actually broken, surfaced first in
# the markdown report -- everything else is informational (flagged for a
# quick look, but the page was still successfully migrated).
FAILURE_CATEGORIES = {
    "Fetch failed (network/DNS/timeout)",
    "Extraction failed validation (too short or mostly links)",
}


def group_rows(rows: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        # Non-200 HTTP responses get their own dynamic category per status
        # code (e.g. "Non-200 HTTP response (http_404)") from
        # categorize_reason -- still counts as a failure category for
        # sorting purposes here.
        groups[categorize_reason(row)].append(row)
    return dict(groups)


def _is_failure_category(category: str) -> bool:
    return category in FAILURE_CATEGORIES or category.startswith("Non-200 HTTP response")


def write_review_report_json(review_rows: list[dict], output_root: str | Path) -> Path:
    path = Path(output_root) / "_review_report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(review_rows, indent=2))
    return path


def write_review_report_markdown(review_rows: list[dict], output_root: str | Path) -> Path:
    """A report meant to be opened and read, not parsed -- grouped by
    category, failures listed before informational flags, every row
    included (no truncation; this is the actual deliverable, unlike the
    terminal summary in summarize_review.py which caps output for
    readability)."""
    path = Path(output_root) / "_review_report.md"
    path.parent.mkdir(parents=True, exist_ok=True)

    groups = group_rows(review_rows)
    # Failures first, then everything else, each sorted by count descending.
    failure_cats = sorted(
        (c for c in groups if _is_failure_category(c)),
        key=lambda c: -len(groups[c]),
    )
    info_cats = sorted(
        (c for c in groups if not _is_failure_category(c)),
        key=lambda c: -len(groups[c]),
    )
    ordered_cats = failure_cats + info_cats

    lines = [
        "# Migration Review Report",
        "",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"Total flagged rows: {len(review_rows)}",
        "",
        "## Summary",
        "",
        "| Category | Count |",
        "|---|---|",
    ]
    for cat in ordered_cats:
        marker = "⚠️ " if _is_failure_category(cat) else ""
        lines.append(f"| {marker}{cat} | {len(groups[cat])} |")

    lines.append("")
    lines.append("---")

    for cat in ordered_cats:
        rows = groups[cat]
        marker = "⚠️ " if _is_failure_category(cat) else ""
        lines.append("")
        lines.append(f"## {marker}{cat} ({len(rows)})")
        lines.append("")
        for row in rows:
            page_id = row.get("page_id", "?")
            page_name = row.get("page_name", "(unknown)")
            url = row.get("url")
            reason = row.get("reason") or "(no detail)"
            header = f"- **[{page_id}] {page_name}**"
            if url:
                header += f" — {url}"
            lines.append(header)
            lines.append(f"  {reason}")

    path.write_text("\n".join(lines) + "\n")
    return path
