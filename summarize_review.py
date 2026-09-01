"""
summarize_review.py

Terminal-friendly summary of an existing _review_report.json -- for a
quick look without opening the file. For the full, readable version with
every row included, see _review_report.md (written automatically by
pipeline.py alongside the JSON).

Usage:
    python3 summarize_review.py migrated_content/_review_report.json
"""

import json
import sys
from collections import Counter

from review_report import categorize_reason


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
    # at 20 printed to the terminal -- past that, open _review_report.md
    # instead, which lists everything.
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
            print(f"  ... and {len(real_failures) - 20} more -- see _review_report.md for the rest.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python3 summarize_review.py <path-to-_review_report.json>")
        sys.exit(1)
    main(sys.argv[1])
