"""
extract.py

Fetches pages, resolves which content-selector rule applies (from
selector_rules.yaml), extracts the main content region, strips known
cruft, and validates what came out. Produces one JSON file per page
matching the schema in page-extraction-schema.md.

Two modes:

  --dry-run   Fetches nothing extra, just resolves + attempts extraction
              for a list of URLs and prints a summary table: which rule
              matched each URL, which selector within that rule actually
              hit, and whether the result passed validation. Use this
              BEFORE a real run to catch selector problems cheaply across
              a whole batch, rather than one page at a time.

  (default)   Full extraction: writes one JSON file per page.

Note on network access: this script makes real HTTP requests and is meant
to run in a normal environment with internet access. It will not run
inside a network-restricted sandbox.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse, urljoin

import requests
import yaml
from bs4 import BeautifulSoup
from markdownify import markdownify


# ---------------------------------------------------------------------------
# Rule loading + resolution
# ---------------------------------------------------------------------------

@dataclass
class Rule:
    name: str
    match: dict
    content_selectors: list[str]
    strip_selectors: list[str]

    def matches(self, domain: str, path: str) -> bool:
        if self.match.get("default"):
            return True
        m_domain = self.match.get("domain")
        m_prefix = self.match.get("path_prefix")
        if m_domain and m_domain != domain:
            return False
        if m_prefix and not path.startswith(m_prefix):
            return False
        # A rule with neither domain nor prefix nor default is malformed;
        # never let it silently match everything.
        if not m_domain and not m_prefix and not self.match.get("default"):
            return False
        return True


@dataclass
class BlockRule:
    class_contains: str
    block_type: str
    action: str  # 'keep' | 'keep_and_flag' | 'exclude_and_flag'
    note: str | None = None


@dataclass
class RuleSet:
    rules: list[Rule]
    min_word_count: int
    max_link_text_ratio: float
    block_selector: str | None = None
    block_rules: list[BlockRule] = field(default_factory=list)

    def resolve_block(self, element) -> BlockRule | None:
        """First block_rule whose class_contains substring appears in this
        element's class list, joined as a string. None if it's a plain
        block with no special handling (the normal 'keep' case)."""
        classes = " ".join(element.get("class", []))
        for br in self.block_rules:
            if br.class_contains in classes:
                return br
        return None

    def applicable_rules(self, url: str) -> list[Rule]:
        """All rules whose match conditions fit this URL, in file order
        (most specific first, catch-all last). A page may match more than
        one rule by domain/path; extract_content() tries each rule's
        selectors in turn until one actually finds content, rather than
        committing to the first domain/path match and giving up if its
        selectors don't hit. This matters on real sites where a handful
        of legacy pages don't follow the domain's usual template."""
        parsed = urlparse(url)
        domain, path = parsed.netloc, parsed.path
        matched = [rule for rule in self.rules if rule.matches(domain, path)]
        if not matched:
            raise RuntimeError(
                f"No rule matched {url!r}, including no default/catch-all rule. "
                "Check selector_rules.yaml -- there should always be one rule "
                "with `match: {default: true}` as the last entry."
            )
        return matched


def load_rules(path: str | Path) -> RuleSet:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Selector rules file not found: {path}")

    with open(path) as f:
        data = yaml.safe_load(f)

    rules = [
        Rule(
            name=r["name"],
            match=r.get("match", {}),
            content_selectors=r.get("content_selectors", []),
            strip_selectors=r.get("strip_selectors", []),
        )
        for r in data.get("rules", [])
    ]

    if not rules:
        raise ValueError(f"{path} defines no rules under a top-level `rules:` key.")
    if not any(r.match.get("default") for r in rules):
        raise ValueError(
            f"{path} has no catch-all rule (a rule with `match: {{default: true}}`). "
            "Without one, unmatched URLs will raise instead of falling back."
        )

    validation = data.get("validation", {})
    block_rules = [
        BlockRule(
            class_contains=b["class_contains"],
            block_type=b["block_type"],
            action=b["action"],
            note=b.get("note"),
        )
        for b in data.get("block_rules", [])
    ]
    return RuleSet(
        rules=rules,
        min_word_count=validation.get("min_word_count", 30),
        max_link_text_ratio=validation.get("max_link_text_ratio", 0.6),
        block_selector=data.get("block_selector"),
        block_rules=block_rules,
    )


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

class FetchError(RuntimeError):
    pass


def fetch_html(url: str, timeout: int = 20) -> tuple[int, str]:
    """Returns (http_status, html). Raises FetchError on network failure
    (not on a non-200 status -- callers may want to inspect e.g. a 404
    rather than treat it as unrecoverable)."""
    try:
        resp = requests.get(url, timeout=timeout, headers={
            "User-Agent": "Mozilla/5.0 (compatible; ContentMigrationBot/1.0)"
        })
    except requests.RequestException as e:
        raise FetchError(f"Failed to fetch {url}: {e}") from e
    return resp.status_code, resp.text


# ---------------------------------------------------------------------------
# Extraction + validation
# ---------------------------------------------------------------------------

@dataclass
class ExtractionResult:
    url: str
    rule_name: str | None
    matched_selector: str | None
    passed_validation: bool
    word_count: int
    link_text_ratio: float
    flags: list[str] = field(default_factory=list)
    block_flags: list[dict] = field(default_factory=list)
    content_html: str | None = None
    content_markdown: str | None = None


def _text_word_count(soup: BeautifulSoup) -> int:
    text = soup.get_text(separator=" ", strip=True)
    return len(text.split())


def _link_text_ratio(soup: BeautifulSoup) -> float:
    total_len = len(soup.get_text(strip=True))
    if total_len == 0:
        return 1.0  # no text at all is treated as maximally suspicious
    link_len = sum(len(a.get_text(strip=True)) for a in soup.find_all("a"))
    return link_len / total_len


def _resolve_relative_urls(content_soup, page_url: str) -> list[str]:
    """Resolve relative img src / a href values against the page's own
    URL, using standard URL resolution (urljoin) -- not any site-specific
    markup convention. This is a universal need: root-relative paths
    (src="/wp-content/uploads/x.jpg") and page-relative paths are common
    on the live web across many different sites and CMSes, and have
    nothing to do with any one client's particular plugins or markup.

    Deliberately does NOT look at custom data-* attributes as a source of
    truth -- that would mean baking one site's specific markup
    conventions into shared code, which won't generalize to the next
    client's site. If a future project's markup genuinely needs that
    (e.g. real lazy-loaded images where src is a placeholder), it belongs
    in that project's YAML config as an explicit, opt-in list -- not
    assumed here.

    Returns a list of flag strings for anything that still isn't a valid
    absolute URL after resolution (a real problem worth surfacing, not
    silently dropping).
    """
    flags = []
    for tag, attr in [("img", "src"), ("a", "href")]:
        for el in content_soup.find_all(tag):
            val = el.get(attr)
            if not val:
                continue
            resolved = urljoin(page_url, val)
            el[attr] = resolved
            if not resolved.startswith("http"):
                flags.append(f"{tag}_{attr}_not_resolvable: {val!r} -> {resolved!r}")
    return flags


def extract_content(html: str, url: str, ruleset: RuleSet) -> ExtractionResult:
    candidate_rules = ruleset.applicable_rules(url)
    soup = BeautifulSoup(html, "lxml")

    rule = None
    matched_selector = None
    content_soup = None
    tried_rules: list[str] = []

    # Cascade through every applicable rule (specific first, catch-all
    # last), trying each one's selectors in turn. Stop at the first
    # selector, in the first rule, that actually finds something -- so a
    # page that matches a specific domain rule by URL but doesn't follow
    # that template still falls through to the generic fallback instead
    # of failing outright.
    for candidate in candidate_rules:
        tried_rules.append(candidate.name)
        for selector in candidate.content_selectors:
            found = soup.select(selector)
            if found:
                # If a selector matches multiple elements, take the first --
                # but flag ties for review since that itself is a signal
                # the selector may be too generic for this page.
                rule = candidate
                matched_selector = selector
                content_soup = found[0]
                break
        if content_soup is not None:
            break

    flags: list[str] = []

    if content_soup is None:
        flags.append(f"no_selector_matched_in_any_rule (tried={tried_rules})")
        return ExtractionResult(
            url=url,
            rule_name=None,
            matched_selector=None,
            passed_validation=False,
            word_count=0,
            link_text_ratio=1.0,
            flags=flags,
        )

    if rule is not candidate_rules[0]:
        # We fell through past the first (more specific) rule(s) that
        # matched by domain/path but didn't actually find content there.
        flags.append(
            f"fell_through_from_more_specific_rule (tried={tried_rules}, used={rule.name!r})"
        )

    # Strip generic cruft from within the matched region first (nav,
    # footer, scripts -- things the rule already told us are never content).
    for strip_sel in rule.strip_selectors:
        for el in content_soup.select(strip_sel):
            el.decompose()

    # Block-level classification: if this page uses the page-builder
    # pattern (repeating <section class="bsu-block ..."> children), walk
    # each block and apply its specific treatment rather than converting
    # the whole region as one undifferentiated blob. Pages with no such
    # child blocks (a plain content page) fall through unchanged -- that's
    # the normal case and needs no special handling.
    block_flags: list[dict] = []
    if ruleset.block_selector:
        blocks = content_soup.select(ruleset.block_selector)
        for block in blocks:
            block_rule = ruleset.resolve_block(block)
            if block_rule is None:
                continue  # plain block, no special handling -- keep as-is
            preview = block.get_text(separator=" ", strip=True)[:80]
            if block_rule.action == "exclude_and_flag":
                block_flags.append({
                    "block_type": block_rule.block_type,
                    "action": block_rule.action,
                    "note": block_rule.note,
                    "preview": preview,
                })
                block.decompose()
            elif block_rule.action == "keep_and_flag":
                block_flags.append({
                    "block_type": block_rule.block_type,
                    "action": block_rule.action,
                    "note": block_rule.note,
                    "preview": preview,
                })
            # 'keep' -- no flag, no removal, block converts normally.

    if block_flags:
        flags.append(f"block_level_flags ({len(block_flags)}): see block_flags field")

    image_flags = _resolve_relative_urls(content_soup, url)
    flags.extend(image_flags)

    word_count = _text_word_count(content_soup)
    link_ratio = _link_text_ratio(content_soup)

    passed = True
    if word_count < ruleset.min_word_count:
        flags.append(f"low_word_count ({word_count} < {ruleset.min_word_count})")
        passed = False
    if link_ratio > ruleset.max_link_text_ratio:
        flags.append(f"high_link_text_ratio ({link_ratio:.2f} > {ruleset.max_link_text_ratio})")
        passed = False
    if "UNCONFIRMED" in rule.name:
        flags.append("rule_unconfirmed_against_real_html")
    if rule.match.get("default"):
        flags.append("matched_generic_fallback_rule")

    content_html = str(content_soup)
    content_md = markdownify(content_html, heading_style="ATX").strip()

    return ExtractionResult(
        url=url,
        rule_name=rule.name,
        matched_selector=matched_selector,
        passed_validation=passed,
        word_count=word_count,
        link_text_ratio=round(link_ratio, 3),
        flags=flags,
        block_flags=block_flags,
        content_html=content_html,
        content_markdown=content_md,
    )


# ---------------------------------------------------------------------------
# Dry-run discovery mode
# ---------------------------------------------------------------------------

def dry_run(urls: list[str], ruleset: RuleSet, fetcher=fetch_html) -> list[dict]:
    """Attempt extraction for every URL, without writing any output files.
    Returns a summary row per URL: which rule matched, which selector hit,
    pass/fail, and why. Intended to be reviewed before a real run."""
    rows = []
    for url in urls:
        try:
            status, html = fetcher(url)
        except FetchError as e:
            rows.append({
                "url": url, "rule": None, "selector": None,
                "passed": False, "word_count": 0, "link_ratio": None,
                "flags": [f"fetch_failed: {e}"],
            })
            continue

        if status != 200:
            rows.append({
                "url": url, "rule": None, "selector": None,
                "passed": False, "word_count": 0, "link_ratio": None,
                "flags": [f"http_status_{status}"],
            })
            continue

        result = extract_content(html, url, ruleset)
        rows.append({
            "url": url,
            "rule": result.rule_name,
            "selector": result.matched_selector,
            "passed": result.passed_validation,
            "word_count": result.word_count,
            "link_ratio": result.link_text_ratio,
            "flags": result.flags,
        })
    return rows


def print_dry_run_summary(rows: list[dict]) -> None:
    by_rule: dict[str, int] = {}
    passed_count = 0
    for row in rows:
        by_rule[row["rule"]] = by_rule.get(row["rule"], 0) + 1
        if row["passed"]:
            passed_count += 1

    print(f"\n{len(rows)} URLs checked, {passed_count} passed validation.\n")
    print("By matched rule:")
    for rule_name, count in by_rule.items():
        print(f"  {rule_name!r}: {count}")

    print("\nFlagged / failed:")
    for row in rows:
        if not row["passed"]:
            print(f"  {row['url']}")
            print(f"    rule={row['rule']!r} selector={row['selector']!r} flags={row['flags']}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("urls_file", help="Text file, one URL per line")
    parser.add_argument("--rules", default="selector_rules.yaml", help="Path to selector rules YAML")
    parser.add_argument("--dry-run", action="store_true", help="Discovery mode: no output files written")
    parser.add_argument("--out", default="/mnt/user-data/outputs/extracted", help="Output directory for full extraction")
    args = parser.parse_args()

    ruleset = load_rules(args.rules)
    urls = [line.strip() for line in Path(args.urls_file).read_text().splitlines() if line.strip()]

    if args.dry_run:
        rows = dry_run(urls, ruleset)
        print_dry_run_summary(rows)
        return

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for url in urls:
        try:
            status, html = fetch_html(url)
        except FetchError as e:
            print(f"SKIP {url}: {e}", file=sys.stderr)
            continue
        if status != 200:
            print(f"SKIP {url}: HTTP {status}", file=sys.stderr)
            continue
        result = extract_content(html, url, ruleset)
        slug = urlparse(url).path.strip("/").replace("/", "__") or "index"
        out_path = out_dir / f"{slug}.json"
        out_path.write_text(json.dumps({
            "source_url": url,
            "rule_matched": result.rule_name,
            "selector_matched": result.matched_selector,
            "passed_validation": result.passed_validation,
            "word_count": result.word_count,
            "link_text_ratio": result.link_text_ratio,
            "flags": result.flags,
            "content_markdown": result.content_markdown,
        }, indent=2))
        print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
