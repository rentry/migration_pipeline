# Content Migration Pipeline — BSU Reference Implementation

## What's here

| File | Stage | Purpose |
|---|---|---|
| `content_strategy.py` | Input parsing | Reads the Content Strategy `.xlsx`, resolves columns by header name (not position), classifies every page as `existing` / `new` / `crosslink` / `needs_review`. |
| `selector_rules.yaml` | Config | Domain/path-matched CSS selector rules for finding "main content" per page template, plus block-classification rules for page-builder components (dynamic feeds, card grids, etc). Fully separate from the client-facing worksheet. |
| `extract.py` | Stage 1 | Fetches a URL, resolves the right selector rule, extracts + cleans content, classifies component blocks, resolves relative image/link URLs, validates the result (word count, link density), converts to markdown. |
| `convert.py` | Stage 2 | Builds a markdown document with YAML frontmatter from one or more extraction results. Handles multi-source pages (content that needs synthesizing from >1 URL) and writes clearly-marked stubs for pages with no existing source. |
| `place.py` | Stage 3 | Writes a converted document to the right path in the output tree, mirroring the new IA via the worksheet's "Descriptive new URL" column. |
| `pipeline.py` | Orchestrator | Ties all of the above together: reads the worksheet, routes every page by status, writes files, produces one combined review report. **This is what you actually run.** Reads workbook/output paths from `local_settings.yaml` if present (see Setup below). |
| `test_extract.py` | Tests | Unit tests for rule resolution, block classification, and URL handling — runs against synthetic fixtures plus the two real HTML samples. No network needed. |
| `test_pipeline.py` | Tests | End-to-end tests: a full-scale run across all 686 real BSU workbook rows (network simulated unavailable) and a small real-content run using the real homepage fixture. No network needed. |
| `summarize_review.py` | Reporting | Groups `_review_report.json` into counts by status and category, so hundreds of rows are scannable before reading anything line by line. |
| `fixtures_real/homepage.html` | Fixture | Real BSU homepage markup — page-builder / block pattern, dynamic feeds. |
| `fixtures_real/residence_halls.html` | Fixture | Real BSU content page — plain prose, real data tables. Saved via Chrome, so its image paths are local artifacts (see note below); kept as-is because it's still a good structural sample. |

## Setup

```bash
pip install -r requirements.txt
```

## Run order

**1. Run the tests first.** `test_extract.py` needs no network and no client files. `test_pipeline.py` needs the real Content Strategy workbook — three ways to point it there, checked in this order:

1. Open `test_pipeline.py` and set `XLSX_PATH` near the top to the real file's path — easiest for regular use, no need to remember a command each time:
   ```python
   XLSX_PATH = "/Users/ryan/Downloads/Bemidji State University_ Content Strategy_active.xlsx"
   ```
   then just run `python3 test_pipeline.py`.
2. Or pass a path on the command line: `python3 test_pipeline.py /path/to/workbook.xlsx`
3. Or drop a copy named `Bemidji_State_University__Content_Strategy_active.xlsx` next to the script.

```bash
python3 test_extract.py
python3 test_pipeline.py
```

If it can't find the workbook, it prints exactly what it tried and any `.xlsx` files it found nearby instead of a traceback — that message will tell you what to fix.

Both should finish with "All assertions passed." If either fails, something about your environment differs from what this was built against (Python version, package version) — worth resolving before trusting a real run.

**2. Try a dry run against a handful of real URLs** before running the full pipeline. This just tells you which selector rule matches each URL and whether it passes validation — it writes nothing:

```bash
python3 extract.py sample_urls.txt --dry-run
```

(`sample_urls.txt` = a plain text file, one URL per line. Not included — make one with a handful of real URLs from the target site.)

**3. Run the full pipeline** against the real Content Strategy workbook. Same idea as the test script — three ways to point it at the workbook, checked in this order:

1. Copy `local_settings.example.yaml` to `local_settings.yaml` and fill in the real path — **this is the one to use for regular real runs**, since it's separate from `selector_rules.yaml` on purpose: that file is reusable extraction logic worth keeping in version control, while `local_settings.yaml` is just "where are my files on my machine" and shouldn't be committed (already covered by `.gitignore`).
   ```yaml
   content_strategy_xlsx: "/Users/ryan/Downloads/Bemidji State University_ Content Strategy_active.xlsx"
   ```
   then just run:
   ```bash
   python3 pipeline.py
   ```
2. Or pass it on the command line, which overrides `local_settings.yaml` if both are present:
   ```bash
   python3 pipeline.py /path/to/workbook.xlsx --out ./migrated_content
   ```

If nothing's configured, it prints exactly what it tried and any `.xlsx` files it found nearby, rather than a traceback.

This will:
- Fetch every page marked `existing` in the workbook, extract, convert, and place it
- Write clearly-marked stub files for every page marked `new`
- Skip file creation for `crosslink` / `needs_review` rows, logging them instead
- Write one combined report to `./migrated_content/_review_report.json` covering both worksheet-level issues (blank/ambiguous rows) and extraction-level issues (failed validation, excluded dynamic content, unresolvable URLs)

**Always check the review report before trusting the output wholesale** — it's the single place both kinds of problems surface. With hundreds of rows, read it through the summarizer first rather than line by line:

```bash
python3 summarize_review.py migrated_content/_review_report.json
```

It groups rows by status and category (fetch failures, worksheet gaps, flagged page-builder blocks, etc.) and only lists individual rows for genuine failures — fetch errors, non-200 responses, failed validation — capped at 20 in the terminal before pointing you at the full JSON.

## Things worth knowing before you test

- **`extract.py` needs real internet access.** It was developed and tested in a sandboxed environment with no access to the live site, so all "real content" testing here used HTML samples pasted/uploaded directly, not live fetches. The logic is sound and tested against real markup, but this is the first time it'll actually hit `bemidjistate.edu` over the network — treat the first real run as a shakeout, and check the review report closely.

- **Only two page templates have been confirmed against real markup**: the homepage (page-builder / block pattern with dynamic feed widgets) and a plain content page with tables (Residence Halls). Program/catalog pages, department pages, and other subdomains (`apply.`, `libguides.`, `bsualumni.org`) are still running on **unconfirmed placeholder selectors** in `selector_rules.yaml` — a real dry run will show you quickly if any of those need adjusting.

- **`selector_rules.yaml` is meant to be edited.** As more page templates turn out to need their own rule (or an existing rule turns out wrong), add/adjust entries there — no code changes needed. Same for `block_rules` if other component types show up beyond the ones already identified (CTA banners, card grids, dynamic feeds).

- **Dynamic content blocks are excluded, not converted.** News feeds, event calendars, and recent-posts widgets get stripped from extracted content and logged in the flags — they're transclusions of content that exists elsewhere, and migrating them as static text would create stale duplicates. If a real run surfaces a component type not already in `block_rules`, it'll just get converted as plain content by default — worth watching for in the review report.

- **The Residence Halls fixture won't round-trip its images correctly** — it was saved via Chrome purely to get real markup for testing, not fetched live, so its image paths are local artifacts that resolve to syntactically valid but nonexistent URLs. This is expected and covered by a test assertion; it's not a bug, and it won't affect real fetches.

- **This is still a reference implementation for one client (BSU).** The header-based worksheet parsing and the general block-classification approach are meant to generalize, but every selector and block-classification rule in `selector_rules.yaml` is BSU-specific and will need a fresh pass for the next project.
