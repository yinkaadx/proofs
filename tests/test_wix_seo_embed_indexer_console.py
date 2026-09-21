"""Tests for the Wix Technical SEO and Embed Indexer Console.

The indexing tests are about attribution rather than about crawlability,
because a cross origin iframe is perfectly crawlable and the text in it
belongs to a URL nobody searches for. The migration tests count the words
on both sides, since a migration that drops a heading has moved the
problem rather than solved it. The schema tests require a refusal when a
required property is absent.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

from tools.wix_seo_embed_indexer_console.core import (
    ELIGIBLE,
    EMBED_CUSTOM_CODE,
    EMBED_HTML_IFRAME,
    EMBED_NATIVE_TEXT,
    EMBED_THIRD_PARTY_IFRAME,
    EMBED_TYPES,
    EMBED_VELO_LIGHT_DOM,
    EMBED_VELO_ON_CLICK,
    EMBED_VELO_SHADOW_DOM,
    ENGINE_VERSION,
    INDEXED_ELSEWHERE,
    INDEXED_ON_THIS_PAGE,
    INVALID,
    NOT_INDEXED,
    NO_TEXT,
    SAMPLE_SNIPPET,
    SAMPLE_URL,
    SCHEMA_CONTEXT,
    SCHEMA_TYPES,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    TYPE_ARTICLE,
    TYPE_FAQ,
    TYPE_LOCAL_BUSINESS,
    TYPE_ORGANIZATION,
    TYPE_PRODUCT,
    VALID_NOT_ELIGIBLE,
    analyze_wix_embed_indexing,
    recommended_properties,
    required_properties,
    simulate_velo_migration,
    validate_json_ld_schema,
    word_count,
)
from tools.registry import all_tools

ROOT = pathlib.Path(__file__).resolve().parents[1]
TOOL_DIR = ROOT / "tools" / "wix_seo_embed_indexer_console"


# ---------------------------------------------------------------------------
# Embed indexing
# ---------------------------------------------------------------------------

def test_a_wix_html_embed_is_credited_to_the_iframe_and_not_the_page():
    """The words are crawlable. They belong to a URL nobody searches for."""
    analysis = analyze_wix_embed_indexing(EMBED_HTML_IFRAME, True)
    assert analysis.indexing_status == INDEXED_ELSEWHERE
    assert not analysis.counts_for_this_page
    assert not analysis.same_origin
    assert not analysis.in_parent_dom
    assert "EMB-CROSSORIGIN" in {f.code for f in analysis.findings}


def test_every_cross_origin_embed_fails_to_credit_the_parent_page():
    for kind in (EMBED_HTML_IFRAME, EMBED_THIRD_PARTY_IFRAME):
        analysis = analyze_wix_embed_indexing(kind, True)
        assert analysis.indexing_status == INDEXED_ELSEWHERE, kind
        assert analysis.severity == SEVERITY_CRITICAL, kind


def test_a_velo_light_dom_element_counts_as_content_on_this_page():
    analysis = analyze_wix_embed_indexing(EMBED_VELO_LIGHT_DOM, True)
    assert analysis.indexing_status == INDEXED_ON_THIS_PAGE
    assert analysis.counts_for_this_page
    assert analysis.same_origin
    assert analysis.in_parent_dom


def test_shadow_dom_is_indexed_and_flagged_as_invisible_to_raw_html():
    """Googlebot flattens shadow roots, and every tool reading raw HTML
    instead will report the section as missing."""
    analysis = analyze_wix_embed_indexing(EMBED_VELO_SHADOW_DOM, True)
    assert analysis.indexing_status == INDEXED_ON_THIS_PAGE
    assert "EMB-SHADOW" in {f.code for f in analysis.findings}
    light = analyze_wix_embed_indexing(EMBED_VELO_LIGHT_DOM, True)
    assert analysis.visibility < light.visibility


def test_content_behind_a_click_never_reaches_the_index():
    """A crawler renders the page and clicks nothing."""
    analysis = analyze_wix_embed_indexing(EMBED_VELO_ON_CLICK, True)
    assert analysis.indexing_status == NOT_INDEXED
    assert not analysis.counts_for_this_page
    assert "EMB-INTERACTION" in {f.code for f in analysis.findings}


def test_being_same_origin_is_not_sufficient_on_its_own():
    """The click case is same origin and still never indexed, so the two
    conditions are genuinely independent."""
    analysis = analyze_wix_embed_indexing(EMBED_VELO_ON_CLICK, True)
    assert analysis.same_origin
    assert not analysis.counts_for_this_page


def test_an_embed_with_no_text_is_not_an_indexing_question():
    for kind in EMBED_TYPES:
        analysis = analyze_wix_embed_indexing(kind, False)
        assert analysis.indexing_status == NO_TEXT, kind
        assert analysis.visibility == 0, kind


def test_native_text_is_the_benchmark_everything_else_is_measured_against():
    analysis = analyze_wix_embed_indexing(EMBED_NATIVE_TEXT, True)
    assert analysis.visibility == 100
    for kind in EMBED_TYPES:
        assert analyze_wix_embed_indexing(kind, True).visibility <= 100


def test_only_same_origin_non_interactive_embeds_credit_this_page():
    """The rule, checked over the whole table rather than on an example."""
    for kind in EMBED_TYPES:
        analysis = analyze_wix_embed_indexing(kind, True)
        expected = analysis.same_origin and analysis.indexing_status != \
            NOT_INDEXED
        assert analysis.counts_for_this_page == expected, kind


def test_custom_code_runs_on_the_page_itself():
    analysis = analyze_wix_embed_indexing(EMBED_CUSTOM_CODE, True)
    assert analysis.same_origin
    assert analysis.counts_for_this_page


def test_an_unknown_embed_type_raises():
    with pytest.raises(ValueError):
        analyze_wix_embed_indexing("Wix App Market widget", True)


# ---------------------------------------------------------------------------
# Velo migration
# ---------------------------------------------------------------------------

def test_the_migration_loses_no_words():
    migration = simulate_velo_migration(SAMPLE_SNIPPET)
    assert migration.lossless
    assert migration.words_in == migration.words_out
    assert migration.words_out == 50
    assert "VEL-LOSSLESS" in {f.code for f in migration.findings}


def test_the_headings_and_list_items_survive_as_their_own_tags():
    migration = simulate_velo_migration(SAMPLE_SNIPPET)
    tags = [block.tag for block in migration.blocks]
    assert tags.count("h2") == 1
    assert tags.count("li") == 3
    assert tags.count("p") == 2
    assert len(migration.blocks) == 6


def test_the_link_moves_into_the_parent_document():
    """A link inside a cross origin iframe passes no signal to the parent
    page."""
    migration = simulate_velo_migration(SAMPLE_SNIPPET)
    assert migration.links == (("/case-studies/didsbury",
                                "Didsbury case study"),)
    assert "VEL-LINKS" in {f.code for f in migration.findings}


def test_the_script_is_dropped_and_named_rather_than_carried_over():
    migration = simulate_velo_migration(SAMPLE_SNIPPET)
    assert "script" in migration.dropped_tags
    assert "script" not in migration.rendered_dom
    assert "VEL-DROPPED" in {f.code for f in migration.findings}


def test_a_nested_iframe_is_dropped_because_it_reproduces_the_problem():
    migration = simulate_velo_migration(
        "<p>Hello there</p><iframe src='https://x.example'>fallback"
        "</iframe>")
    assert "iframe" in migration.dropped_tags
    assert "fallback" not in migration.rendered_dom
    assert migration.words_out == 2


def test_the_before_and_after_badges_are_computed_not_written():
    migration = simulate_velo_migration(SAMPLE_SNIPPET)
    assert migration.before.indexing_status == INDEXED_ELSEWHERE
    assert migration.after.indexing_status == INDEXED_ON_THIS_PAGE
    assert migration.after.visibility > migration.before.visibility


def test_the_emitted_element_defines_a_valid_custom_element():
    migration = simulate_velo_migration(SAMPLE_SNIPPET, "seo-content-block")
    assert "customElements.define('seo-content-block'" in \
        migration.element_definition
    assert "connectedCallback" in migration.element_definition
    assert "this.innerHTML" in migration.element_definition


def test_the_element_renders_on_connect_and_never_waits_for_a_click():
    migration = simulate_velo_migration(SAMPLE_SNIPPET)
    assert "addEventListener('click'" not in migration.element_definition
    assert "connectedCallback" in migration.element_definition


def test_the_embedded_payload_is_real_json_carrying_every_block():
    migration = simulate_velo_migration(SAMPLE_SNIPPET)
    start = migration.element_definition.index("const blocks = ") + 15
    end = migration.element_definition.index(";\n", start)
    payload = json.loads(migration.element_definition[start:end])
    assert len(payload) == len(migration.blocks)
    assert sum(word_count(row["text"]) for row in payload) == \
        migration.words_out


def test_a_tag_without_a_hyphen_is_refused():
    """That is the specification, not a convention. A tag without one is
    treated as an unknown HTML element."""
    for tag in ("seocontent", "SeoContent", "", "-leading", "trailing-"):
        with pytest.raises(ValueError):
            simulate_velo_migration(SAMPLE_SNIPPET, tag)


def test_an_empty_snippet_produces_an_empty_element_and_not_a_crash():
    migration = simulate_velo_migration("")
    assert migration.blocks == ()
    assert migration.words_in == 0
    assert migration.words_out == 0
    assert migration.lossless


def test_markup_is_escaped_on_the_way_into_the_rendered_dom():
    migration = simulate_velo_migration("<p>5 &lt; 7 &amp; rising</p>")
    assert "&lt;" in migration.rendered_dom
    assert "&amp;" in migration.rendered_dom


# ---------------------------------------------------------------------------
# JSON LD validation
# ---------------------------------------------------------------------------

def test_every_shipped_sample_carries_its_required_properties():
    for schema_type in SCHEMA_TYPES:
        validation = validate_json_ld_schema(schema_type, SAMPLE_URL)
        assert validation.valid, schema_type
        assert validation.missing_required == (), schema_type
        assert validation.json_ld, schema_type
        assert json.loads(validation.json_ld)["@context"] == SCHEMA_CONTEXT


def test_a_missing_required_property_emits_no_json_ld_at_all():
    """A block with a hole in it is a block somebody pastes into the site
    and then stops thinking about."""
    for schema_type in SCHEMA_TYPES:
        base = validate_json_ld_schema(schema_type, SAMPLE_URL)
        payload = dict(base.payload)
        payload.pop(required_properties(schema_type)[0])
        validation = validate_json_ld_schema(schema_type, SAMPLE_URL,
                                             payload)
        assert validation.status == INVALID, schema_type
        assert validation.json_ld == "", schema_type
        assert not validation.valid, schema_type
        assert validation.severity == SEVERITY_CRITICAL, schema_type


def test_a_product_without_an_offer_is_invalid():
    validation = validate_json_ld_schema(
        TYPE_PRODUCT, SAMPLE_URL,
        {"@context": SCHEMA_CONTEXT, "@type": TYPE_PRODUCT,
         "name": "Cavity drain membrane survey",
         "aggregateRating": {"@type": "AggregateRating", "ratingValue": "5"}})
    assert "offers" in validation.missing_required
    assert validation.json_ld == ""


def test_valid_and_eligible_are_kept_apart():
    """FAQPage has been valid throughout and draws no rich result on an
    ordinary business site since August 2023."""
    faq = validate_json_ld_schema(TYPE_FAQ, SAMPLE_URL)
    assert faq.valid
    assert not faq.eligible
    assert faq.status == VALID_NOT_ELIGIBLE
    assert not faq.rich_result_available
    assert "LD-NORICH" in {f.code for f in faq.findings}


def test_organization_is_valid_and_also_draws_no_snippet():
    org = validate_json_ld_schema(TYPE_ORGANIZATION, SAMPLE_URL)
    assert org.valid
    assert org.status == VALID_NOT_ELIGIBLE


def test_an_article_with_every_property_is_eligible():
    article = validate_json_ld_schema(TYPE_ARTICLE, SAMPLE_URL)
    assert article.status == ELIGIBLE
    assert article.eligible
    assert article.missing_recommended == ()


def test_a_missing_recommended_property_warns_without_invalidating():
    base = validate_json_ld_schema(TYPE_ARTICLE, SAMPLE_URL)
    payload = dict(base.payload)
    payload.pop("image")
    validation = validate_json_ld_schema(TYPE_ARTICLE, SAMPLE_URL, payload)
    assert validation.valid
    assert validation.status == ELIGIBLE
    assert "image" in validation.missing_recommended
    assert "LD-RECOMMENDED" in {f.code for f in validation.findings}


def test_a_wrong_context_is_raised_as_critical():
    validation = validate_json_ld_schema(
        TYPE_ARTICLE, SAMPLE_URL,
        {"@context": "http://schema.org/", "@type": TYPE_ARTICLE,
         "headline": "Something"})
    assert "LD-CONTEXT" in {f.code for f in validation.findings}


def test_a_local_business_address_is_an_object_and_not_a_string():
    """A flattened address is the most common reason a local business item
    is rejected."""
    validation = validate_json_ld_schema(TYPE_LOCAL_BUSINESS, SAMPLE_URL)
    address = validation.payload["address"]
    assert isinstance(address, dict)
    assert address["@type"] == "PostalAddress"
    assert {"streetAddress", "addressLocality", "postalCode",
            "addressCountry"} <= set(address)


def test_every_validation_states_the_visible_content_rule():
    """A price or a rating that exists only inside the markup is a policy
    violation rather than a shortcut."""
    for schema_type in SCHEMA_TYPES:
        codes = {f.code for f in
                 validate_json_ld_schema(schema_type, SAMPLE_URL).findings}
        assert "LD-VISIBLE" in codes, schema_type
        assert "LD-IFRAME" in codes, schema_type


def test_required_and_recommended_never_overlap():
    for schema_type in SCHEMA_TYPES:
        assert not (set(required_properties(schema_type))
                    & set(recommended_properties(schema_type))), schema_type


def test_an_unknown_type_or_a_bare_url_raises():
    with pytest.raises(ValueError):
        validate_json_ld_schema("HowTo", SAMPLE_URL)
    for url in ("northwestdamp.co.uk", "/services", "", "ftp://x.example"):
        with pytest.raises(ValueError):
            validate_json_ld_schema(TYPE_ARTICLE, url)


# ---------------------------------------------------------------------------
# House invariants
# ---------------------------------------------------------------------------

def test_the_engine_is_versioned():
    assert ENGINE_VERSION == "1.0.0"


def test_the_core_imports_no_streamlit():
    """The engine has to run outside the app, or it is not an engine."""
    script = (
        "import sys; sys.modules['streamlit'] = None; "
        "sys.path.insert(0, %r); "
        "import tools.wix_seo_embed_indexer_console.core as c; "
        "print(c.ENGINE_VERSION)" % str(ROOT))
    done = subprocess.run([sys.executable, "-c", script],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert "1.0.0" in done.stdout


def test_no_dash_characters_reach_the_screen():
    for name in ("core.py", "page.py"):
        text = (TOOL_DIR / name).read_text(encoding="utf-8")
        assert "—" not in text, name
        assert "–" not in text, name


def test_the_tool_is_registered_with_a_unique_icon():
    tools = [t for t in all_tools() if not t.key.startswith("synthetic-")]
    keys = [t.key for t in tools]
    assert "wix-seo-embed-indexer-console" in keys
    icons = [t.icon for t in tools]
    assert len(icons) == len(set(icons))
    assert SEVERITY_OK != SEVERITY_CRITICAL
