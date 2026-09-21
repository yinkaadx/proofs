"""Wix Technical SEO and Embed Indexer Console.

Rendered inside the hub app. All logic lives in core.py, which has no
Streamlit dependency and parses HTML with the standard library, so the
same engine could run inside an audit job.

Everything on this page is evaluated live from the controls. There is no
session state to go stale, which removes the class of bug where a card on
screen describes a verdict that two interactions ago stopped being true.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.wix_seo_embed_indexer_console.core import (
    ELIGIBLE,
    EMBED_TYPES,
    ENGINE_VERSION,
    INDEXED_ELSEWHERE,
    INDEXED_ON_THIS_PAGE,
    INVALID,
    NOT_INDEXED,
    NO_TEXT,
    SAMPLE_SNIPPET,
    SAMPLE_URL,
    SCHEMA_TYPES,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SEVERITY_WARN,
    analyze_wix_embed_indexing,
    required_properties,
    simulate_velo_migration,
    validate_json_ld_schema,
)

_TONE = {SEVERITY_OK: "ok", SEVERITY_WARN: "warn", SEVERITY_CRITICAL: "crit"}

_STATUS_TONE = {
    INDEXED_ON_THIS_PAGE: "ok",
    INDEXED_ELSEWHERE: "crit",
    NOT_INDEXED: "crit",
    NO_TEXT: "warn",
}


def _kpis(pairs) -> None:
    cells = "".join(
        f'<div class="app-kpi {tone}"><b>{esc(value)}</b>'
        f"<span>{esc(label)}</span></div>"
        for value, label, tone in pairs)
    st.markdown(f'<div class="app-kpis">{cells}</div>',
                unsafe_allow_html=True)


def _finding_card(finding) -> None:
    tone = _TONE[finding.severity]
    st.markdown(
        f'<div class="app-card {tone}">\n'
        f'  <h4><span class="app-tag {tone}">{esc(finding.code)}</span>\n'
        f"  {esc(finding.title)}</h4>\n"
        f"  <p>{esc(finding.detail)}</p>\n"
        f'  <div class="app-ev">Fix: {esc(finding.fix)}</div>\n'
        f"</div>",
        unsafe_allow_html=True)


def _stage(state: str, name: str, detail: str) -> str:
    return (f'<div class="app-stage"><span class="s {state}">'
            f"{esc(state.upper())}</span>"
            f'<span class="n">{esc(name)}</span>'
            f'<span class="d">{esc(detail)}</span></div>')


def _indexability_badge(analysis, label: str) -> str:
    tone = _STATUS_TONE[analysis.indexing_status]
    return (
        f'<div class="app-card {tone}">\n'
        f'  <h4><span class="app-tag {tone}">{esc(label)}</span>\n'
        f"  {esc(analysis.indexing_status)}</h4>\n"
        f"  <p>{esc(analysis.embed_type)}. {esc(analysis.rendering)}.</p>\n"
        f'  <div class="app-ev">Googlebot visibility '
        f"{analysis.visibility} of 100</div>\n"
        f"</div>")


def render() -> None:
    inject()
    st.markdown(
        '<div class="app-hero">\n'
        "  <h1>Wix Technical SEO and Embed Indexer Console</h1>\n"
        "  <p>The question about an embed is not whether a crawler can read\n"
        "  it. A cross origin iframe is perfectly crawlable, and when its\n"
        "  text is indexed it is credited to the iframe's own URL, which\n"
        "  nobody searches for and nothing links to. A Velo custom element\n"
        "  is registered on the page itself, so what it renders is content\n"
        "  on this URL. The migration below counts the words on both sides,\n"
        "  because a migration that drops a heading has moved the problem\n"
        "  somewhere nobody is looking.</p>\n"
        "</div>",
        unsafe_allow_html=True)

    embed_tab, velo_tab, schema_tab = st.tabs(
        ["Embed indexing", "Velo migration", "JSON LD validator"])

    # -- 1. embed indexing -------------------------------------------------
    with embed_tab:
        st.markdown(
            "#### Crawlable and credited to you are different questions\n\n"
            "Every row below can be fetched by a crawler. Only some of them "
            "put words on the URL a visitor typed.")

        col_a, col_b = st.columns([3, 1])
        with col_a:
            embed_type = st.selectbox("Embed type", EMBED_TYPES, index=0)
        with col_b:
            contains_text = st.toggle("Carries text", value=True)

        analysis = analyze_wix_embed_indexing(embed_type, contains_text)
        tone = _TONE[analysis.severity]
        status_tone = _STATUS_TONE[analysis.indexing_status]

        _kpis([
            (analysis.indexing_status, "Indexing", status_tone),
            (f"{analysis.visibility} of 100", "Googlebot visibility",
             status_tone),
            ("yes" if analysis.same_origin else "no", "Same origin",
             "ok" if analysis.same_origin else "crit"),
            ("yes" if analysis.in_parent_dom else "no", "In the parent DOM",
             "ok" if analysis.in_parent_dom else "crit"),
        ])

        st.markdown(
            f'<div class="app-card {tone}">\n'
            f'  <h4><span class="app-tag {status_tone}">'
            f'{esc(analysis.indexing_status)}</span>\n'
            f"  {esc(analysis.headline)}</h4>\n"
            f"  <p>{esc(analysis.technical_reason)}</p>\n"
            f'  <div class="app-ev">Rendering: '
            f"{esc(analysis.rendering)}</div>\n"
            f"</div>",
            unsafe_allow_html=True)

        st.markdown("#### Every embed type against the same question")
        rows = []
        for one in EMBED_TYPES:
            outcome = analyze_wix_embed_indexing(one, True)
            state = ("pass" if outcome.counts_for_this_page
                     else "fail" if outcome.indexing_status == NOT_INDEXED
                     else "warn")
            rows.append(_stage(
                state, one,
                f"{outcome.indexing_status}. Visibility "
                f"{outcome.visibility} of 100."))
        st.markdown(f'<div class="app-card">{"".join(rows)}</div>',
                    unsafe_allow_html=True)

        for finding in analysis.findings:
            _finding_card(finding)

    # -- 2. velo migration -------------------------------------------------
    with velo_tab:
        st.markdown(
            "#### Move the words into this document, and count them\n\n"
            "The migration is only worth something if nothing is lost on "
            "the way, so the words the snippet carried and the words the "
            "rendered element will carry are counted separately and "
            "compared.")

        snippet = st.text_area("HTML currently sitting in the embed",
                               value=SAMPLE_SNIPPET, height=230)
        element_tag = st.text_input("Custom element tag",
                                    value="seo-content-block")

        try:
            migration = simulate_velo_migration(snippet, element_tag)
        except ValueError as error:
            st.markdown(
                f'<div class="app-card crit">\n'
                f'  <h4><span class="app-tag crit">REFUSED</span>\n'
                f"  {esc(str(error))}</h4>\n"
                f"  <p>A custom element tag has to be lowercase and contain "
                f"a hyphen. That is the specification, not a convention, "
                f"and a tag without one is silently treated as an unknown "
                f"HTML element.</p>\n"
                f"</div>",
                unsafe_allow_html=True)
        else:
            tone = _TONE[migration.severity]

            _kpis([
                (str(migration.words_in), "Words in the embed", ""),
                (str(migration.words_out), "Words in the parent DOM",
                 "ok" if migration.lossless else "crit"),
                (str(len(migration.blocks)), "Blocks moved", ""),
                (str(len(migration.links)), "Links moved", ""),
            ])

            left, right = st.columns(2)
            with left:
                st.markdown(_indexability_badge(migration.before, "BEFORE"),
                            unsafe_allow_html=True)
            with right:
                st.markdown(_indexability_badge(migration.after, "AFTER"),
                            unsafe_allow_html=True)

            st.markdown(
                f'<div class="app-card {tone}">\n'
                f'  <h4><span class="app-tag '
                f'{"ok" if migration.lossless else "crit"}">MIGRATION</span>\n'
                f"  {esc(migration.headline)}</h4>\n"
                f"</div>",
                unsafe_allow_html=True)

            st.markdown("**Custom element definition**")
            st.code(migration.element_definition, language="javascript")
            st.markdown("**Page code**")
            st.code(migration.page_code, language="javascript")
            st.markdown("**What lands in the parent document**")
            st.code(migration.rendered_dom or "(nothing)", language="html")

            st.caption(
                f"{migration.words_in} word(s) went in and "
                f"{migration.words_out} came out across "
                f"{len(migration.blocks)} block(s). Nothing was summarised "
                f"and nothing was dropped except "
                f"{', '.join(migration.dropped_tags) or 'nothing'}.")

            for finding in migration.findings:
                _finding_card(finding)

    # -- 3. json ld --------------------------------------------------------
    with schema_tab:
        st.markdown(
            "#### Valid and eligible are different answers\n\n"
            "FAQPage markup has been valid throughout and has drawn no rich "
            "result on an ordinary business site since August 2023. "
            "Reporting it as a win is how a quarter of work gets attributed "
            "to something that changed nothing.")

        col_c, col_d = st.columns([1, 2])
        with col_c:
            schema_type = st.selectbox("Schema type", SCHEMA_TYPES, index=0)
        with col_d:
            page_url = st.text_input("Page URL", value=SAMPLE_URL)
        drop_required = st.toggle(
            "Remove a required property to see the refusal", value=False)

        try:
            base = validate_json_ld_schema(schema_type, page_url)
        except ValueError as error:
            st.markdown(
                f'<div class="app-card crit">\n'
                f'  <h4><span class="app-tag crit">REFUSED</span>\n'
                f"  {esc(str(error))}</h4>\n"
                f"</div>",
                unsafe_allow_html=True)
        else:
            payload = dict(base.payload)
            if drop_required:
                payload.pop(required_properties(schema_type)[0], None)
            validation = validate_json_ld_schema(schema_type, page_url,
                                                 payload)

            tone = _TONE[validation.severity]
            status_tone = (
                "ok" if validation.status == ELIGIBLE
                else "crit" if validation.status == INVALID else "warn")

            _kpis([
                (validation.status, "Status", status_tone),
                ("yes" if validation.valid else "no", "Valid",
                 "ok" if validation.valid else "crit"),
                ("yes" if validation.rich_result_available else "no",
                 "Rich result exists for this type",
                 "ok" if validation.rich_result_available else "warn"),
                (str(len(validation.missing_recommended)),
                 "Missing recommended",
                 "warn" if validation.missing_recommended else "ok"),
            ])

            st.markdown(
                f'<div class="app-card {tone}">\n'
                f'  <h4><span class="app-tag {status_tone}">'
                f'{esc(validation.status)}</span>\n'
                f"  {esc(validation.headline)}</h4>\n"
                f"</div>",
                unsafe_allow_html=True)

            if validation.json_ld:
                st.markdown("**Emitted JSON LD**")
                st.code(
                    '<script type="application/ld+json">\n'
                    + validation.json_ld + "\n</script>",
                    language="html")
            else:
                rows = "".join(
                    _stage("fail", "Required property missing", name)
                    for name in validation.missing_required)
                st.markdown(f'<div class="app-card crit">{rows}</div>',
                            unsafe_allow_html=True)
                st.caption(
                    "No JSON LD was emitted. A block with a hole in it is a "
                    "block somebody pastes into the site and then stops "
                    "thinking about.")

            for finding in validation.findings:
                _finding_card(finding)

    st.markdown(
        f'<div class="app-foot">Engine {esc(ENGINE_VERSION)}. The HTML is '
        f"parsed with the standard library and the words are counted on "
        f"both sides of the migration, so the before and after badges are a "
        f"measurement rather than an illustration. A schema missing a "
        f"required property emits no JSON LD at all. Confirm any custom "
        f"element in the rendered DOM and never in view source: a web "
        f"component writes its content with JavaScript, so an empty tag in "
        f"view source is the expected result and not a regression.</div>",
        unsafe_allow_html=True)
