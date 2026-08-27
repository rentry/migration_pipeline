"""
place.py

Stage 3 of the pipeline: takes a rendered PageDocument (from convert.py)
and a Page (from content_strategy.py) and writes it to the correct path
in the output tree, using the worksheet's "Descriptive new URL" column to
build the directory structure.

Convention: each URL path segment becomes a directory; the page itself is
written as index.md inside it. The root page ("/") becomes index.md at
the output root. This mirrors how most static-site generators and many
headless CMS import tools expect a folder tree, and keeps the file tree
browsable in a way that matches the new IA -- a reviewer can navigate the
output folder the same way a visitor would navigate the new site.
"""

from __future__ import annotations

from pathlib import Path

from content_strategy import Page
from convert import PageDocument


def target_path(descriptive_new_url: str | None, output_root: Path) -> Path:
    if not descriptive_new_url or descriptive_new_url.strip("/") == "":
        return output_root / "index.md"
    slug = descriptive_new_url.strip("/")
    return output_root / slug / "index.md"


def write_page_document(doc: PageDocument, page: Page, output_root: Path) -> Path:
    path = target_path(page.descriptive_new_url, output_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc.render(), encoding="utf-8")
    return path
