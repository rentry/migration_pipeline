"""
Two tests:

1. test_full_scale_no_network -- runs the ENTIRE real BSU workbook (686
   pages) through the pipeline with a fetcher that always fails, matching
   this sandbox's actual constraint (no access to bemidjistate.edu). Proves
   the orchestration handles full real-world scale without crashing, and
   that stub/review-report generation work correctly even when zero pages
   can actually be fetched.

2. test_small_real_content -- runs a tiny subset through with a fake
   fetcher that returns the REAL homepage HTML fixture for the real Home
   page's real source URL, proving the full chain (content strategy ->
   extract -> convert -> place) produces a correct file on disk end to end
   for at least one page we have real markup for.
"""

import shutil
import sys
from pathlib import Path

from pipeline import run_pipeline, write_review_report

# ---------------------------------------------------------------------------
# CONFIG -- set this to the real path on your machine and just run the
# script with no arguments. This is the easiest option for regular use.
#
#   XLSX_PATH = "/Users/ryan/Downloads/Bemidji State University_ Content Strategy_active.xlsx"
#
# Leave as None to fall back to (in order): a command-line argument, then
# a file with the default name sitting next to this script.
# ---------------------------------------------------------------------------
XLSX_PATH = None

RULES = "selector_rules.yaml"
DEFAULT_XLSX_NAME = "Bemidji_State_University__Content_Strategy_active.xlsx"


def resolve_xlsx_path() -> Path:
    """Figures out which workbook to use and fails with something actually
    actionable if it can't find one -- not a bare traceback."""
    candidates = []
    if XLSX_PATH:
        candidates.append(("XLSX_PATH set in this script", Path(XLSX_PATH)))
    if len(sys.argv) > 1:
        candidates.append(("command-line argument", Path(sys.argv[1])))
    candidates.append(("default filename next to this script", Path(DEFAULT_XLSX_NAME)))

    for source, path in candidates:
        if path.exists():
            print(f"Using workbook from {source}: {path}")
            return path

    # Nothing worked -- give a clear, complete picture instead of a
    # traceback: what was tried, and what .xlsx files DO exist nearby, in
    # case it's just a name mismatch.
    lines = [
        "",
        "Could not find the Content Strategy workbook. Tried, in order:",
    ]
    for source, path in candidates:
        lines.append(f"  - {source}: {path}  (exists: {path.exists()})")

    nearby = sorted(Path(".").glob("*.xlsx")) + sorted(Path.home().glob("Downloads/*.xlsx"))
    if nearby:
        lines.append("")
        lines.append(".xlsx files found nearby, in case one of these is it:")
        for p in nearby:
            lines.append(f"  - {p}")
    lines.append("")
    lines.append("Easiest fix: open this script and set XLSX_PATH near the top")
    lines.append('   XLSX_PATH = "/full/path/to/your/workbook.xlsx"')
    lines.append("then run again with no arguments.")

    print("\n".join(lines))
    sys.exit(1)


XLSX = resolve_xlsx_path()


def failing_fetcher(url: str):
    from extract import FetchError
    raise FetchError(f"Simulated: no network access to {url} in this sandbox")


def test_full_scale_no_network():
    out = Path("/tmp/pipeline_full_scale")
    if out.exists():
        shutil.rmtree(out)

    stats, review_rows = run_pipeline(XLSX, RULES, out, fetcher=failing_fetcher)
    report_path = write_review_report(review_rows, out)

    print("=" * 70)
    print("FULL SCALE RUN (686 pages, no network)")
    print(dict(stats))
    print(f"review rows: {len(review_rows)}")
    print(f"report written to: {report_path}")

    # Sanity checks against numbers we already confirmed by hand earlier
    # in this project: 15 'new' pages -> stubs written; 4 crosslink + 20
    # needs_review content-strategy-level flags; 647 existing-page rows
    # all fail to fetch (expected, since network is simulated down) and
    # should NOT crash or silently vanish -- each shows up as a
    # fetch_failed review row.
    assert stats["stub_written"] == 15, stats
    assert stats["crosslink"] == 4, stats
    assert stats["needs_review"] == 20, stats
    assert stats["no_content_extracted"] == 647, stats  # every existing page, since fetch always fails

    fetch_failed_rows = [r for r in review_rows if r.get("status") == "fetch_failed"]
    # 647 existing pages, but page 1.2 has 2 source URLs -> 2 fetch attempts,
    # both failing -> 2 rows for that one page.
    assert len(fetch_failed_rows) == 648, len(fetch_failed_rows)

    # Stub files should actually exist on disk.
    stub_files = list(out.rglob("*.md"))
    assert len(stub_files) == 15, f"expected 15 stub .md files, found {len(stub_files)}"

    print("All full-scale assertions passed.")


REAL_HOMEPAGE_URL = "https://www.bemidjistate.edu/"


def fake_fetcher_with_homepage(url: str):
    from extract import FetchError
    if url == REAL_HOMEPAGE_URL:
        html = Path("fixtures_real/homepage.html").read_text()
        return 200, html
    raise FetchError(f"No fixture available for {url} in this small test")


def test_small_real_content():
    out = Path("/tmp/pipeline_small_real")
    if out.exists():
        shutil.rmtree(out)

    # Home page (0.0) has source URL https://www.bemidjistate.edu/ in the
    # real workbook -- confirmed earlier in this project. Limit the run to
    # just that page ID so this test is fast and focused.
    stats, review_rows = run_pipeline(
        XLSX, RULES, out, fetcher=fake_fetcher_with_homepage, only_page_ids={"0.0"}
    )

    print("=" * 70)
    print("SMALL REAL-CONTENT RUN (page 0.0 Home only)")
    print(dict(stats))

    assert stats["written"] == 1, stats
    index_path = out / "index.md"  # descriptive new URL for 0.0 is "/"
    assert index_path.exists(), f"expected {index_path} to exist"

    content = index_path.read_text()
    print("\n--- Written file: index.md ---")
    print(content[:1500])
    print("... (truncated)")

    # Split frontmatter from body -- the excluded feed text legitimately
    # appears in the frontmatter's block_flags preview (so a reviewer can
    # see what was removed and why); it should NOT appear in the actual
    # body content that will ship as the page.
    parts = content.split("---\n", 2)
    body = parts[2] if len(parts) > 2 else content

    # Confirm the real extracted content made it through: hero CTA copy
    # present, excluded dynamic-feed content absent from the BODY,
    # frontmatter has the right page metadata and flags.
    assert "title: Home" in content
    assert "page_id: '0.0'" in content or "page_id: 0.0" in content
    assert "Don" in body and "miss your start" in body
    assert "Explore Our Programs" in body
    assert "Join Social Work Club" not in body  # excluded events feed -- gone from body
    assert "Join Social Work Club" in content  # but visible in block_flags preview for review
    assert "block_flags" in content  # card grid / hero flagged for review

    print("\nAll small real-content assertions passed.")


if __name__ == "__main__":
    test_full_scale_no_network()
    print()
    test_small_real_content()
