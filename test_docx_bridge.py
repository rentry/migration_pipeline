"""
Exercises docx_export.py + docx_import.py together:
  1. Nested-folder export -- confirms the new IA structure is mirrored as
     real folders, not flattened underscore-joined filenames.
  2. WEBP image handling -- confirms the earlier fix still works.
  3. Full export -> import round trip, including a page nested several
     levels deep, confirming reconstruction lands at the correct nested
     path regardless of how the returned docx was organized.

No network access needed -- images are faked via a mocked requests.get.
"""

import shutil
import sys
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml
from PIL import Image as PILImage

import docx_export
import docx_import

FIXTURE_ROOT = Path("/tmp/docx_bridge_test_ia")
OUT_DIR = Path("/tmp/docx_bridge_test_out")
IMPORT_OUT_DIR = Path("/tmp/docx_bridge_test_import_out")


def make_webp_bytes(color):
    img = PILImage.new("RGB", (100, 100), color=color)
    buf = BytesIO()
    img.save(buf, format="WEBP")
    return buf.getvalue()


def fake_get(url, timeout=10):
    resp = MagicMock()
    resp.raise_for_status = lambda: None
    resp.content = make_webp_bytes((180, 60, 60))
    return resp


def write_fixture_md(path: Path, title: str, new_url: str, with_image: bool = False):
    path.parent.mkdir(parents=True, exist_ok=True)
    image_line = (
        '\n\n![A test photo](https://example.edu/photo.webp)\n' if with_image else ""
    )
    content = f"""---
title: {title}
page_id: '9.9'
source_urls:
- https://example.edu/some-page/
new_url: {new_url}
migration_status: extracted
flags: []
---

Some real body content for {title}, long enough to be meaningful.{image_line}

## A heading

More paragraph text here to round things out.
"""
    path.write_text(content)


def setup_fixture_tree():
    for d in (FIXTURE_ROOT, OUT_DIR, IMPORT_OUT_DIR):
        if d.exists():
            shutil.rmtree(d)
    write_fixture_md(FIXTURE_ROOT / "about" / "our-community" / "index.md",
                      "Our Community", "/about/our-community", with_image=True)
    write_fixture_md(FIXTURE_ROOT / "about" / "governance" / "student-rights" / "index.md",
                      "Student Rights", "/about/governance/student-rights")
    write_fixture_md(FIXTURE_ROOT / "index.md", "Home", "/")


def test_nested_export_structure():
    setup_fixture_tree()
    with patch.object(docx_export.requests, "get", side_effect=fake_get):
        manifest_path = OUT_DIR / "manifest.yaml"
        docx_export.export_batch(FIXTURE_ROOT, OUT_DIR, manifest_path, project="test")

    print("=" * 70)
    print("Files written under OUT_DIR:")
    written = sorted(p.relative_to(OUT_DIR) for p in OUT_DIR.rglob("*.docx"))
    for p in written:
        print(f"  {p}")

    # The whole point: real nested folders mirroring the IA, NOT flattened
    # underscore-joined filenames.
    assert Path("about/our-community/index.docx") in written
    assert Path("about/governance/student-rights/index.docx") in written
    assert Path("index.docx") in written
    # Confirm the OLD flat behavior is gone -- this exact filename should
    # NOT exist anywhere.
    assert not (OUT_DIR / "about_our-community_index.docx").exists()
    assert not (OUT_DIR / "about_governance_student-rights_index.docx").exists()

    print("\nNested export structure confirmed correct.")
    return manifest_path


def test_webp_image_embedded():
    manifest_path = OUT_DIR / "manifest.yaml"
    docx_path = OUT_DIR / "about" / "our-community" / "index.docx"
    assert docx_path.exists()

    from docx import Document
    doc = Document(str(docx_path))
    has_picture = bool(doc.element.body.findall(
        ".//{http://schemas.openxmlformats.org/drawingml/2006/main}blip"
    ))
    body_text = "\n".join(p.text for p in doc.paragraphs)
    print("=" * 70)
    print(f"WEBP image embedded (not a text placeholder): {has_picture}")
    assert has_picture
    assert "UnrecognizedImageError" not in body_text
    print("WEBP handling confirmed still working after the nested-path change.")


def test_full_round_trip_with_nested_path():
    manifest_path = OUT_DIR / "manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text())

    print("=" * 70)
    print("Round-tripping the deeply-nested page with ZERO edits...")
    results = docx_import.import_batch(
        input_path=OUT_DIR / "about" / "governance" / "student-rights" / "index.docx",
        manifest_path=manifest_path,
        out_dir=IMPORT_OUT_DIR,
    )
    for r in results:
        print(" ", r)

    assert len(results) == 1
    result = results[0]
    assert "error" not in result
    assert result["ia_path"] == "about/governance/student-rights/index.md"

    expected_out = IMPORT_OUT_DIR / "about" / "governance" / "student-rights" / "index.md"
    assert expected_out.exists(), f"expected reconstructed file at {expected_out}"
    assert not result["warnings"], f"unexpected warnings: {result['warnings']}"

    reconstructed = expected_out.read_text()
    assert "Student Rights" in reconstructed
    assert "A heading" in reconstructed

    print("\nFull round trip through a nested path confirmed correct.")


def test_import_finds_nested_docx_via_rglob():
    """Confirms find_returned_docx_files still works when pointed at the
    whole nested OUT_DIR tree, not just a single file -- this already
    worked before (rglob), but worth confirming it still finds everything
    now that the tree actually has real subdirectories to walk."""
    found = docx_import.find_returned_docx_files(OUT_DIR)
    print("=" * 70)
    print(f"find_returned_docx_files found {len(found)} files across the nested tree:")
    for f in found:
        print(f"  {f.relative_to(OUT_DIR)}")
    assert len(found) == 3


if __name__ == "__main__":
    test_nested_export_structure()
    test_webp_image_embedded()
    test_full_round_trip_with_nested_path()
    test_import_finds_nested_docx_via_rglob()
    print("\n" + "=" * 70)
    print("All docx bridge assertions passed.")
