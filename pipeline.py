"""
pipeline.py

Orchestrates the full run: reads the content strategy workbook,
classifies every page, then for each page:

  - status == 'existing'   -> fetch each source URL, extract, convert,
                               place at the new IA path.
  - status == 'new'        -> write a clearly-marked stub at the new IA
                               path (no source to extract from).
  - status == 'crosslink'  -> skip file creation entirely; logged in the
                               review report only. Not a real standalone
                               page.
  - status == 'needs_review' -> skip file creation; logged in the review
                               report so a human resolves it, rather than
                               guessing.

Produces:
  - one .md file per existing/new page, under output_root, mirroring the
    new IA via descriptive new URL
  - a single review report (JSON) combining content-strategy-level flags
    (blank/ambiguous rows) and extraction-level flags (failed validation,
    fell-through rules, excluded dynamic-feed blocks, etc.) in one place
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

from content_strategy import read_content_strategy, Page
from extract import load_rules, extract_content, fetch_html, FetchError, RuleSet
from convert import build_page_document, build_stub_document
from place import write_page_document
from review_report import write_review_report_json, write_review_report_markdown


def run_pipeline(
    content_strategy_path: str | Path,
    rules_path: str | Path,
    output_root: str | Path,
    fetcher=fetch_html,
    only_page_ids: set[str] | None = None,
) -> tuple[Counter, list[dict]]:
    pages: list[Page] = read_content_strategy(content_strategy_path)
    ruleset: RuleSet = load_rules(rules_path)
    output_root = Path(output_root)

    stats = Counter()
    review_rows: list[dict] = []

    for page in pages:
        if only_page_ids is not None and page.page_id not in only_page_ids:
            continue

        if page.status == "new":
            doc = build_stub_document(page)
            write_page_document(doc, page, output_root)
            stats["stub_written"] += 1
            continue

        if page.status in ("crosslink", "needs_review"):
            review_rows.append({
                "page_id": page.page_id,
                "sheet": page.sheet,
                "page_name": page.page_name,
                "source": "content_strategy",
                "status": page.status,
                "reason": page.review_reason,
            })
            stats[page.status] += 1
            continue

        # status == 'existing'
        results = []
        for url in page.source_urls:
            try:
                status_code, html = fetcher(url)
            except FetchError as e:
                review_rows.append({
                    "page_id": page.page_id,
                    "sheet": page.sheet,
                    "page_name": page.page_name,
                    "source": "extraction",
                    "status": "fetch_failed",
                    "reason": str(e),
                    "url": url,
                })
                continue

            if status_code != 200:
                review_rows.append({
                    "page_id": page.page_id,
                    "sheet": page.sheet,
                    "page_name": page.page_name,
                    "source": "extraction",
                    "status": f"http_{status_code}",
                    "reason": f"Non-200 response fetching {url}",
                    "url": url,
                })
                continue

            result = extract_content(html, url, ruleset)
            results.append(result)

            if result.flags:
                review_rows.append({
                    "page_id": page.page_id,
                    "sheet": page.sheet,
                    "page_name": page.page_name,
                    "source": "extraction",
                    "status": "passed_with_flags" if result.passed_validation else "failed_validation",
                    "reason": "; ".join(result.flags),
                    "url": url,
                })

        if not results:
            stats["no_content_extracted"] += 1
            continue

        doc = build_page_document(page, results)
        write_page_document(doc, page, output_root)
        stats["written"] += 1

    return stats, review_rows


def write_review_report(review_rows: list[dict], output_root: Path) -> Path:
    """Writes BOTH the raw JSON (for scripts / summarize_review.py) and a
    grouped, human-readable markdown version (for anyone who isn't going
    to open a JSON file). Returns the JSON path for backward compatibility
    with existing callers that only cared about one path.
    """
    json_path = write_review_report_json(review_rows, output_root)
    md_path = write_review_report_markdown(review_rows, output_root)
    print(f"Review report written to:\n  {json_path}  (raw data)\n  {md_path}  (human-readable)")
    return json_path


if __name__ == "__main__":
    import argparse
    import yaml

    LOCAL_SETTINGS_FILE = "local_settings.yaml"

    def load_local_settings() -> dict:
        """Optional, machine-specific settings (real file paths) kept
        separate from selector_rules.yaml on purpose -- that file is
        reusable, shareable extraction logic for the target site; this
        one is just "where are my files on my machine", which is
        different for every person running this and shouldn't get mixed
        into version-controlled project config. See local_settings.example.yaml.
        """
        path = Path(LOCAL_SETTINGS_FILE)
        if not path.exists():
            return {}
        with open(path) as f:
            return yaml.safe_load(f) or {}

    def resolve(cli_value, settings: dict, settings_key: str, default, label: str):
        """CLI argument wins if given; otherwise local_settings.yaml;
        otherwise the built-in default. Reports which source it used."""
        if cli_value is not None:
            print(f"{label}: {cli_value} (from command line)")
            return cli_value
        if settings_key in settings:
            print(f"{label}: {settings[settings_key]} (from {LOCAL_SETTINGS_FILE})")
            return settings[settings_key]
        print(f"{label}: {default} (default)")
        return default

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("content_strategy_xlsx", nargs="?", default=None,
                         help="Optional if set in local_settings.yaml instead")
    parser.add_argument("--rules", default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    settings = load_local_settings()

    xlsx_path = resolve(args.content_strategy_xlsx, settings, "content_strategy_xlsx", None, "Workbook")
    rules_path = resolve(args.rules, settings, "rules_file", "selector_rules.yaml", "Rules file")
    out_path = resolve(args.out, settings, "output_dir", "./migrated_content", "Output directory")

    if xlsx_path is None:
        nearby = sorted(Path(".").glob("*.xlsx")) + sorted(Path.home().glob("Downloads/*.xlsx"))
        print(
            "\nNo Content Strategy workbook given. Set one of:\n"
            f"  - content_strategy_xlsx: /full/path/to/workbook.xlsx   in {LOCAL_SETTINGS_FILE}\n"
            "    (copy local_settings.example.yaml to get started)\n"
            "  - or pass it directly: python3 pipeline.py /path/to/workbook.xlsx\n"
        )
        if nearby:
            print(".xlsx files found nearby, in case one of these is it:")
            for p in nearby:
                print(f"  - {p}")
        sys.exit(1)

    if not Path(xlsx_path).exists():
        print(f"\nWorkbook path doesn't exist: {xlsx_path}")
        sys.exit(1)

    stats, review_rows = run_pipeline(xlsx_path, rules_path, out_path)
    report_path = write_review_report(review_rows, Path(out_path))

    print("\nRun complete.")
    print(json.dumps(dict(stats), indent=2))
    print(f"\n{len(review_rows)} rows need review -- see {report_path}")
