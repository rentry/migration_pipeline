"""
docx_import.py -- prototype (single file)

Reverses docx_export.py:

  - Reads the identifier out of the docx's core-properties (NOT the filename
    or folder location -- this is the whole point of the approach).
  - Looks the identifier up in the manifest to find the original ia_path and
    pipeline-internal frontmatter.
  - Pulls the client-editable fields back out of the "Page Details" table,
    then strips that table + the divider paragraph before converting the
    remaining body back to markdown via Pandoc.
  - Merges: pipeline-internal frontmatter passes through untouched;
    editable_metadata is replaced with whatever the client's docx now
    contains (that's the one part of frontmatter we *expect* may change).
  - Writes the reconstructed .md file and reports whether the body changed
    relative to the manifest's diff_baseline.
"""

import copy
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import yaml
from docx import Document

from docx_export import DIVIDER_TEXT, IMAGE_PLACEHOLDER_FMT

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _element_text(element) -> str:
    """
    Join text from <w:t> descendants only.

    NOTE: lxml's built-in .itertext() was observed tripling text on these
    python-docx element classes (likely a property-shadowing quirk in
    python-docx's custom oxml classes, not real duplicated content --
    verified against the raw <w:t> count). Walking <w:t> elements directly
    sidesteps that entirely and is what we actually mean anyway: the
    rendered text, not every element's .text/.tail.
    """
    return "".join(t.text or "" for t in element.findall(f".//{W_NS}t"))


def extract_metadata_table_and_strip(doc: Document):
    """
    Pull Title / Meta Description values off the first table in the doc,
    then remove that table and the divider paragraph that follows it, so
    Pandoc only ever sees actual body content.
    """
    body = doc.element.body
    table = doc.tables[0]

    editable_metadata = {
        "title": table.cell(0, 1).text.strip(),
        "meta_description": table.cell(1, 1).text.strip(),
    }

    # Find and remove the table element plus the very next paragraph
    # (expected to be the divider -- verified by text match; if it doesn't
    # match, we leave it in place rather than guessing and eating real
    # content, and this should surface as a validation warning).
    tbl_element = table._tbl
    parent = tbl_element.getparent()
    siblings = list(parent)
    tbl_index = siblings.index(tbl_element)

    warnings = []
    divider_removed = False
    if tbl_index + 1 < len(siblings):
        next_el = siblings[tbl_index + 1]
        next_text = _element_text(next_el).strip()
        if next_text == DIVIDER_TEXT:
            parent.remove(next_el)
            divider_removed = True
    if not divider_removed:
        warnings.append(
            "divider paragraph not found immediately after metadata table "
            "-- it may have been moved or deleted; check for stray content"
        )

    parent.remove(tbl_element)
    return editable_metadata, warnings


IMAGE_CAPTION_RE = re.compile(r"^\[\[IMAGE REF (\d{4})\]\]")


def extract_and_strip_images(doc: Document):
    """
    Find every image-reference caption we wrote at export time, remove it
    plus whatever precedes it (the embedded picture, or the "preview
    unavailable" fallback note -- either way, presentation-only content
    Pandoc should never see), and drop a bare placeholder paragraph in its
    place so Pandoc's conversion emits the token as plain text at exactly
    that position in the body.

    Returns the set of indices actually found, so callers can flag any
    manifest images that seem to have vanished (deleted by the client, or
    moved somewhere the caption no longer sits right after the picture).
    """
    found_indices = set()
    for paragraph in list(doc.paragraphs):
        m = IMAGE_CAPTION_RE.match(paragraph.text.strip())
        if not m:
            continue
        idx = int(m.group(1))
        found_indices.add(idx)

        caption_el = paragraph._p
        parent = caption_el.getparent()
        siblings = list(parent)
        cap_pos = siblings.index(caption_el)

        # The picture/fallback-note paragraph should be the immediately
        # preceding sibling. Remove it too if present; if it's already
        # gone (client deleted just the picture, left the caption), that's
        # fine -- we still collapse down to a single placeholder.
        if cap_pos > 0:
            prev_el = siblings[cap_pos - 1]
            parent.remove(prev_el)

        placeholder_el = parent.makeelement(caption_el.tag, {})
        parent.replace(caption_el, placeholder_el)
        from docx.text.paragraph import Paragraph
        Paragraph(placeholder_el, paragraph._parent).add_run(
            IMAGE_PLACEHOLDER_FMT.format(idx)
        )

    return found_indices


def restore_original_image_markdown(body_markdown: str, images: list, found_indices: set):
    """
    Swap each placeholder token back out for the EXACT original markdown
    snippet captured at export time -- not a re-derived guess -- so an
    unedited image round-trips byte-identical, and an image the client
    genuinely deleted just doesn't reappear.
    """
    warnings = []
    for img in images:
        token = IMAGE_PLACEHOLDER_FMT.format(img["index"])
        if token in body_markdown:
            body_markdown = body_markdown.replace(token, img["original_markdown"])
        elif img["index"] not in found_indices:
            warnings.append(
                f"image at original position {img['index']} (source: {img['image_url']}) "
                f"appears to have been removed from the returned document"
            )
    return body_markdown, warnings


def docx_to_markdown_body(docx_path: Path) -> str:
    # Pandoc's default "markdown" writer emits grid tables (ASCII boxes),
    # but the migration pipeline's own output (convert.py, via
    # markdownify) uses GFM-style pipe tables. "gfm" as the target keeps
    # returned files in the same dialect the rest of the pipeline already
    # expects, rather than something structurally equivalent but
    # differently shaped.
    result = subprocess.run(
        ["pandoc", str(docx_path), "-f", "docx", "-t", "gfm"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip("\n")


def import_page(docx_path: Path, manifest_pages: dict, out_dir: Path):
    doc = Document(str(docx_path))
    page_id = doc.core_properties.identifier

    if not page_id or page_id not in manifest_pages:
        raise LookupError(
            f"{docx_path.name}: no matching manifest entry for identifier "
            f"{page_id!r} -- falling back to filename match is the next "
            f"step, not implemented in this prototype"
        )

    entry = manifest_pages[page_id]

    editable_metadata, table_warnings = extract_metadata_table_and_strip(doc)
    found_image_indices = extract_and_strip_images(doc)

    stripped_path = docx_path.with_suffix(".stripped.docx")
    doc.save(str(stripped_path))
    body_markdown = docx_to_markdown_body(stripped_path)
    stripped_path.unlink()

    body_markdown, image_warnings = restore_original_image_markdown(
        body_markdown, entry.get("images", []), found_image_indices
    )
    warnings = table_warnings + image_warnings

    merged_frontmatter = copy.deepcopy(entry["frontmatter"])
    merged_frontmatter["title"] = editable_metadata["title"]
    merged_frontmatter["meta_description"] = editable_metadata["meta_description"]

    # Reconstruct the FULL nested IA path here, not just the basename --
    # this is what makes reassembly automatic regardless of where the
    # returned docx physically came from (single stray file, or a whole
    # batch folder). The identifier told us which page this is; ia_path
    # (carried in the manifest since export time) tells us where in the
    # tree it belongs.
    out_path = out_dir / entry["ia_path"]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fm_yaml = yaml.dump(merged_frontmatter, sort_keys=False, allow_unicode=True)
    out_path.write_text(f"---\n{fm_yaml}---\n\n{body_markdown}\n", encoding="utf-8")

    changed = entry["diff_baseline"] is not None and body_markdown != entry["diff_baseline"]

    return {
        "id": page_id,
        "ia_path": entry["ia_path"],
        "out_path": str(out_path),
        "editable_metadata": editable_metadata,
        "body_markdown": body_markdown,
        "changed_vs_baseline": changed,
        "warnings": warnings,
    }


def find_returned_docx_files(input_path: Path):
    """input_path may be a single .docx file or a directory to walk (arbitrary depth --
    we never assume the client preserved any folder structure we sent)."""
    if input_path.is_file():
        return [input_path]
    return sorted(input_path.rglob("*.docx"))


def import_batch(input_path: Path, manifest_path: Path, out_dir: Path):
    """
    Import one docx, or recursively walk a directory of returned docx files
    (however they're organized -- one at a time as they trickle back in, or
    all at once), reassembling each into its correct nested IA location and
    updating the on-disk manifest's status/diff for each page as it's
    processed.
    """
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    pages = manifest["pages"]

    results = []
    for docx_path in find_returned_docx_files(input_path):
        try:
            result = import_page(docx_path, pages, out_dir)
        except LookupError as exc:
            results.append({"file": str(docx_path), "error": str(exc)})
            continue

        entry = pages[result["id"]]
        entry["returned_at"] = entry.get("returned_at") or datetime.now(timezone.utc).isoformat()
        entry["imported_at"] = datetime.now(timezone.utc).isoformat()
        entry["status"] = "flagged" if result["warnings"] else (
            "changed" if result["changed_vs_baseline"] else "validated"
        )
        entry["validation"]["warnings"].extend(result["warnings"])
        results.append(result)

    manifest_path.write_text(
        yaml.dump(manifest, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return results


if __name__ == "__main__":
    results = import_batch(
        input_path=Path("client_returns"),
        manifest_path=Path("manifest.yaml"),
        out_dir=Path("import_out"),
    )
    for r in results:
        if "error" in r:
            print("ERROR:", r["file"], "--", r["error"])
        else:
            print(r["ia_path"], "->", r["out_path"],
                  "| changed:", r["changed_vs_baseline"],
                  "| warnings:", r["warnings"])
