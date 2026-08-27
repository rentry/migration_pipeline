"""
convert.py

Stage 2 of the pipeline: takes one or more ExtractionResult objects (a page
usually has one source URL, but see the "multiple source URLs" case in the
content strategy worksheet) plus the Page's metadata, and produces a single
markdown document with YAML frontmatter.

Kept deliberately simple: this is NOT trying to reproduce the full
block-by-block content_blocks schema from page-extraction-schema.md. It
takes the flat markdown + flags that extract.py already produced per
source URL and combines them, since that's what's needed to actually
write files -- the granular schema stays available as a design reference
if we later need per-block fidelity (e.g. a smarter re-conversion pass).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import yaml

from extract import ExtractionResult
from content_strategy import Page


@dataclass
class PageDocument:
    frontmatter: dict
    body_markdown: str

    def render(self) -> str:
        fm = yaml.safe_dump(self.frontmatter, sort_keys=False, allow_unicode=True)
        return f"---\n{fm}---\n\n{self.body_markdown}\n"


def build_page_document(page: Page, results: list[ExtractionResult]) -> PageDocument:
    """Combine one or more extraction results for a single Page into one
    markdown document. Multiple results happen when the worksheet listed
    more than one source URL for a page (content needs synthesis) --
    handled by concatenating each source's content under a clear divider
    rather than silently merging, so a human reviewer can see exactly
    what came from where and edit accordingly.
    """
    all_flags: list[str] = []
    all_block_flags: list[dict] = []
    sections: list[str] = []

    for i, r in enumerate(results):
        all_flags.extend(f"[{r.url}] {f}" for f in r.flags)
        for bf in r.block_flags:
            all_block_flags.append({**bf, "source_url": r.url})

        if len(results) > 1:
            sections.append(f"<!-- SOURCE {i+1} OF {len(results)}: {r.url} -->\n\n{r.content_markdown or ''}")
        else:
            sections.append(r.content_markdown or "")

    if len(results) > 1:
        all_flags.append(
            f"multi_source_synthesis_needed ({len(results)} sources concatenated below; "
            "needs human editing to merge into one coherent page)"
        )

    body = "\n\n---\n\n".join(sections)

    frontmatter = {
        "title": page.page_name,
        "page_id": page.page_id,
        "source_urls": page.source_urls,
        "new_url": page.descriptive_new_url,
        "extracted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "migration_status": "extracted",
        "flags": all_flags,
    }
    if all_block_flags:
        frontmatter["block_flags"] = all_block_flags

    return PageDocument(frontmatter=frontmatter, body_markdown=body)


def build_stub_document(page: Page) -> PageDocument:
    """For pages classified as 'new' in the content strategy -- no source
    content exists to extract. Rather than silently skipping these pages
    (leaving a gap someone discovers later) or fabricating placeholder
    copy, write a clearly-marked stub that exists at the right path in
    the new IA structure, ready for a copywriter to fill in.
    """
    frontmatter = {
        "title": page.page_name,
        "page_id": page.page_id,
        "source_urls": [],
        "new_url": page.descriptive_new_url,
        "extracted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "migration_status": "needs_new_copy",
        "flags": ["no_existing_source -- this page was marked new in the content strategy worksheet"],
    }
    body = (
        f"<!-- NEEDS NEW COPY -->\n\n"
        f"# {page.page_name}\n\n"
        f"*This page has no existing source content. It was marked as new "
        f"in the content strategy worksheet (sheet: {page.sheet}, page ID: "
        f"{page.page_id}). Replace this stub with drafted copy.*\n"
    )
    return PageDocument(frontmatter=frontmatter, body_markdown=body)
