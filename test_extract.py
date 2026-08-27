"""
Exercises extract.py's rule resolution + validation logic against
synthetic HTML fixtures, standing in for real page samples until we have
actual HTML from the target sites. Uses a fake fetcher so no network
access is required.
"""

from extract import load_rules, extract_content, dry_run, print_dry_run_summary

FIXTURES = {
    # A clean, modern page that should match the "Main site" rule cleanly.
    "https://www.bemidjistate.edu/academics/": """
        <html><body>
        <nav><a href="/about/">About</a><a href="/admissions/">Admissions</a></nav>
        <main id="content">
            <article class="entry-content">
                <h1>Academics</h1>
                <p>At Bemidji State University, students come first. We make every
                effort to ensure that all of our undergraduate, graduate and
                online/distance students have access to the resources, services
                and personal support they need to thrive in school and in life.
                Learn year-round with BSU online and on-campus summer programs.</p>
                <p>Bemidji State University's four-story A.C. Clark Library
                collects, curates and provides access to a wide range of
                resources to campus.</p>
            </article>
        </main>
        <footer>Copyright BSU</footer>
        </body></html>
    """,

    # An old, non-semantic page: everything is divs and tables, no <main>,
    # no <article>. Should fall through to the generic fallback rule and
    # get flagged as unconfirmed / fallback.
    "https://www.bemidjistate.edu/legacy-department-page/": """
        <html><body>
        <table><tr><td class="nav">
            <a href="/x">X</a><a href="/y">Y</a>
        </td><td id="main">
            <h1>Department of Old Things</h1>
            <p>This department offers a rigorous curriculum covering the
            history and practice of old things, with hands-on coursework
            and a capstone thesis requirement in the senior year.</p>
            <p>Students who complete the program go on to careers in
            museums, archives, and historical preservation societies.</p>
        </td></tr></table>
        </body></html>
    """,

    # A page where the only thing resembling "main content" is actually a
    # nav-heavy sidebar -- should FAIL validation on link_text_ratio.
    "https://www.bemidjistate.edu/link-farm/": """
        <html><body>
        <main id="content">
            <ul>
                <li><a href="/a">Link A with some descriptive anchor text</a></li>
                <li><a href="/b">Link B with some descriptive anchor text</a></li>
                <li><a href="/c">Link C with some descriptive anchor text</a></li>
                <li><a href="/d">Link D with some descriptive anchor text</a></li>
            </ul>
        </main>
        </body></html>
    """,

    # A page with basically no content at all -- should fail on word count.
    "https://www.bemidjistate.edu/empty-stub/": """
        <html><body>
        <main id="content"><p>TBD</p></main>
        </body></html>
    """,

    # LibGuides subdomain -- should match the specific LibGuides rule, not
    # the generic main-site rule, even though it's a different domain.
    "https://libguides.bemidjistate.edu/nursing": """
        <html><body>
        <div class="s-lg-nav-guide-side"><a href="/x">Sidebar link</a></div>
        <div id="s-lg-guide-tabs-container">
            <h1>Nursing Research Guide</h1>
            <p>This guide covers key databases, citation tools, and research
            strategies for nursing students at every level, from first-year
            coursework through graduate research projects. It includes
            recommended search strategies for CINAHL and PubMed, guidance
            on evaluating peer-reviewed sources, and citation formatting
            help for APA style, which is standard across the nursing
            program's coursework and thesis requirements.</p>
        </div>
        </body></html>
    """,

    # A domain/path with NO rule at all defined -- must still resolve via
    # the catch-all default rule rather than raising.
    "https://apply.bemidjistate.edu/portal/events": """
        <html><body>
        <div id="main-content">
            <h1>Upcoming Admissions Events</h1>
            <p>Join us for a campus visit, virtual info session, or one of
            our upcoming open house events for prospective students and
            families exploring Bemidji State. Each event includes a
            guided tour of campus, a chance to meet current students and
            faculty, and an overview of financial aid and scholarship
            opportunities available for incoming students.</p>
        </div>
        </body></html>
    """,
}


def fake_fetcher(url: str):
    html = FIXTURES.get(url)
    if html is None:
        raise KeyError(f"No fixture defined for {url}")
    return 200, html


def test_individual_cases():
    ruleset = load_rules("selector_rules.yaml")

    print("=" * 70)
    print("Clean modern page (expect: Main site rule, PASS)")
    r = extract_content(FIXTURES["https://www.bemidjistate.edu/academics/"],
                         "https://www.bemidjistate.edu/academics/", ruleset)
    print(f"  rule={r.rule_name!r} selector={r.matched_selector!r} passed={r.passed_validation} "
          f"words={r.word_count} link_ratio={r.link_text_ratio} flags={r.flags}")
    assert r.passed_validation
    assert "Main site" in r.rule_name

    print("=" * 70)
    print("Legacy table-based page, no semantic tags (expect: generic fallback, flagged)")
    r = extract_content(FIXTURES["https://www.bemidjistate.edu/legacy-department-page/"],
                         "https://www.bemidjistate.edu/legacy-department-page/", ruleset)
    print(f"  rule={r.rule_name!r} selector={r.matched_selector!r} passed={r.passed_validation} "
          f"words={r.word_count} link_ratio={r.link_text_ratio} flags={r.flags}")
    # This page has id="main" -- caught by the generic fallback's #main
    # selector, NOT the "Main site" rule (which looks for main#content /
    # .entry-content / etc, none of which exist here). Confirms fallback
    # behavior works for genuinely non-semantic markup.
    assert r.passed_validation
    assert "matched_generic_fallback_rule" in r.flags

    print("=" * 70)
    print("Nav-heavy false match (expect: FAIL on link_text_ratio)")
    r = extract_content(FIXTURES["https://www.bemidjistate.edu/link-farm/"],
                         "https://www.bemidjistate.edu/link-farm/", ruleset)
    print(f"  rule={r.rule_name!r} selector={r.matched_selector!r} passed={r.passed_validation} "
          f"words={r.word_count} link_ratio={r.link_text_ratio} flags={r.flags}")
    assert not r.passed_validation
    assert any("high_link_text_ratio" in f for f in r.flags)

    print("=" * 70)
    print("Empty stub page (expect: FAIL on word count)")
    r = extract_content(FIXTURES["https://www.bemidjistate.edu/empty-stub/"],
                         "https://www.bemidjistate.edu/empty-stub/", ruleset)
    print(f"  rule={r.rule_name!r} selector={r.matched_selector!r} passed={r.passed_validation} "
          f"words={r.word_count} link_ratio={r.link_text_ratio} flags={r.flags}")
    assert not r.passed_validation
    assert any("low_word_count" in f for f in r.flags)

    print("=" * 70)
    print("LibGuides subdomain (expect: LibGuides-specific rule, not generic)")
    r = extract_content(FIXTURES["https://libguides.bemidjistate.edu/nursing"],
                         "https://libguides.bemidjistate.edu/nursing", ruleset)
    print(f"  rule={r.rule_name!r} selector={r.matched_selector!r} passed={r.passed_validation} "
          f"words={r.word_count} link_ratio={r.link_text_ratio} flags={r.flags}")
    assert r.rule_name == "LibGuides subsite"
    assert r.passed_validation

    print("=" * 70)
    print("Unconfigured subdomain (expect: falls through to generic default, no crash)")
    r = extract_content(FIXTURES["https://apply.bemidjistate.edu/portal/events"],
                         "https://apply.bemidjistate.edu/portal/events", ruleset)
    print(f"  rule={r.rule_name!r} selector={r.matched_selector!r} passed={r.passed_validation} "
          f"words={r.word_count} link_ratio={r.link_text_ratio} flags={r.flags}")
    assert r.rule_name == "Generic fallback"
    assert r.passed_validation

    print("=" * 70)
    print("REAL homepage fixture (expect: confirmed selector, block classification fires)")
    homepage_html = open("fixtures_real/homepage.html").read()
    r = extract_content(homepage_html, "https://www.bemidjistate.edu/", ruleset)
    print(f"  rule={r.rule_name!r} selector={r.matched_selector!r} passed={r.passed_validation} "
          f"words={r.word_count} link_ratio={r.link_text_ratio}")
    print(f"  flags={r.flags}")
    print(f"  block_flags ({len(r.block_flags)}):")
    for bf in r.block_flags:
        print(f"    - {bf['block_type']} / {bf['action']}: {bf['note']} -- {bf['preview']!r}")
    assert r.matched_selector == "main.bsu-body-content"
    assert r.rule_name == "Main site, general template"
    # 3 dynamic feed blocks (news-category-posts, events, news) should be
    # excluded. The hero banner matches "bsu-block-image-side" (more
    # specific than "bsu-content-on-color", checked first) and the card
    # grid matches "bsu-cards" -- both keep_and_flag. 5 flags total.
    excluded = [bf for bf in r.block_flags if bf["action"] == "exclude_and_flag"]
    kept_flagged = [bf for bf in r.block_flags if bf["action"] == "keep_and_flag"]
    assert len(excluded) == 3, f"expected 3 excluded dynamic-feed blocks, got {len(excluded)}"
    assert len(kept_flagged) == 2, f"expected 2 kept-and-flagged blocks (hero + card grid), got {len(kept_flagged)}"
    # Confirm the excluded feed text is actually gone from the markdown,
    # and the real CTA content survived.
    assert "Don" in r.content_markdown and "miss your start" in r.content_markdown
    assert "Explore Our Programs" in r.content_markdown
    assert "Join Social Work Club" not in r.content_markdown  # events feed, excluded
    assert "More Than 400 BSU Students" not in r.content_markdown  # news feed, excluded

    print("=" * 70)
    print("REAL residence-halls fixture (expect: no bsu-block sections, tables + images clean)")
    reshalls_html = open("fixtures_real/residence_halls.html").read()
    r = extract_content(reshalls_html, "https://www.bemidjistate.edu/campus-life/residence-halls/", ruleset)
    print(f"  rule={r.rule_name!r} selector={r.matched_selector!r} passed={r.passed_validation} "
          f"words={r.word_count} link_ratio={r.link_text_ratio}")
    print(f"  flags={r.flags}")
    print(f"  block_flags: {r.block_flags}")
    assert r.matched_selector == "main.bsu-body-content"
    assert r.passed_validation
    assert r.block_flags == []  # plain WYSIWYG page, no page-builder blocks at all
    # Tables should survive as real markdown tables, not get mangled.
    assert "| Residence Hall | Type of Room | Amenities |" in r.content_markdown
    assert "[Oak Hall](https://www.bemidjistate.edu/services/reslife/residence-halls/oak-hall/)" in r.content_markdown
    # Real internal links (already absolute in the source) should be preserved.
    assert "https://www.bemidjistate.edu/services/reslife/residence-halls/oak-hall/" in r.content_markdown
    # This fixture was saved via Chrome's "Webpage, Complete" download,
    # purely to give us a real markup sample -- NOT representative of the
    # real workflow (which fetches live pages directly). Chrome rewrote
    # image src to local relative paths (./Residence Halls_files/...)
    # pointing at files that only exist on the machine that saved the
    # page. Standard URL resolution (urljoin) turns that into a
    # SYNTACTICALLY valid absolute URL -- it has no way to know the
    # resulting path doesn't correspond to a real resource on the live
    # site without an extra HTTP call, which is out of scope for this
    # check. This is a known, acceptable limit: resolving a relative path
    # can't guarantee the resolved URL actually resolves to something
    # real. On a genuine live fetch this isn't a problem, since a real
    # page's relative paths are relative to real site structure.
    first_image_line = next(l for l in r.content_markdown.splitlines() if l.strip().startswith("!["))
    assert "Residence Halls_files" in first_image_line  # confirms the known limitation, not silently hidden
    assert r.flags == []  # no false alarm -- the URL IS syntactically valid, just not a real asset

    print("=" * 70)
    print("Generic relative URL resolution (expect: root-relative and page-relative both resolve via urljoin)")
    relative_url_html = """
        <html><body><main class="bsu-body-content" role="main">
            <p>Padding text so this block clears the minimum word count
            threshold for validation, since the real check here is about
            URL resolution, not content length. More padding follows to
            be safe against the configured minimum word threshold here.</p>
            <img src="/wp-content/uploads/root-relative.jpg" alt="root relative">
            <p><a href="../sibling-page/">page-relative link</a></p>
        </main></body></html>
    """
    r2 = extract_content(relative_url_html, "https://example.edu/some/page/", ruleset)
    print(f"  flags={r2.flags}")
    assert "https://example.edu/wp-content/uploads/root-relative.jpg" in r2.content_markdown
    assert "https://example.edu/some/sibling-page/" in r2.content_markdown
    assert not any("not_resolvable" in f for f in r2.flags)  # URL resolution itself raised no flags

    print("=" * 70)
    print("\nAll assertions passed.\n")


def test_dry_run_mode():
    ruleset = load_rules("selector_rules.yaml")
    rows = dry_run(list(FIXTURES.keys()), ruleset, fetcher=fake_fetcher)
    print_dry_run_summary(rows)


if __name__ == "__main__":
    test_individual_cases()
    print("\n\n########## DRY RUN SUMMARY ##########")
    test_dry_run_mode()
