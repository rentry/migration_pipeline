"""
summarize_review.py

The review report is a flat list of every flagged row, from two different
sources (content-strategy-level gaps and per-page extraction flags) mixed
together. Reading it row by row doesn't tell you much about the shape of
the problem. This groups it so you can see what's actually common before
looking at anything individually.

Usage:
    python3 summarize_review.py migrated_content/_review_report.json
"""

import json
import sys
from collections import Counter


def categorize_reason(row: dict) -> str:
    """Collapse a row into a coarse bucket for counting. Checks the
    STRUCTURED status field first (fetch_failed, http_xxx, etc. are exact
    values set by pipeline.py, not text to pattern-match), then falls back
    to scanning the free-text reason for the less-structured cases. Order
    matters within the reason-text checks -- more specific patterns
    checked first."""
    status = (row.get("status") or "")
    reason = row.get("reason")

    if status == "fetch_failed":
        return "fetch failed (network/DNS/timeout)"
    if status.startswith("http_"):
        return f"non-200 HTTP response ({status})"
    if status == "failed_validation":
        return "extraction failed validation (see reason for word count/link ratio)"

    if reason is None:
        return "(no reason given)"
    r = reason.lower()
    if "block_level_flags" in r:
        return "page-builder block flagged (dynamic feed excluded, card grid, etc.)"
    if "fell_through_from_more_specific_rule" in r:
        return "selector fell through to a more generic rule than expected"
    if "matched_generic_fallback_rule" in r:
        return "matched the generic fallback rule (no site-specific rule fired)"
    if "unconfirmed" in r:
        return "matched a rule not yet confirmed against real markup"
    if "not_resolvable" in r:
        return "image/link URL could not be resolved to absolute"
    if "low_word_count" in r:
        return "extracted content too short (failed validation)"
    if "high_link_text_ratio" in r:
        return "extracted content mostly links (failed validation)"
    if "multi_source_synthesis_needed" in r:
        return "multiple source URLs -- needs manual synthesis"
    if "blank source url" in r:
        return "worksheet: blank source URL, no explanation"
    if "cross-link" in r or "crosslink" in r:
        return "worksheet: cross-link reference, not a real page"
    if "department tag" in r:
        return "worksheet: blank URL but has a department tag"
    return "(other / uncategorized)"


def main(path: str):
    rows = json.load(open(path))
    print(f"{len(rows)} total review rows\n")

    by_status = Counter(r.get("status", "(none)") for r in rows)
    print("By status:")
    for status, count in by_status.most_common():
        print(f"  {count:>4}  {status}")

    print("\nBy category:")
    by_category = Counter(categorize_reason(r) for r in rows)
    for category, count in by_category.most_common():
        print(f"  {count:>4}  {category}")

    # Anything that's a REAL failure, not just an informational flag,
    # surfaced separately since that's what needs attention first. Capped
    # at 20 printed to the terminal -- past that it's more useful to open
    # the JSON directly than scroll a wall of text.
    real_failures = [
        r for r in rows
        if r.get("status") in ("fetch_failed", "failed_validation")
        or (r.get("status") or "").startswith("http_")
    ]
    if real_failures:
        print(f"\n{len(real_failures)} rows are actual failures (fetch/HTTP/validation), not just informational flags:")
        for r in real_failures[:20]:
            print(f"  [{r.get('page_id')}] {r.get('page_name')!r}: {r.get('status')} -- {r.get('reason')}")
        if len(real_failures) > 20:
            print(f"  ... and {len(real_failures) - 20} more -- see the full JSON for the rest.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python3 summarize_review.py <path-to-_review_report.json>")
        sys.exit(1)
    main(sys.argv[1])
