"""
docx_export.py -- prototype (single file)

Converts one migrated markdown file (frontmatter + body) into a client-editable
.docx:

  - Splits frontmatter into pipeline-internal fields vs. client-editable fields
    (title, meta_description).
  - Converts the markdown body to .docx via Pandoc, using Word's built-in
    Heading styles (Pandoc's default docx output already does this).
  - Prepends a clearly-labeled "Page Details" table containing the
    client-editable fields, separated from body content by a divider.
  - Writes a stable UUID into the docx's core-properties `identifier` field --
    this is what drives reassembly later, independent of filename/location.
  - Returns a manifest entry describing everything needed to reverse the
    conversion.

This is a working prototype for a single file, to validate the mechanics
before generalizing to a directory-of-directories walk.
"""

import hashlib
import re
import subprocess
import uuid
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import requests
import yaml
from docx import Document
from docx.image.exceptions import (
    InvalidImageStreamError,
    UnexpectedEndOfFileError,
    UnrecognizedImageError,
)
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from docx.shared import Pt, Inches
from PIL import Image as PILImage

DIVIDER_TEXT = "— Page content begins below. Please do not edit the table above. —"

# Characters illegal in Windows filenames (colon, pipe, angle brackets, etc.)
# -- clients are Microsoft-only, so a docx_filename that's merely valid on
# this Linux sandbox isn't good enough. Real example that surfaced this:
# a page whose new_url was a literal annotation string like
# "Existing subsite: https://libguides.bemidjistate.edu/..." produced a
# folder (and therefore an ia_path) containing a colon.
WINDOWS_ILLEGAL_CHARS_RE = re.compile(r'[<>:"|?*]')


def sanitize_filename(name: str) -> str:
    return WINDOWS_ILLEGAL_CHARS_RE.sub("_", name)


def is_real_ia_page(frontmatter: dict) -> bool:
    """
    A normal page's new_url is a real path in the new IA (e.g.
    "/about/governance/hlc-accreditation"). Some pages instead carry a
    categorization label as new_url -- "TBD", "Library subsite",
    "External link: bsualumni.org/", etc. -- meaning the content
    strategist has flagged this as NOT a page being rebuilt on the new
    site (an external link, an existing subsite staying as-is, or a
    still-undecided destination), not an oversight in the IA.

    place.py currently materializes these labels as literal folder names
    in migrated_content, and extract.py doesn't skip full content
    extraction for them either -- for at least one real case, that meant
    598 lines of an entirely different organization's website got
    extracted and would otherwise be sent to a client as if it were their
    own content to review. Both of those are core-pipeline concerns, out
    of scope here -- but the docx bridge shouldn't put this content in
    front of a client regardless of whether/when that gets fixed upstream.

    Detecting this via "new_url doesn't start with /" is deliberately
    general rather than matching specific label strings like "External
    link:" -- it works for any current or future non-page category
    without hardcoding this client's particular label vocabulary.
    """
    return str(frontmatter.get("new_url", "")).startswith("/")


def parse_markdown_file(path: Path):
    """Split a migrated .md file into (frontmatter_dict, body_str)."""
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---"):
        raise ValueError(f"{path} has no YAML frontmatter block")
    _, fm_block, body = raw.split("---", 2)
    frontmatter = yaml.safe_load(fm_block) or {}
    return frontmatter, body.strip("\n")


def extract_editable_metadata(frontmatter: dict):
    """
    Pull the client-facing fields out of frontmatter.

    Real BSU data note: `meta_description` isn't currently promoted to a
    top-level field by convert.py -- it only lives inside per-source
    `source_metadata` entries, and is frequently null there. We fall back to
    the first non-null value we find, and flag it if nothing is available,
    since that's a real content gap the client review step should surface,
    not silently swallow.
    """
    warnings = []
    title = frontmatter.get("title")

    meta_description = frontmatter.get("meta_description")
    if not meta_description:
        for src in frontmatter.get("source_metadata", []) or []:
            if src.get("meta_description"):
                meta_description = src["meta_description"]
                break

    if not meta_description:
        warnings.append("no meta_description found in source frontmatter or source_metadata")
        meta_description = ""

    return {"title": title or "", "meta_description": meta_description}, warnings


# Matches Wordpress's common "linked thumbnail" pattern -- an image wrapped
# in a link to its own full-resolution version -- BEFORE the plain-image
# pattern, since the plain pattern would otherwise match just the inner
# ![]() and leave a dangling, broken []() around it.
LINKED_IMAGE_RE = re.compile(r"\[!\[([^\]]*)\]\(([^)\s]+)\)\]\(([^)\s]+)\)")
PLAIN_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")

IMAGE_PLACEHOLDER_FMT = "IMAGEPLACEHOLDER{:04d}"  # no underscores: avoids any
# risk of a Markdown parser reading them as emphasis delimiters


def extract_images(body_markdown: str):
    """
    Pull every image reference (plain or linked-thumbnail) out of the body
    BEFORE it ever reaches Pandoc, replacing each with a plain-text
    placeholder token.

    This matters because Pandoc's md->docx->md round trip does NOT preserve
    image URLs -- on the way to docx it fetches and embeds the image as a
    raster picture, and on the way back it extracts that picture back out
    to a local media file, discarding the original URL entirely. For
    "automation-migration-ready markdown," that's a corruption, not a
    conversion. Keeping the canonical original markdown snippet in the
    manifest (see export_page) and re-inserting it verbatim on import
    sidesteps the whole problem rather than trying to make Pandoc's image
    handling round-trip losslessly, which it fundamentally isn't designed
    to do.
    """
    images = []

    def replace_linked(match):
        idx = len(images)
        alt, thumb_url, link_url = match.groups()
        images.append({
            "index": idx,
            "kind": "linked",
            "original_markdown": match.group(0),
            "alt": alt,
            "image_url": thumb_url,
            "link_url": link_url,
        })
        return IMAGE_PLACEHOLDER_FMT.format(idx)

    body_markdown = LINKED_IMAGE_RE.sub(replace_linked, body_markdown)

    def replace_plain(match):
        idx = len(images)
        alt, url = match.groups()
        images.append({
            "index": idx,
            "kind": "plain",
            "original_markdown": match.group(0),
            "alt": alt,
            "image_url": url,
            "link_url": None,
        })
        return IMAGE_PLACEHOLDER_FMT.format(idx)

    body_markdown = PLAIN_IMAGE_RE.sub(replace_plain, body_markdown)

    return body_markdown, images


def _set_monospace(paragraph, bold=False):
    for run in paragraph.runs:
        run.font.name = "Consolas"
        # Word resolves fonts per script; setting rFonts directly ensures
        # Consolas applies even for runs Word would otherwise treat as
        # "complex script" or "east asian" and silently fall back on.
        rPr = run._element.get_or_add_rPr()
        rFonts = rPr.find(qn("w:rFonts"))
        if rFonts is None:
            rFonts = OxmlElement("w:rFonts")
            rPr.append(rFonts)
        for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
            rFonts.set(qn(attr), "Consolas")
        run.font.bold = bold


def _shade_cell(cell, fill_hex="EFEFEF"):
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:fill"), fill_hex)
    tcPr.append(shd)


# python-docx identifies image formats by scanning file headers, and only
# recognizes a fixed set: PNG, JPEG, GIF, BMP, TIFF. It has no WEBP support
# at all -- add_picture() raises UnrecognizedImageError for any format it
# doesn't have a header scanner for (confirmed directly: a real WEBP file
# reproduces this exact exception). BSU's WordPress theme serves WEBP
# broadly, and other clients' sites may use it, AVIF, or something else
# entirely -- so this converts via Pillow ONLY when python-docx actually
# can't ingest the stream as-is, rather than re-encoding every image
# unconditionally. Re-encoding a JPEG or PNG that already works would be
# wasted work and a needless quality hit for the common case.
UNRECOGNIZED_BY_DOCX = (UnrecognizedImageError, InvalidImageStreamError, UnexpectedEndOfFileError)


def _convert_to_png_bytes(image_bytes: bytes) -> BytesIO:
    """Re-encodes arbitrary image bytes to PNG via Pillow, preserving
    transparency where the source actually has an alpha channel (dropping
    it unconditionally would visibly break any image with a transparent
    background)."""
    with PILImage.open(BytesIO(image_bytes)) as im:
        if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
            im = im.convert("RGBA")
        else:
            im = im.convert("RGB")
        out = BytesIO()
        im.save(out, format="PNG")
        out.seek(0)
        return out


def _embed_placeholder_images(doc: Document, images: list):
    """
    Walk the document looking for our placeholder tokens and replace each
    with an actual embedded picture plus a small, visually distinct,
    machine-readable caption underneath, as its own dedicated paragraph
    block.

    IMPORTANT: the token is searched for as a SUBSTRING within a run, not
    matched against the whole paragraph. Wordpress content frequently has
    an image glued directly to the start of a sentence with no separating
    whitespace (a markdownify quirk on the way in) -- an exact-match check
    against the full paragraph text silently skips those and leaves the
    raw placeholder token visibly stuck in the client-facing document,
    which is worse than erroring. Splitting the run turns any inline
    image into its own clean paragraph, which also happens to be a
    reasonable normalization: an image glued wordlessly to prose wasn't
    well-formed to begin with.

    The caption is what makes this reversible: it carries the same index
    the placeholder had, in a fixed, greppable format, so docx_import.py
    can find it regardless of whether the picture itself downloaded
    successfully or was left completely untouched by the client.

    Returns the set of image indices actually found and embedded, so
    export_page can flag any that were never located at all (e.g. Pandoc
    split the token across multiple runs -- not handled here, and worth
    surfacing rather than silently dropping).
    """
    token_re = re.compile(r"IMAGEPLACEHOLDER(\d{4})")
    by_index = {img["index"]: img for img in images}
    found = set()

    for paragraph in list(doc.paragraphs):
        match_run = None
        match = None
        for run in paragraph.runs:
            m = token_re.search(run.text)
            if m:
                match_run, match = run, m
                break
        if match is None:
            continue

        idx = int(match.group(1))
        img = by_index.get(idx)
        if img is None:
            continue
        found.add(idx)

        before = match_run.text[: match.start()]
        after = match_run.text[match.end():]
        match_run.text = before

        current_p = paragraph._p
        parent = current_p.getparent()

        after_p = None
        if after.strip():
            after_p = OxmlElement("w:p")
            current_p.addnext(after_p)
            from docx.text.paragraph import Paragraph
            Paragraph(after_p, paragraph._parent).add_run(after)

        caption_p = OxmlElement("w:p")
        current_p.addnext(caption_p)
        from docx.text.paragraph import Paragraph
        caption_para = Paragraph(caption_p, paragraph._parent)
        caption_text = (
            f"[[IMAGE REF {idx:04d}]] Image reference -- not directly editable; "
            f"the original page reference is preserved automatically. "
            f"Source: {img['image_url']}"
        )
        caption_run = caption_para.add_run(caption_text)
        _set_monospace(caption_para)
        caption_run.font.size = Pt(8)
        caption_run.italic = True

        image_p = OxmlElement("w:p")
        current_p.addnext(image_p)
        image_para = Paragraph(image_p, paragraph._parent)
        try:
            resp = requests.get(img["image_url"], timeout=10)
            resp.raise_for_status()
            image_bytes = resp.content
            try:
                image_para.add_run().add_picture(BytesIO(image_bytes), width=Inches(4))
            except UNRECOGNIZED_BY_DOCX:
                # python-docx couldn't ingest this format directly (WEBP is
                # the common real-world case) -- convert via Pillow and
                # retry once. If Pillow ALSO can't open it, this re-raises
                # and falls through to the outer except below, which
                # degrades to the same text-note fallback as a genuine
                # fetch failure -- never a raw crash either way.
                converted = _convert_to_png_bytes(image_bytes)
                image_para.add_run().add_picture(converted, width=Inches(4))
        except Exception as exc:  # noqa: BLE001 -- fetch failure degrades to a
            # visible text note rather than crashing the whole export.
            note_run = image_para.add_run(
                f"[Image preview unavailable ({exc.__class__.__name__}) -- "
                f"original reference preserved automatically below.]"
            )
            note_run.italic = True

        # If the "before" text was empty, the original paragraph is now
        # genuinely empty -- drop it rather than leaving a stray blank line.
        if not before.strip() and len(paragraph.runs) <= 1:
            parent.remove(current_p)

    return found


def build_docx(body_markdown: str, editable_metadata: dict, images: list, out_path: Path):
    """Pandoc-convert the body, then prepend the editable-metadata table."""
    tmp_md = out_path.with_suffix(".body.md")
    tmp_md.write_text(body_markdown, encoding="utf-8")

    # "gfm" on both ends of the round trip (see docx_import.py) so the
    # dialect stays consistent with the pipe-table markdown the rest of
    # the pipeline already produces and expects.
    subprocess.run(
        ["pandoc", str(tmp_md), "-f", "gfm", "-t", "docx", "-o", str(out_path)],
        check=True,
        capture_output=True,
    )
    tmp_md.unlink()

    doc = Document(str(out_path))

    # Replace image placeholders with real embedded pictures + reversible
    # captions before doing anything else, so their position in the
    # document is whatever Pandoc naturally gave them.
    found_image_indices = _embed_placeholder_images(doc, images)

    # Build the metadata table, then move it (and a divider paragraph) to the
    # very top of the document body. Styled distinctly from body content
    # (monospace + shaded label column) so it reads as a structured field
    # to fill in, not as page prose -- the client should never mistake this
    # for content, or vice versa.
    table = doc.add_table(rows=2, cols=2)
    # "Table" is Pandoc's own built-in default table style (bordered grid) --
    # guaranteed to exist in any Pandoc-generated docx, unlike Word's
    # "Light Grid Accent 1" etc., which only exist if Word itself created
    # the file or a reference-doc defines them.
    table.style = "Table"

    labels = ["Title", "Meta Description"]
    values = [editable_metadata["title"], editable_metadata["meta_description"]]
    for row_idx, (label, value) in enumerate(zip(labels, values)):
        label_cell = table.cell(row_idx, 0)
        value_cell = table.cell(row_idx, 1)
        label_cell.text = label
        value_cell.text = value
        _shade_cell(label_cell)
        _set_monospace(label_cell.paragraphs[0], bold=True)
        _set_monospace(value_cell.paragraphs[0])

    divider = doc.add_paragraph(DIVIDER_TEXT)
    divider.runs[0].italic = True
    divider.runs[0].font.size = Pt(9)

    body = doc.element.body
    tbl_element = table._tbl
    divider_element = divider._p
    body.remove(tbl_element)
    body.remove(divider_element)
    body.insert(0, divider_element)
    body.insert(0, tbl_element)

    doc.save(str(out_path))
    return out_path, found_image_indices


def inject_identifier(docx_path: Path, page_id: str, title: str):
    doc = Document(str(docx_path))
    doc.core_properties.identifier = page_id
    doc.core_properties.title = title
    doc.save(str(docx_path))


def export_page(md_path: Path, ia_root: Path, out_dir: Path):
    frontmatter, body = parse_markdown_file(md_path)
    editable_metadata, warnings = extract_editable_metadata(frontmatter)
    body, images = extract_images(body)

    page_id = str(uuid.uuid4())
    ia_path = md_path.relative_to(ia_root).as_posix()
    # Mirrors the new IA as real nested folders, not a flattened filename
    # (e.g. about/our-community/index.docx, not about_our-community_index.docx)
    # -- easier for a client to navigate a Dropbox/shared-drive delivery the
    # same way they'd navigate the actual new site. Safe to change: import
    # matches by the identifier core-property, never by filename or path
    # (see docx_import.py), and find_returned_docx_files() already walks
    # arbitrarily nested folders on the way back in regardless.
    # sanitize_filename's regex doesn't touch "/", so it's safe to apply to
    # the whole path at once -- each segment gets cleaned independently
    # either way since it's a global substitution.
    docx_filename = sanitize_filename(ia_path.replace(".md", ".docx"))
    out_path = out_dir / docx_filename

    out_path.parent.mkdir(parents=True, exist_ok=True)
    _, found_image_indices = build_docx(body, editable_metadata, images, out_path)
    inject_identifier(out_path, page_id, editable_metadata["title"])

    for img in images:
        if img["index"] not in found_image_indices:
            warnings.append(
                f"image at position {img['index']} (source: {img['image_url']}) "
                f"could not be located in the converted document -- placeholder "
                f"token was likely split across multiple runs by Pandoc"
            )

    source_hash = hashlib.sha256(md_path.read_bytes()).hexdigest()

    # `frontmatter` here retains everything, including the editable fields --
    # editable_metadata is also broken out separately since it's handled
    # differently (compared/merged) on import, not just passed through.
    manifest_entry = {
        "id": page_id,
        "ia_path": ia_path,
        "docx_filename": docx_filename,
        "status": "exported",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "returned_at": None,
        "imported_at": None,
        "source_hash": source_hash,
        "frontmatter": frontmatter,
        "editable_metadata": editable_metadata,
        "images": images,
        "diff_baseline": None,  # filled in by export_batch's self-check round trip
        "validation": {"errors": [], "warnings": warnings},
    }
    return manifest_entry


# Review artifacts and non-content files that live alongside real IA pages
# but should never be sent to a client as if they were content -- same
# exclusion concern noted in the core pipeline (the _review_report.md /
# stub-glob collision).
EXCLUDED_NAMES = {"_review_report.md"}


def find_ia_markdown_files(source_root: Path):
    """Recursively walk an IA tree of arbitrary depth, yielding real content .md files."""
    for path in sorted(source_root.rglob("*.md")):
        if path.name in EXCLUDED_NAMES or path.name.startswith("_"):
            continue
        yield path


def export_batch(source_root: Path, out_dir: Path, manifest_path: Path,
                  project: str = "unnamed-project"):
    """
    Walk source_root (an IA tree, arbitrarily nested -- a "directory of
    directories") and export every real content .md file to a flat batch
    of .docx files in out_dir, alongside one manifest.yaml describing the
    whole batch.

    Immediately after each export, we self-check by running that exact
    docx straight through import_page with zero edits, both to populate
    diff_baseline (the noise-free comparison point used later) and to
    catch any file that fails to round-trip cleanly *now*, at export time,
    rather than discovering it only after the client has already edited
    and returned it.
    """
    # Deferred import to avoid a module-load cycle (docx_import imports
    # DIVIDER_TEXT from this module).
    from docx_import import import_page

    out_dir.mkdir(parents=True, exist_ok=True)
    pages = {}
    excluded_placeholders = []

    for md_path in find_ia_markdown_files(source_root):
        frontmatter, _ = parse_markdown_file(md_path)
        if not is_real_ia_page(frontmatter):
            # Not a page being rebuilt on the new site (external link,
            # existing subsite, still-TBD) -- keep it visible in the
            # manifest for tracking, but never convert or send to a
            # client. See is_real_ia_page's docstring.
            excluded_placeholders.append({
                "ia_path": md_path.relative_to(source_root).as_posix(),
                "new_url": frontmatter.get("new_url"),
                "reason": "new_url is not a real IA path -- not in scope for client review",
            })
            continue

        entry = export_page(md_path, source_root, out_dir)
        pages[entry["id"]] = entry

        docx_path = out_dir / entry["docx_filename"]
        try:
            check = import_page(docx_path, pages, out_dir=Path("/tmp/docx_bridge_selfcheck"))
            entry["diff_baseline"] = check["body_markdown"]
            entry["validation"]["warnings"].extend(check["warnings"])
        except Exception as exc:  # noqa: BLE001 -- deliberately broad: any
            # self-check failure should become a visible flag on this one
            # page, not an uncaught crash that halts the whole batch.
            entry["status"] = "exported_with_errors"
            entry["validation"]["errors"].append(f"self-check round trip failed: {exc}")

    manifest = {
        "batch": {
            "project": project,
            "ia_root": str(source_root),
            "schema_version": 1,
            "exported_at": datetime.now(timezone.utc).isoformat(),
        },
        "pages": pages,
        "excluded_placeholders": excluded_placeholders,
    }
    manifest_path.write_text(
        yaml.dump(manifest, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return manifest


def _load_local_settings(settings_path: Path = Path("local_settings.yaml")) -> dict:
    """
    Matches the flat, top-level-key convention already used by
    local_settings.yaml (content_strategy_xlsx, rules_file, output_dir,
    etc.) rather than introducing a nested sub-section -- keeping the
    docx bridge's settings living in exactly the same file as everything
    else the pipeline reads. All keys are optional and fall back to a
    CLI arg if given, then a sensible default, same as the rest of the
    file's documented convention.
    """
    if not settings_path.exists():
        return {}
    return yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}


def _build_arg_parser():
    import argparse

    settings = _load_local_settings()
    parser = argparse.ArgumentParser(
        description="Export a migrated IA tree of markdown files to client-editable .docx files."
    )
    parser.add_argument(
        "source_root", nargs="?",
        # docx_source_root is a dedicated override; output_dir is the SAME
        # directory the core pipeline already writes migrated_content to,
        # so we default to it rather than asking for a redundant path.
        default=settings.get("docx_source_root") or settings.get("output_dir"),
        help="Root of the migrated_content IA tree (falls back to local_settings.yaml: "
             "docx_source_root, then output_dir)",
    )
    parser.add_argument(
        "--out", default=settings.get("docx_export_dir", "docx_export"),
        help="Directory to write the flat batch of .docx files into (default: docx_export, "
             "or local_settings.yaml: docx_export_dir)",
    )
    parser.add_argument(
        "--manifest", default=settings.get("docx_manifest", "manifest.yaml"),
        help="Path to write manifest.yaml to (default: manifest.yaml, or local_settings.yaml: docx_manifest)",
    )
    parser.add_argument(
        "--project", default=settings.get("docx_project", "unnamed-project"),
        help="Project name recorded in the manifest (default from local_settings.yaml: docx_project)",
    )
    return parser


if __name__ == "__main__":
    args = _build_arg_parser().parse_args()

    if not args.source_root:
        raise SystemExit(
            "No source_root given, and none found in local_settings.yaml under "
            "docx_source_root or output_dir. Usage: python docx_export.py /path/to/migrated_content"
        )

    manifest = export_batch(
        source_root=Path(args.source_root),
        out_dir=Path(args.out),
        manifest_path=Path(args.manifest),
        project=args.project,
    )
    n = len(manifest["pages"])
    excluded = manifest.get("excluded_placeholders", [])
    errors = [(p["ia_path"], p["validation"]["errors"]) for p in manifest["pages"].values() if p["validation"]["errors"]]
    print(f"exported {n} pages, {len(errors)} with self-check errors")
    for ia, e in errors:
        print(" ", ia, e)
    if excluded:
        print(f"{len(excluded)} pages excluded as non-page placeholders (not sent for client review):")
        for p in excluded:
            print(" ", p["ia_path"], "->", p["new_url"])
    print(f"manifest written to {args.manifest}")
