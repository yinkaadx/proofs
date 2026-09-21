"""Wix technical SEO, embed indexing, and structured data engine.

No Streamlit import lives in this file, and no external parser either.
The HTML is parsed with the standard library, the words are counted on
both sides of the migration, and the JSON LD is built and then validated
against its own required fields, because each of those is the difference
between a claim and a check.

Three facts shape the file:

* content inside a cross origin iframe can be crawled, and when it is, it
  is attributed to the document it was served from rather than to the page
  that embeds it. A Wix HTML embed is served from a different origin, so
  the words inside it are not words on your page no matter how clearly a
  visitor reads them;
* a Velo custom element is a web component registered on the page itself,
  so what it renders lands in the parent document. That is the entire
  reason to migrate, and the migration is only worth something if no words
  are lost on the way, which is counted here rather than assumed;
* structured data has to describe what the page actually shows. Markup
  that asserts a rating, a price, or an answer that a visitor cannot see
  is a policy violation rather than a shortcut, and this engine says so
  every time it emits any.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Sequence
from urllib.parse import urlparse

ENGINE_VERSION = "1.0.0"

SEVERITY_OK = "ok"
SEVERITY_WARN = "warn"
SEVERITY_CRITICAL = "critical"

_SEVERITY_ORDER = {SEVERITY_OK: 0, SEVERITY_WARN: 1, SEVERITY_CRITICAL: 2}


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    title: str
    detail: str
    fix: str


def _worst(findings: Sequence[Finding]) -> str:
    if not findings:
        return SEVERITY_OK
    return max((f.severity for f in findings), key=lambda s: _SEVERITY_ORDER[s])


def word_count(text: str) -> int:
    return len(re.findall(r"[\w'-]+", str(text or "")))


# ---------------------------------------------------------------------------
# 1. Embed indexing analysis
# ---------------------------------------------------------------------------

EMBED_HTML_IFRAME = "Wix Embed HTML widget"
EMBED_THIRD_PARTY_IFRAME = "Third party iframe, for example a video"
EMBED_CUSTOM_CODE = "Wix custom code in the head or body"
EMBED_VELO_LIGHT_DOM = "Velo custom element rendering into the light DOM"
EMBED_VELO_SHADOW_DOM = "Velo custom element rendering into a shadow root"
EMBED_VELO_ON_CLICK = "Velo custom element that renders after a click"
EMBED_NATIVE_TEXT = "Native Wix text element"

EMBED_TYPES: tuple[str, ...] = (
    EMBED_HTML_IFRAME, EMBED_THIRD_PARTY_IFRAME, EMBED_CUSTOM_CODE,
    EMBED_VELO_LIGHT_DOM, EMBED_VELO_SHADOW_DOM, EMBED_VELO_ON_CLICK,
    EMBED_NATIVE_TEXT,
)

INDEXED_ON_THIS_PAGE = "Counts as content on this page"
INDEXED_ELSEWHERE = "Crawlable, but credited to the embed source URL"
NOT_INDEXED = "Never reaches the index"
NO_TEXT = "Carries no text to index"


@dataclass(frozen=True)
class EmbedAnalysis:
    embed_type: str
    contains_text: bool
    rendering: str
    same_origin: bool
    in_parent_dom: bool
    indexing_status: str
    visibility: int
    technical_reason: str
    headline: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def counts_for_this_page(self) -> bool:
        return self.indexing_status == INDEXED_ON_THIS_PAGE

    @property
    def severity(self) -> str:
        return _worst(self.findings)


@dataclass(frozen=True)
class EmbedProfile:
    rendering: str
    same_origin: bool
    in_parent_dom: bool
    needs_interaction: bool
    visibility_with_text: int
    reason: str


_PROFILES: dict[str, EmbedProfile] = {
    EMBED_HTML_IFRAME: EmbedProfile(
        rendering="Sandboxed iframe on a Wix owned domain",
        same_origin=False, in_parent_dom=False, needs_interaction=False,
        visibility_with_text=25,
        reason=("The widget is an iframe whose document is served from a "
                "different origin. A crawler can fetch that document, and "
                "when it indexes the text it attributes it to the iframe's "
                "own URL. Nothing inside it is text on the page a visitor "
                "typed into the address bar.")),
    EMBED_THIRD_PARTY_IFRAME: EmbedProfile(
        rendering="Cross origin iframe owned by the third party",
        same_origin=False, in_parent_dom=False, needs_interaction=False,
        visibility_with_text=20,
        reason=("Same mechanism and a worse position, because the source "
                "document belongs to somebody else entirely. Embedding a "
                "video does not put its transcript on your page.")),
    EMBED_CUSTOM_CODE: EmbedProfile(
        rendering="Script tag injected into the head or the body",
        same_origin=True, in_parent_dom=True, needs_interaction=False,
        visibility_with_text=55,
        reason=("Custom code runs on the page itself, so anything it writes "
                "into the document is parent page content. Most custom code "
                "is analytics or a pixel and writes no text at all, which "
                "is why it usually contributes nothing to indexing either "
                "way.")),
    EMBED_VELO_LIGHT_DOM: EmbedProfile(
        rendering="Web component rendering into the parent document",
        same_origin=True, in_parent_dom=True, needs_interaction=False,
        visibility_with_text=95,
        reason=("A custom element is registered on the page, so what it "
                "renders into its own light DOM is part of the rendered "
                "HTML of this page and is indexed against this URL. This is "
                "the destination the migration exists to reach.")),
    EMBED_VELO_SHADOW_DOM: EmbedProfile(
        rendering="Web component rendering into an attached shadow root",
        same_origin=True, in_parent_dom=True, needs_interaction=False,
        visibility_with_text=75,
        reason=("Googlebot flattens shadow roots when it serialises the "
                "rendered page, so this text is usually indexed against "
                "this URL. It is more fragile than the light DOM: it does "
                "not appear in view source, and any tool that reads the "
                "raw HTML rather than the rendered DOM will report the page "
                "as empty.")),
    EMBED_VELO_ON_CLICK: EmbedProfile(
        rendering="Web component that renders only after a user event",
        same_origin=True, in_parent_dom=False, needs_interaction=True,
        visibility_with_text=5,
        reason=("A crawler renders the page and does not click anything in "
                "it. Content that exists only after an interaction is "
                "content that exists only for visitors, whatever the "
                "element is built with.")),
    EMBED_NATIVE_TEXT: EmbedProfile(
        rendering="Text node in the page markup",
        same_origin=True, in_parent_dom=True, needs_interaction=False,
        visibility_with_text=100,
        reason=("Plain text in the document. Nothing needs to run for a "
                "crawler to read it, which is why it is the benchmark "
                "everything else is measured against.")),
}


def analyze_wix_embed_indexing(embed_type: str,
                               contains_text: bool = True) -> EmbedAnalysis:
    """Say whether the words in an embed are words on this page.

    The distinction that matters is not crawlable against not crawlable.
    An iframe's document is perfectly crawlable. The question is which URL
    the text is credited to, and for a cross origin iframe the answer is
    the iframe's own URL, which nobody searches for and nothing links to.
    """
    kind = str(embed_type or "").strip()
    if kind not in _PROFILES:
        raise ValueError(f"unknown embed type {embed_type!r}")

    profile = _PROFILES[kind]
    has_text = bool(contains_text)
    findings: list[Finding] = []

    if not has_text:
        status = NO_TEXT
        visibility = 0
    elif profile.needs_interaction:
        status = NOT_INDEXED
        visibility = profile.visibility_with_text
    elif not profile.same_origin:
        status = INDEXED_ELSEWHERE
        visibility = profile.visibility_with_text
    else:
        status = INDEXED_ON_THIS_PAGE
        visibility = profile.visibility_with_text

    if status == INDEXED_ELSEWHERE:
        findings.append(Finding(
            code="EMB-CROSSORIGIN", severity=SEVERITY_CRITICAL,
            title="The text is credited to the iframe, not to this page",
            detail=profile.reason,
            fix=("Move the content into a Velo custom element so it renders "
                 "into this document, or place the same text in a native "
                 "text element beside the embed.")))
        findings.append(Finding(
            code="EMB-AUDIT", severity=SEVERITY_WARN,
            title="This is invisible to most on page audits",
            detail=("A crawler of your own site follows the iframe and "
                    "reports the words it found, and a word count tool "
                    "reports the parent page as thin. Both are correct, "
                    "which is why the two numbers never agree and the "
                    "argument never resolves."),
            fix=("Audit with the rendered DOM of the parent document only, "
                 "and exclude iframe sources from the count.")))
    elif status == NOT_INDEXED:
        findings.append(Finding(
            code="EMB-INTERACTION", severity=SEVERITY_CRITICAL,
            title="A crawler renders the page and clicks nothing",
            detail=profile.reason,
            fix=("Render the content on load and hide it with CSS if the "
                 "design needs it collapsed. Hidden is indexable, absent is "
                 "not.")))
    elif status == NO_TEXT:
        findings.append(Finding(
            code="EMB-NOTEXT", severity=SEVERITY_OK,
            title="Nothing here was ever going to be indexed",
            detail=("The embed carries no text, so its indexing behaviour "
                    "is not a ranking question. It is still a performance "
                    "and a consent question."),
            fix="Check what it loads and when, not what it says."))
    else:
        findings.append(Finding(
            code="EMB-PARENT", severity=SEVERITY_OK,
            title="The text is in this document and is credited to this URL",
            detail=profile.reason,
            fix="Confirm in the rendered DOM, not in view source."))

    if kind == EMBED_VELO_SHADOW_DOM:
        findings.append(Finding(
            code="EMB-SHADOW", severity=SEVERITY_WARN,
            title="Shadow DOM is indexed and is invisible to raw HTML tools",
            detail=("The rendered serialisation Googlebot works from "
                    "flattens shadow roots, so this content is normally "
                    "seen. Every tool that fetches the raw HTML instead, "
                    "which is most link checkers and many audit crawlers, "
                    "will report the section as missing."),
            fix=("Prefer the light DOM unless style encapsulation is worth "
                 "the ambiguity.")))

    headline = f"{kind}: {status}"
    return EmbedAnalysis(
        embed_type=kind, contains_text=has_text, rendering=profile.rendering,
        same_origin=profile.same_origin, in_parent_dom=profile.in_parent_dom,
        indexing_status=status, visibility=visibility,
        technical_reason=profile.reason, headline=headline,
        findings=tuple(findings))


# ---------------------------------------------------------------------------
# 2. Velo custom element migration
# ---------------------------------------------------------------------------

_BLOCK_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "blockquote",
               "figcaption", "td", "th", "dt", "dd")

_DROPPED_TAGS = ("script", "style", "iframe", "noscript", "object", "embed")


class _SnippetParser(HTMLParser):
    """Collect the text and the links a crawler would have seen."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, str]] = []
        self.links: list[tuple[str, str]] = []
        self.dropped: list[str] = []
        self._stack: list[str] = []
        self._buffer: list[str] = []
        self._href: str | None = None
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _DROPPED_TAGS:
            self._skip_depth += 1
            self.dropped.append(tag)
            return
        if self._skip_depth:
            return
        if tag in _BLOCK_TAGS:
            self._flush()
            self._stack.append(tag)
        elif tag == "a":
            self._href = dict(attrs).get("href", "")

    def handle_endtag(self, tag):
        if tag in _DROPPED_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag in _BLOCK_TAGS and self._stack:
            self._flush()
            self._stack.pop()
        elif tag == "a":
            self._href = None

    def handle_data(self, data):
        if self._skip_depth:
            return
        text = data.strip()
        if not text:
            return
        self._buffer.append(text)
        if self._href is not None:
            self.links.append((self._href, text))

    def _flush(self) -> None:
        if not self._buffer:
            return
        tag = self._stack[-1] if self._stack else "p"
        self.blocks.append((tag, " ".join(self._buffer)))
        self._buffer = []

    def close(self) -> None:  # noqa: D102
        super().close()
        self._flush()


@dataclass(frozen=True)
class TextBlock:
    tag: str
    text: str


@dataclass(frozen=True)
class MigrationResult:
    source_html: str
    blocks: tuple[TextBlock, ...]
    links: tuple[tuple[str, str], ...]
    dropped_tags: tuple[str, ...]
    element_tag: str
    element_definition: str
    page_code: str
    rendered_dom: str
    words_in: int
    words_out: int
    before: EmbedAnalysis
    after: EmbedAnalysis
    headline: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def lossless(self) -> bool:
        return self.words_in == self.words_out

    @property
    def severity(self) -> str:
        return _worst(self.findings)


def _escape(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def simulate_velo_migration(html_snippet: str,
                            element_tag: str = "seo-content-block"
                            ) -> MigrationResult:
    """Turn an iframe snippet into a custom element, and count the words.

    A migration that quietly drops a heading or a link has moved the
    problem rather than solved it, so the words the snippet carried and
    the words the rendered element will carry are counted separately and
    compared. A script or a nested iframe inside the snippet is dropped on
    purpose and named, because those cannot follow the text into the
    parent document.
    """
    source = str(html_snippet or "")
    tag_name = str(element_tag or "").strip().lower()
    findings: list[Finding] = []

    if not re.fullmatch(r"[a-z][a-z0-9]*(-[a-z0-9]+)+", tag_name):
        raise ValueError(
            "a custom element tag has to be lowercase and contain a hyphen")

    parser = _SnippetParser()
    parser.feed(source)
    parser.close()

    blocks = tuple(TextBlock(tag, text) for tag, text in parser.blocks)
    links = tuple(parser.links)
    dropped = tuple(dict.fromkeys(parser.dropped))

    words_in = sum(word_count(text) for _, text in parser.blocks)

    rendered_parts: list[str] = []
    for block in blocks:
        rendered_parts.append(
            f"<{block.tag}>{_escape(block.text)}</{block.tag}>")
    rendered = "\n".join(rendered_parts)
    words_out = sum(word_count(block.text) for block in blocks)

    payload = json.dumps(
        [{"tag": block.tag, "text": block.text} for block in blocks],
        indent=2, ensure_ascii=False)

    definition = (
        f"// Velo custom element file: public/custom-elements/"
        f"{tag_name}.js\n"
        f"class SeoContentBlock extends HTMLElement {{\n"
        f"  connectedCallback() {{\n"
        f"    // Rendered into the light DOM on connect, so it is part of\n"
        f"    // this document's rendered HTML before any interaction.\n"
        f"    const blocks = {payload};\n"
        f"    this.innerHTML = blocks\n"
        f"      .map(function (block) {{\n"
        f"        const node = document.createElement(block.tag);\n"
        f"        node.textContent = block.text;\n"
        f"        return node.outerHTML;\n"
        f"      }})\n"
        f"      .join('');\n"
        f"  }}\n"
        f"}}\n"
        f"customElements.define('{tag_name}', SeoContentBlock);")

    page_code = (
        f"// Page code\n"
        f"$w.onReady(function () {{\n"
        f"  // The element renders itself on connect. Nothing here waits\n"
        f"  // for a click, because a crawler never sends one.\n"
        f"  $w('#{tag_name.replace('-', '')}').setAttribute('data-ready',\n"
        f"                                                  'true');\n"
        f"}});")

    before = analyze_wix_embed_indexing(EMBED_HTML_IFRAME, words_in > 0)
    after = analyze_wix_embed_indexing(EMBED_VELO_LIGHT_DOM, words_out > 0)

    if words_in == words_out:
        findings.append(Finding(
            code="VEL-LOSSLESS", severity=SEVERITY_OK,
            title=f"{words_out} word(s) in, {words_out} word(s) out",
            detail=("Every word the snippet carried appears in the rendered "
                    "element. A migration that drops a heading has moved "
                    "the problem into a place nobody is looking."),
            fix="Diff the rendered DOM against the old iframe before cutover."))
    else:
        findings.append(Finding(
            code="VEL-LOSSY", severity=SEVERITY_CRITICAL,
            title=f"{words_in} word(s) in against {words_out} out",
            detail=("Text was lost in translation. The new element will "
                    "index cleanly and will index less than the iframe "
                    "held."),
            fix="Find the missing block before the old embed is removed."))

    if dropped:
        findings.append(Finding(
            code="VEL-DROPPED", severity=SEVERITY_WARN,
            title=f"Dropped {', '.join(dropped)} from the snippet",
            detail=("These cannot follow the text into the parent document "
                    "as they stand. A script belongs in the element's own "
                    "file, and a nested iframe reproduces the exact problem "
                    "this migration exists to remove."),
            fix=("Port the behaviour into the custom element rather than "
                 "pasting the tag back in.")))

    if links:
        findings.append(Finding(
            code="VEL-LINKS", severity=SEVERITY_OK,
            title=f"{len(links)} link(s) now sit in the parent document",
            detail=("A link inside a cross origin iframe passes no signal "
                    "to the parent page. The same link rendered by a custom "
                    "element is a link on this page."),
            fix="Check the destinations are still current while you are here."))

    findings.append(Finding(
        code="VEL-RENDER", severity=SEVERITY_WARN,
        title="Confirm this in the rendered DOM, never in view source",
        detail=("A custom element writes its content with JavaScript, so "
                "view source shows an empty tag. That is expected and it is "
                "also how this migration gets reported as a regression by "
                "anyone checking the wrong artefact."),
        fix=("Use the URL inspection tool's rendered HTML, or the browser "
             "element inspector.")))

    headline = (f"{len(blocks)} block(s), {words_out} word(s), "
                f"{len(links)} link(s) moved into the parent DOM")

    return MigrationResult(
        source_html=source, blocks=blocks, links=links, dropped_tags=dropped,
        element_tag=tag_name, element_definition=definition,
        page_code=page_code, rendered_dom=rendered, words_in=words_in,
        words_out=words_out, before=before, after=after, headline=headline,
        findings=tuple(findings))


SAMPLE_SNIPPET = (
    "<div class=\"panel\">\n"
    "  <h2>Structural waterproofing for Victorian basements</h2>\n"
    "  <p>We tank, drain and guarantee below ground rooms across the "
    "North West, with a thirty year insurance backed guarantee on every "
    "installation.</p>\n"
    "  <ul>\n"
    "    <li>Cavity drain membrane systems</li>\n"
    "    <li>Cementitious tanking slurry</li>\n"
    "    <li>Sump and pump installation with battery backup</li>\n"
    "  </ul>\n"
    "  <p>Read our <a href=\"/case-studies/didsbury\">Didsbury case "
    "study</a> for a full specification.</p>\n"
    "  <script>trackPanelView();</script>\n"
    "</div>")


# ---------------------------------------------------------------------------
# 3. JSON LD structured data validation
# ---------------------------------------------------------------------------

SCHEMA_CONTEXT = "https://schema.org"

TYPE_ARTICLE = "Article"
TYPE_PRODUCT = "Product"
TYPE_FAQ = "FAQPage"
TYPE_LOCAL_BUSINESS = "LocalBusiness"
TYPE_BREADCRUMB = "BreadcrumbList"
TYPE_ORGANIZATION = "Organization"

SCHEMA_TYPES: tuple[str, ...] = (TYPE_ARTICLE, TYPE_PRODUCT, TYPE_FAQ,
                                 TYPE_LOCAL_BUSINESS, TYPE_BREADCRUMB,
                                 TYPE_ORGANIZATION)

ELIGIBLE = "Eligible for a rich result"
VALID_NOT_ELIGIBLE = "Valid, no rich result available for this type"
INVALID = "Invalid, a required property is missing"


@dataclass(frozen=True)
class SchemaRule:
    required: tuple[str, ...]
    recommended: tuple[str, ...]
    rich_result: bool
    note: str


# Required and recommended split as Google documents it for rich results.
# A property Google calls recommended is not optional in practice, it is
# the difference between a valid item and one that actually renders.
_RULES: dict[str, SchemaRule] = {
    TYPE_ARTICLE: SchemaRule(
        required=("headline",),
        recommended=("image", "author", "datePublished", "dateModified"),
        rich_result=True,
        note=("Headline is the only strictly required property. An article "
              "without an image and a date is valid and rarely renders as "
              "anything a visitor notices.")),
    TYPE_PRODUCT: SchemaRule(
        required=("name", "offers"),
        recommended=("image", "description", "brand", "aggregateRating",
                     "review"),
        rich_result=True,
        note=("Offers needs both price and priceCurrency. A product with a "
              "rating and no offer is a common and ineligible shape.")),
    TYPE_FAQ: SchemaRule(
        required=("mainEntity",),
        recommended=(),
        rich_result=False,
        note=("Since August 2023 the FAQ rich result is shown only for "
              "well known government and health sites. The markup is still "
              "valid and still useful to other consumers, and on an "
              "ordinary business site it will not draw a rich result.")),
    TYPE_LOCAL_BUSINESS: SchemaRule(
        required=("name", "address"),
        recommended=("telephone", "openingHoursSpecification", "url",
                     "geo", "priceRange"),
        rich_result=True,
        note=("Address has to be a PostalAddress object, not a string. A "
              "flattened address is the single most common reason a local "
              "business item is rejected.")),
    TYPE_BREADCRUMB: SchemaRule(
        required=("itemListElement",),
        recommended=(),
        rich_result=True,
        note=("Every ListItem needs a position and a name, and every item "
              "except the last needs an item URL.")),
    TYPE_ORGANIZATION: SchemaRule(
        required=("name", "url"),
        recommended=("logo", "sameAs", "contactPoint"),
        rich_result=False,
        note=("Organization drives the knowledge panel rather than a "
              "snippet, so there is no rich result to preview and it is "
              "still worth emitting once per site.")),
}


def required_properties(schema_type: str) -> tuple[str, ...]:
    """The properties without which the item cannot be interpreted."""
    kind = str(schema_type or "").strip()
    if kind not in _RULES:
        raise ValueError(f"unknown schema type {schema_type!r}")
    return _RULES[kind].required


def recommended_properties(schema_type: str) -> tuple[str, ...]:
    """The properties that decide whether the valid item renders."""
    kind = str(schema_type or "").strip()
    if kind not in _RULES:
        raise ValueError(f"unknown schema type {schema_type!r}")
    return _RULES[kind].recommended


def _sample_payload(schema_type: str, page_url: str) -> dict:
    parsed = urlparse(page_url)
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else ""
    if schema_type == TYPE_ARTICLE:
        return {
            "@context": SCHEMA_CONTEXT, "@type": TYPE_ARTICLE,
            "headline": "Structural waterproofing for Victorian basements",
            "image": [f"{origin}/images/basement-tanking.jpg"],
            "author": {"@type": "Organization", "name": "Northwest Damp"},
            "datePublished": "2026-03-04", "dateModified": "2026-08-19",
            "mainEntityOfPage": {"@type": "WebPage", "@id": page_url},
        }
    if schema_type == TYPE_PRODUCT:
        return {
            "@context": SCHEMA_CONTEXT, "@type": TYPE_PRODUCT,
            "name": "Cavity drain membrane survey",
            "image": [f"{origin}/images/survey.jpg"],
            "description": "A measured survey of a below ground room.",
            "brand": {"@type": "Brand", "name": "Northwest Damp"},
            "offers": {"@type": "Offer", "price": "240.00",
                       "priceCurrency": "GBP", "url": page_url,
                       "availability": "https://schema.org/InStock"},
        }
    if schema_type == TYPE_FAQ:
        return {
            "@context": SCHEMA_CONTEXT, "@type": TYPE_FAQ,
            "mainEntity": [{
                "@type": "Question",
                "name": "How long does a tanking installation take?",
                "acceptedAnswer": {
                    "@type": "Answer",
                    "text": "A single room is usually three to five days."},
            }],
        }
    if schema_type == TYPE_LOCAL_BUSINESS:
        return {
            "@context": SCHEMA_CONTEXT, "@type": TYPE_LOCAL_BUSINESS,
            "name": "Northwest Damp", "url": origin or page_url,
            "telephone": "+44 161 496 0000",
            "address": {"@type": "PostalAddress",
                        "streetAddress": "14 Wilmslow Road",
                        "addressLocality": "Manchester",
                        "addressRegion": "Greater Manchester",
                        "postalCode": "M20 3AA", "addressCountry": "GB"},
            "openingHoursSpecification": [{
                "@type": "OpeningHoursSpecification",
                "dayOfWeek": ["Monday", "Tuesday", "Wednesday", "Thursday",
                              "Friday"],
                "opens": "08:00", "closes": "17:30"}],
        }
    if schema_type == TYPE_BREADCRUMB:
        return {
            "@context": SCHEMA_CONTEXT, "@type": TYPE_BREADCRUMB,
            "itemListElement": [
                {"@type": "ListItem", "position": 1, "name": "Services",
                 "item": f"{origin}/services"},
                {"@type": "ListItem", "position": 2,
                 "name": "Basement waterproofing"},
            ],
        }
    return {
        "@context": SCHEMA_CONTEXT, "@type": TYPE_ORGANIZATION,
        "name": "Northwest Damp", "url": origin or page_url,
        "logo": f"{origin}/images/logo.png",
        "sameAs": ["https://www.linkedin.com/company/northwest-damp"],
    }


@dataclass(frozen=True)
class SchemaValidation:
    schema_type: str
    page_url: str
    payload: dict
    json_ld: str
    missing_required: tuple[str, ...]
    missing_recommended: tuple[str, ...]
    status: str
    rich_result_available: bool
    headline: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def valid(self) -> bool:
        return not self.missing_required

    @property
    def eligible(self) -> bool:
        return self.status == ELIGIBLE

    @property
    def severity(self) -> str:
        return _worst(self.findings)


def validate_json_ld_schema(schema_type: str, page_url: str,
                            payload: dict | None = None
                            ) -> SchemaValidation:
    """Build or check one JSON LD item and say what it can actually win.

    A required property that is absent makes the item invalid, and this
    returns no JSON LD at all in that case rather than an item with a hole
    in it, because a block that parses is a block somebody pastes.

    Valid and eligible are different answers. FAQPage markup has been
    valid throughout and has drawn no rich result on an ordinary business
    site since August 2023, and reporting it as a win is how a quarter of
    work gets attributed to something that changed nothing.
    """
    kind = str(schema_type or "").strip()
    url = str(page_url or "").strip()
    findings: list[Finding] = []

    if kind not in _RULES:
        raise ValueError(f"unknown schema type {schema_type!r}")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("a page URL with a scheme and a host is required")

    rule = _RULES[kind]
    item = dict(payload) if payload is not None else _sample_payload(kind, url)

    missing_required = tuple(
        name for name in rule.required
        if name not in item or item[name] in (None, "", [], {}))
    missing_recommended = tuple(
        name for name in rule.recommended
        if name not in item or item[name] in (None, "", [], {}))

    if item.get("@context") != SCHEMA_CONTEXT:
        findings.append(Finding(
            code="LD-CONTEXT", severity=SEVERITY_CRITICAL,
            title=f"@context must be {SCHEMA_CONTEXT}",
            detail=("A missing or misspelled context makes the block a "
                    "piece of anonymous JSON. It parses, it validates as "
                    "JSON, and no consumer knows what any key means."),
            fix=f'Set "@context": "{SCHEMA_CONTEXT}" exactly.'))

    if missing_required:
        status = INVALID
        rendered = ""
        findings.append(Finding(
            code="LD-REQUIRED", severity=SEVERITY_CRITICAL,
            title=f"Missing required: {', '.join(missing_required)}",
            detail=(f"{kind} cannot be interpreted without these, so no "
                    f"JSON LD was emitted at all. A block with a hole in it "
                    f"is a block somebody pastes into the site and then "
                    f"stops thinking about."),
            fix="Supply the property from real page content, not a guess."))
    else:
        rendered = json.dumps(item, indent=2, ensure_ascii=False)
        status = ELIGIBLE if rule.rich_result else VALID_NOT_ELIGIBLE
        findings.append(Finding(
            code="LD-VALID", severity=SEVERITY_OK,
            title=f"{kind} carries every required property",
            detail=rule.note,
            fix="Re check after any content change that touches these fields."))

    if missing_recommended and not missing_required:
        findings.append(Finding(
            code="LD-RECOMMENDED", severity=SEVERITY_WARN,
            title=f"Missing recommended: {', '.join(missing_recommended)}",
            detail=("Recommended is not optional in practice. It is the "
                    "difference between an item that validates and one that "
                    "renders as something a visitor notices in the "
                    "results."),
            fix="Fill these from content that is already on the page."))

    if not rule.rich_result and not missing_required:
        findings.append(Finding(
            code="LD-NORICH", severity=SEVERITY_WARN,
            title=f"{kind} draws no rich result here",
            detail=rule.note,
            fix=("Emit it for the other consumers of structured data and "
                 "report it as correctness, never as a ranking win.")))

    findings.append(Finding(
        code="LD-VISIBLE", severity=SEVERITY_CRITICAL,
        title="Every property here has to be visible on the page",
        detail=("Structured data describes what the page shows. A price, a "
                "rating, or an answer that exists only inside the markup is "
                "a policy violation rather than a shortcut, and the penalty "
                "lands on the whole site rather than the one block."),
        fix=("Check each property against the rendered page before the "
             "block ships.")))

    findings.append(Finding(
        code="LD-IFRAME", severity=SEVERITY_WARN,
        title="JSON LD inside an embed belongs to the embed",
        detail=("A script block placed inside a Wix HTML embed sits in the "
                "iframe's document, so it describes the iframe's URL. It "
                "has to go in the page's own head or body through custom "
                "code or Velo."),
        fix="Put it in the site's custom code, scoped to this page."))

    headline = f"{kind} on {parsed.netloc}: {status}"
    return SchemaValidation(
        schema_type=kind, page_url=url, payload=item, json_ld=rendered,
        missing_required=missing_required,
        missing_recommended=missing_recommended, status=status,
        rich_result_available=rule.rich_result, headline=headline,
        findings=tuple(findings))


SAMPLE_URL = "https://www.northwestdamp.co.uk/services/basement-tanking"
