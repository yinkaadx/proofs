"""Page tests for the HubSpot B2B Network Architect, via AppTest.

Written for pytest.

Run: pytest tests/test_hubspot_b2b_network_architect_page.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.hubspot_b2b_network_architect.core import (  # noqa: E402
    CATEGORY_HUBSPOT,
    CATEGORY_USER,
    CODE_AMBIGUOUS_TYPE,
    CODE_BACKFILL,
    CODE_CLEAN,
    CODE_DUPLICATE,
    CODE_LEDGER_CLEAN,
    CODE_MAP_CLEAN,
    CODE_REDELIVERY,
    CODE_REPLACED,
    ENGINE_VERSION,
    LABEL_INTRODUCED_BY,
    LABEL_TARGET_FOR,
    MODE_BLIND_CREATE,
    MODE_SEARCH_APPEND,
    MODE_SEARCH_REPLACE,
    PROPERTY_NAME,
    SAMPLE_COMPANIES,
    SKIPPED_HISTORICAL,
    SOURCE_SURFE,
    normalise_domain,
    sample_messages,
)

HARNESS = (Path(__file__).resolve().parent
           / "_page_harness_hubspot_b2b_network_architect.py")

COMPANY = "Company"
NETWORKS = "Gridwise networks to assign"
MODE = "How the integration writes the property"
NORMALISE = "Normalise the domain before searching"
LABEL = "Custom association label"
HARDCODE = "Hardcode the typeId instead of resolving the label"
SOURCE = "Integration"
SKIP = "Skip anything sent before the connection"
DELIVERED = "Messages delivered"

ALL_NETWORKS = ["Client Network", "Member Network", "Relationship Network"]


def text_of(at: AppTest) -> str:
    parts: list[str] = []
    for attr in ("markdown", "text", "info", "success", "warning", "error",
                 "caption", "code", "header", "subheader", "title"):
        try:
            parts += [str(getattr(el, "value", "")) for el in getattr(at, attr)]
        except Exception:
            pass
    try:
        for frame in at.get("dataframe"):
            value = getattr(frame, "value", None)
            if value is not None and hasattr(value, "to_numpy"):
                parts += [str(cell) for cell in value.to_numpy().ravel()]
                parts += [str(col) for col in value.columns]
    except Exception:
        pass
    return "\n".join(parts)


def flat(at: AppTest) -> str:
    """The page text with runs of whitespace collapsed.

    The hero and the footer are HTML, where a newline in the source is just a
    space on screen. Asserting against the raw source makes a test fail when a
    sentence is rewrapped, which measures the line breaks rather than the
    words the reader sees.
    """
    return re.sub(r"\s+", " ", text_of(at))


def run_app(timeout: int = 180) -> AppTest:
    at = AppTest.from_file(str(HARNESS), default_timeout=timeout)
    at.run()
    return at


def widget(at: AppTest, kind: str, label: str):
    for el in getattr(at, kind):
        if el.label == label:
            return el
    raise AssertionError(f"no {kind} labelled {label!r}; "
                         f"have {[el.label for el in getattr(at, kind)]}")


def set_value(at: AppTest, kind: str, label: str, value) -> AppTest:
    widget(at, kind, label).set_value(value).run()
    return at


# ---------------------------------------------------------------------------
# Cold start
# ---------------------------------------------------------------------------

def test_the_page_renders_with_no_exception():
    at = run_app()
    assert not at.exception, [str(e.value) for e in at.exception]


def test_the_hero_and_the_three_tabs_are_present():
    at = run_app()
    assert "HubSpot B2B Network Architect" in text_of(at)
    labels = [tab.label for tab in at.tabs]
    for expected in ("Network Categorization", "Prospect Associations",
                     "LinkedIn Ledger"):
        assert expected in labels


def test_no_two_widgets_of_a_kind_share_a_label():
    at = run_app()
    for kind in ("slider", "toggle", "selectbox", "radio", "multiselect"):
        labels = [el.label for el in getattr(at, kind)]
        assert len(labels) == len(set(labels)), f"{kind}: {labels}"


def test_it_says_plainly_that_no_portal_is_connected():
    body = flat(run_app())
    assert "No HubSpot token is held" in body
    assert "no HubSpot portal is connected" in body
    assert "no record or association is written" in body
    assert "no LinkedIn account is read" in body


def test_the_engine_version_is_on_screen():
    assert f"Engine version {ENGINE_VERSION}" in text_of(run_app())


def test_the_sidebar_opens_with_how_to_use():
    """The sidebar belongs to the tool, so its first block is the instructions."""
    assert "How to use" in [el.value for el in run_app().sidebar.subheader]


# ---------------------------------------------------------------------------
# Network categorization
# ---------------------------------------------------------------------------

def test_it_opens_on_one_record_with_no_duplicates():
    body = text_of(run_app())
    assert CODE_CLEAN in body
    assert CODE_DUPLICATE not in body


def test_all_three_networks_land_on_one_record():
    at = set_value(run_app(), "multiselect", NETWORKS, ALL_NETWORKS)
    body = text_of(at)
    assert "client_network;member_network;relationship_network" in body
    assert CODE_DUPLICATE not in body


def test_creating_a_company_per_network_is_reported_on_screen():
    at = set_value(run_app(), "radio", MODE, MODE_BLIND_CREATE)
    body = text_of(at)
    assert CODE_DUPLICATE in body
    assert "Duplicates created" in body


def test_replacing_keeps_one_record_and_still_raises_a_finding():
    at = set_value(run_app(), "radio", MODE, MODE_SEARCH_REPLACE)
    body = text_of(at)
    assert CODE_REPLACED in body
    assert CODE_DUPLICATE not in body


def test_the_append_mode_is_the_default():
    assert widget(run_app(), "radio", MODE).value == MODE_SEARCH_APPEND


def test_the_property_name_is_shown_as_a_column():
    assert PROPERTY_NAME in text_of(run_app())


def test_the_last_request_is_shown_in_full():
    body = text_of(run_app())
    assert "/crm/v3/objects/companies" in body
    assert '"properties"' in body


def test_the_leading_semicolon_is_visible_in_the_request():
    at = set_value(run_app(), "multiselect", NETWORKS, ALL_NETWORKS)
    assert '";relationship_network"' in text_of(at)


def test_the_variant_table_shows_every_paste_resolving_to_one_domain():
    body = text_of(run_app())
    assert "Normalised" in body
    assert "Matches" in body


def test_turning_off_normalisation_leaves_the_raw_website_as_the_domain():
    at = set_value(run_app(), "toggle", NORMALISE, False)
    assert SAMPLE_COMPANIES[0].website.strip().lower() in text_of(at)


@pytest.mark.parametrize("seed", SAMPLE_COMPANIES)
def test_every_company_renders_and_shows_its_normalised_domain(seed):
    at = set_value(run_app(), "selectbox", COMPANY, seed.name)
    assert not at.exception, [str(e.value) for e in at.exception]
    assert normalise_domain(seed.website) in text_of(at)


def test_choosing_no_network_still_renders():
    at = set_value(run_app(), "multiselect", NETWORKS, [])
    assert not at.exception, [str(e.value) for e in at.exception]


# ---------------------------------------------------------------------------
# Prospect associations
# ---------------------------------------------------------------------------

def test_the_association_map_opens_clean_with_one_primary():
    body = text_of(run_app())
    assert CODE_MAP_CLEAN in body
    assert "Companies marked primary" in body


def test_both_companies_appear_with_their_record_ids():
    body = text_of(run_app())
    assert "Northwind Freight (8001)" in body
    assert "Halyard Capital Partners (8044)" in body


def test_the_target_for_label_is_on_the_client_not_the_employer():
    body = text_of(run_app())
    assert LABEL_TARGET_FOR in body
    assert CATEGORY_USER in body


def test_hardcoding_the_type_id_is_reported_as_critical():
    at = set_value(run_app(), "toggle", HARDCODE, True)
    body = text_of(at)
    assert CODE_AMBIGUOUS_TYPE in body
    assert CODE_MAP_CLEAN not in body


def test_the_second_label_can_be_selected():
    at = set_value(run_app(), "selectbox", LABEL, LABEL_INTRODUCED_BY)
    assert LABEL_INTRODUCED_BY in text_of(at)


def test_the_label_table_shows_both_categories():
    body = text_of(run_app())
    assert CATEGORY_HUBSPOT in body
    assert CATEGORY_USER in body


def test_the_request_carries_the_category_beside_the_type_id():
    body = text_of(run_app())
    assert "associationCategory" in body
    assert "associationTypeId" in body


def test_the_labels_endpoint_is_shown_as_the_way_ids_are_resolved():
    assert "/crm/v4/associations/contacts/companies/labels" in text_of(run_app())


# ---------------------------------------------------------------------------
# LinkedIn ledger
# ---------------------------------------------------------------------------

def test_the_ledger_opens_guarded():
    body = text_of(run_app())
    assert CODE_LEDGER_CLEAN in body
    assert CODE_REDELIVERY in body
    assert CODE_BACKFILL not in body


def test_turning_the_cutoff_off_backfills_and_says_so():
    at = set_value(run_app(), "toggle", SKIP, False)
    body = text_of(at)
    assert CODE_BACKFILL in body
    assert SKIPPED_HISTORICAL not in body


def test_the_workflow_enrolment_count_is_on_screen():
    assert "Workflow enrolments" in text_of(run_app())


def test_the_comparison_shows_both_runs():
    body = text_of(run_app())
    assert "Ledger on" in body
    assert "Ledger off" in body


def test_the_other_integration_can_be_selected():
    at = set_value(run_app(), "selectbox", SOURCE, SOURCE_SURFE)
    assert f'"source": "{SOURCE_SURFE}"' in text_of(at)


@pytest.mark.parametrize("count", range(1, len(sample_messages()) + 1))
def test_every_prefix_of_the_payload_renders(count):
    at = set_value(run_app(), "slider", DELIVERED, count)
    assert not at.exception, [str(e.value) for e in at.exception]


def test_the_fingerprint_is_shown_beside_every_message():
    assert "Fingerprint" in text_of(run_app())


def test_the_incoming_payload_is_on_screen_as_json():
    body = text_of(run_app())
    assert '"conversationId"' in body
    assert "urn:li:message:6613" in body


# ---------------------------------------------------------------------------
# House rules
# ---------------------------------------------------------------------------

def test_no_dash_characters_reach_the_screen():
    at = set_value(run_app(), "toggle", SKIP, False)
    at = set_value(at, "radio", MODE, MODE_BLIND_CREATE)
    at = set_value(at, "toggle", HARDCODE, True)
    body = text_of(at)
    assert "—" not in body
    assert "–" not in body
