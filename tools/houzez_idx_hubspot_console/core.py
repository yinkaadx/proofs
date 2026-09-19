"""Houzez IDX and HubSpot Console: the engine.

Three joints in a real estate funnel, and each one fails quietly rather than
loudly, which is why they survive to production.

1. The IDX feed. An MLS record carries display permissions alongside the
   data, and a listing that says do not publish the address means it. Mapping
   the fields and ignoring the flags is the mistake that gets a feed switched
   off, and a switched off feed takes the whole site's inventory with it.
2. The HubSpot form. A submission with no tracking cookie does not fail. It
   succeeds, creates the contact, and silently arrives with no page history
   attached, so attribution reporting quietly understates every channel that
   works. A validator that only checks whether the submission succeeded will
   never find it.
3. The routing scenario. A webhook retry, a double tap on a slow form, or a
   scenario rerun all produce the same lead twice. Idempotency has to come
   from a key derived from the lead itself, and the key for a contact is not
   the key for an enquiry: the same person asking about a second property is
   one contact and two enquiries, not two of each.

No Streamlit import lives here on purpose, so the same engine could sit behind
a real integration without a line changing.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

ENGINE_VERSION = "1.0.0"

SEVERITY_OK = "OK"
SEVERITY_WARN = "WARN"
SEVERITY_CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    title: str
    detail: str
    fix: str


def _worst(findings) -> str:
    for level in (SEVERITY_CRITICAL, SEVERITY_WARN):
        if any(f.severity == level for f in findings):
            return level
    return SEVERITY_OK


# ---------------------------------------------------------------------------
# 1. IDX feed to Houzez
# ---------------------------------------------------------------------------

# Houzez keeps property data in post meta behind a fave_ prefix, so the map
# from a standards shaped feed field to the theme is a rename rather than a
# transformation. Getting the prefix wrong produces a property page that
# renders with every field blank and no error anywhere.
FIELD_MAP: tuple[tuple[str, str, str], ...] = (
    ("ListingId", "fave_property_id", "The number an agent quotes on the phone"),
    ("ListPrice", "fave_property_price", "Numeric only, no currency symbol"),
    ("BedroomsTotal", "fave_property_bedrooms", "Stored as a string by the theme"),
    ("BathroomsTotalInteger", "fave_property_bathrooms", "Half baths round down here"),
    ("LivingArea", "fave_property_size", "Paired with the size postfix setting"),
    ("LotSizeSquareFeet", "fave_property_land", "Land area, separate from living area"),
    ("YearBuilt", "fave_property_year", "Blank on new build until completion"),
    ("GarageSpaces", "fave_property_garage", "Count, not a description"),
    ("UnparsedAddress", "fave_property_address", "Subject to the display flag"),
    ("PostalCode", "fave_property_zip", "Subject to the display flag"),
    ("Latitude", "fave_property_location_lat", "Half of the map pin"),
    ("Longitude", "fave_property_location_long", "The other half"),
)

# The feed's own property types, and the Houzez taxonomy term each one lands
# on. An unmapped type does not error, it lands nowhere and the listing
# vanishes from every filtered search on the site.
TYPE_MAP: dict = {
    "Residential": "Houses",
    "ResidentialLease": "Rentals",
    "ResidentialIncome": "Multi Family",
    "CommercialSale": "Commercial",
    "CommercialLease": "Commercial",
    "Land": "Land",
    "Farm": "Farm and Ranch",
}

PROPERTY_TYPES: tuple[str, ...] = tuple(TYPE_MAP)

STATUS_MAP: dict = {
    "Active": "For Sale",
    "ActiveUnderContract": "Under Offer",
    "Pending": "Under Offer",
    "Closed": "Sold",
    "Canceled": "Withdrawn",
    "Withdrawn": "Withdrawn",
}

MLS_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,31}$")

# Sample records, each written to exercise a different display rule. The
# permissions ride with the record because that is how a real feed delivers
# them: as fields, not as a separate policy document nobody reads.
SAMPLE_LISTINGS: dict = {
    "MLS-4471902": {
        "ListingKey": "a41f2e9c-7d33-4b0a-9c11-6f2b8e0d5a77",
        "ListingId": "MLS-4471902",
        "StandardStatus": "Active",
        "ListPrice": 489000,
        "BedroomsTotal": 4,
        "BathroomsTotalInteger": 2,
        "LivingArea": 2140,
        "LotSizeSquareFeet": 6500,
        "YearBuilt": 2012,
        "GarageSpaces": 2,
        "UnparsedAddress": "18 Larchfield Road, Dublin 14",
        "PostalCode": "D14XY21",
        "Latitude": 53.2965,
        "Longitude": -6.2611,
        "ListOfficeName": "Ashbourne Residential",
        "InternetEntireListingDisplayYN": True,
        "InternetAddressDisplayYN": True,
    },
    "MLS-5580311": {
        "ListingKey": "b77c1a04-2e58-4f6d-8a90-31de4c7b2f18",
        "ListingId": "MLS-5580311",
        "StandardStatus": "Active",
        "ListPrice": 1250000,
        "BedroomsTotal": 5,
        "BathroomsTotalInteger": 4,
        "LivingArea": 3890,
        "LotSizeSquareFeet": 21780,
        "YearBuilt": 1998,
        "GarageSpaces": 3,
        "UnparsedAddress": "Private, address withheld by seller",
        "PostalCode": "",
        "Latitude": 53.3108,
        "Longitude": -6.2289,
        "ListOfficeName": "Northside Prestige",
        "InternetEntireListingDisplayYN": True,
        "InternetAddressDisplayYN": False,
    },
    "MLS-6012887": {
        "ListingKey": "c9012b73-5ad6-41ee-b3c7-77a4e9d81c02",
        "ListingId": "MLS-6012887",
        "StandardStatus": "Pending",
        "ListPrice": 315000,
        "BedroomsTotal": 2,
        "BathroomsTotalInteger": 1,
        "LivingArea": 890,
        "LotSizeSquareFeet": 0,
        "YearBuilt": 2019,
        "GarageSpaces": 0,
        "UnparsedAddress": "Apartment 3, The Mill, Cork",
        "PostalCode": "T12AB34",
        "Latitude": 51.8985,
        "Longitude": -8.4756,
        "ListOfficeName": "Lee Valley Lettings",
        "InternetEntireListingDisplayYN": False,
        "InternetAddressDisplayYN": False,
    },
}

SYNC_PUBLISH = "PUBLISH"
SYNC_PUBLISH_REDACTED = "PUBLISH WITH ADDRESS WITHHELD"
SYNC_SUPPRESS = "DO NOT PUBLISH"


@dataclass(frozen=True)
class IdxRecord:
    mls_id: str
    listing_key: str
    property_type: str
    action: str
    houzez_type_term: str
    houzez_status_term: str
    meta: dict
    taxonomies: dict
    attribution: str
    withheld_fields: tuple[str, ...]
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def severity(self) -> str:
        return _worst(self.findings)

    @property
    def publishable(self) -> bool:
        return self.action != SYNC_SUPPRESS

    @property
    def mapped_field_count(self) -> int:
        return len(self.meta)


def simulate_idx_feed_sync(mls_id: str, property_type: str) -> IdxRecord:
    """Turn one feed record into the post meta a Houzez template reads.

    The display permissions are applied before the mapping rather than after.
    A listing that forbids publication produces no meta at all, because a
    dictionary that exists is a dictionary something will eventually render.
    """
    identifier = str(mls_id or "").strip()
    kind = str(property_type or "").strip()

    findings: list[Finding] = []

    if not MLS_ID_PATTERN.match(identifier):
        findings.append(Finding(
            code="IDX-ID", severity=SEVERITY_CRITICAL,
            title=f"{identifier or 'The listing id'} is not a usable listing id",
            detail=("A listing id is what the sync keys its upsert on. One "
                    "that is empty or oddly shaped means the sync either "
                    "creates a duplicate post or overwrites the wrong one."),
            fix="Take the id from the feed rather than from a spreadsheet column."))
        return IdxRecord(
            mls_id=identifier, listing_key="", property_type=kind,
            action=SYNC_SUPPRESS, houzez_type_term="", houzez_status_term="",
            meta={}, taxonomies={}, attribution="", withheld_fields=(),
            findings=tuple(findings))

    if kind not in TYPE_MAP:
        findings.append(Finding(
            code="IDX-TYPE", severity=SEVERITY_CRITICAL,
            title=f"{kind or 'The property type'} maps to no Houzez term",
            detail=("An unmapped type does not raise an error. The listing "
                    "imports with no property type term, which drops it out "
                    "of every filtered search on the site while still showing "
                    "on its own page, so nobody notices until an agent asks "
                    "why a property gets no enquiries."),
            fix=(f"Add the term to the type map. The feed sends "
                 f"{', '.join(PROPERTY_TYPES)}.")))
        return IdxRecord(
            mls_id=identifier, listing_key="", property_type=kind,
            action=SYNC_SUPPRESS, houzez_type_term="", houzez_status_term="",
            meta={}, taxonomies={}, attribution="", withheld_fields=(),
            findings=tuple(findings))

    listing = SAMPLE_LISTINGS.get(identifier)
    if listing is None:
        findings.append(Finding(
            code="IDX-ABSENT", severity=SEVERITY_WARN,
            title=f"{identifier} is not in the current feed pull",
            detail=("A feed is a replica, so a record that stops appearing "
                    "has been withdrawn at the source. Leaving the post "
                    "published keeps a sold or withdrawn property live, "
                    "which is the complaint that reaches the MLS."),
            fix=("Unpublish anything absent from the pull rather than only "
                 "updating what is present. A sync that never deletes is a "
                 "site that only grows.")))
        return IdxRecord(
            mls_id=identifier, listing_key="", property_type=kind,
            action=SYNC_SUPPRESS, houzez_type_term=TYPE_MAP[kind],
            houzez_status_term="", meta={}, taxonomies={}, attribution="",
            withheld_fields=(), findings=tuple(findings))

    entire = bool(listing.get("InternetEntireListingDisplayYN", True))
    address_ok = bool(listing.get("InternetAddressDisplayYN", True))

    if not entire:
        findings.append(Finding(
            code="IDX-NO-DISPLAY", severity=SEVERITY_CRITICAL,
            title=f"{identifier} forbids internet display entirely",
            detail=("The seller has opted this listing out of IDX display. "
                    "Publishing it anyway is a compliance breach, and the "
                    "penalty is the feed being switched off, which takes "
                    "every other listing on the site with it."),
            fix=("Skip the record and make sure any post created for it on an "
                 "earlier run is unpublished too.")))
        return IdxRecord(
            mls_id=identifier, listing_key=str(listing["ListingKey"]),
            property_type=kind, action=SYNC_SUPPRESS,
            houzez_type_term=TYPE_MAP[kind],
            houzez_status_term=STATUS_MAP.get(str(listing["StandardStatus"]), ""),
            meta={}, taxonomies={}, attribution="",
            withheld_fields=("every field",), findings=tuple(findings))

    withheld: list[str] = []
    meta: dict = {}
    for feed_field, houzez_key, _note in FIELD_MAP:
        value = listing.get(feed_field, "")
        if not address_ok and houzez_key in ("fave_property_address",
                                             "fave_property_zip"):
            withheld.append(houzez_key)
            continue
        meta[houzez_key] = "" if value is None else str(value)

    if not address_ok:
        findings.append(Finding(
            code="IDX-NO-ADDRESS", severity=SEVERITY_WARN,
            title=f"{identifier} permits display but withholds the address",
            detail=(f"{len(withheld)} field(s) are held back on purpose. The "
                    f"map pin is still allowed, so a template that plots the "
                    f"coordinates and hides the street is compliant and a "
                    f"template that reverse geocodes the pin is not."),
            fix=("Leave the address meta unset rather than writing an empty "
                 "string. A theme that checks for the key rather than for a "
                 "value will print a blank line otherwise.")))

    attribution = str(listing.get("ListOfficeName", "")).strip()
    if attribution:
        findings.append(Finding(
            code="IDX-ATTRIBUTION", severity=SEVERITY_WARN,
            title="The listing brokerage has to appear on the page",
            detail=(f"IDX rules require {attribution} to be credited on any "
                    f"page showing this listing. It is the single most "
                    f"commonly missed rule in an IDX build."),
            fix=("Render the attribution in the property template itself, not "
                 "only in a card, so it survives a template change.")))
    else:
        findings.append(Finding(
            code="IDX-NO-ATTRIBUTION", severity=SEVERITY_CRITICAL,
            title="This record carries no brokerage to credit",
            detail=("A listing with no attribution cannot be displayed "
                    "compliantly, because there is nothing to display."),
            fix="Raise it with the feed provider before publishing the record."))

    findings.append(Finding(
        code="IDX-KEY", severity=SEVERITY_OK,
        title="ListingKey is the upsert key, not ListingId",
        detail=("ListingId is readable and can repeat across MLSs. "
                "ListingKey is the stable unique one, and keying the upsert "
                "on the readable field is how the same property arrives "
                "twice from two boards."),
        fix=f"Store {listing['ListingKey']} on the post and match on it."))

    action = SYNC_PUBLISH if address_ok else SYNC_PUBLISH_REDACTED
    taxonomies = {
        "property_type": TYPE_MAP[kind],
        "property_status": STATUS_MAP.get(str(listing["StandardStatus"]),
                                          "For Sale"),
    }

    return IdxRecord(
        mls_id=identifier, listing_key=str(listing["ListingKey"]),
        property_type=kind, action=action, houzez_type_term=TYPE_MAP[kind],
        houzez_status_term=taxonomies["property_status"], meta=meta,
        taxonomies=taxonomies, attribution=attribution,
        withheld_fields=tuple(withheld), findings=tuple(findings),
    )


# ---------------------------------------------------------------------------
# 2. HubSpot embed validation
# ---------------------------------------------------------------------------

# The tracking cookie HubSpot's own script sets. A submission without it is
# accepted and attributed to nobody.
TRACKING_COOKIE_NAME = "hubspotutk"
HUTK_PATTERN = re.compile(r"^[0-9a-f]{32}$")
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]{2,}$")

REQUIRED_FIELDS: tuple[str, ...] = ("email",)
RECOMMENDED_FIELDS: tuple[str, ...] = ("firstname", "lastname", "phone")

VALID_ACCEPTED = "ACCEPTED"
VALID_ACCEPTED_UNATTRIBUTED = "ACCEPTED BUT UNATTRIBUTED"
VALID_REJECTED = "REJECTED"


@dataclass(frozen=True)
class EmbedValidation:
    status: str
    fields_seen: tuple[str, ...]
    missing_required: tuple[str, ...]
    missing_recommended: tuple[str, ...]
    tracking_cookie_present: bool
    tracking_cookie_valid: bool
    attribution_works: bool
    payload: dict
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def severity(self) -> str:
        return _worst(self.findings)

    @property
    def accepted(self) -> bool:
        return self.status != VALID_REJECTED


def validate_hubspot_embed(form_data: dict, tracking_cookie: str
                           ) -> EmbedValidation:
    """Check the submission the way HubSpot will, and then the way reporting will.

    Those are two different checks and only the first one is usually run. A
    payload with a valid email is accepted whether or not the tracking cookie
    came with it, so a validator that stops at accepted will never find the
    reason six months of attribution data is thin.
    """
    data = dict(form_data or {})
    cookie = str(tracking_cookie or "").strip()

    seen = tuple(str(k).strip().lower() for k in data)
    missing_required = tuple(f for f in REQUIRED_FIELDS if not str(
        data.get(f, "")).strip())
    missing_recommended = tuple(f for f in RECOMMENDED_FIELDS if not str(
        data.get(f, "")).strip())

    findings: list[Finding] = []

    email = str(data.get("email", "")).strip()
    if not email:
        findings.append(Finding(
            code="HS-NO-EMAIL", severity=SEVERITY_CRITICAL,
            title="No email in the payload, so HubSpot rejects the submission",
            detail=("Email is how HubSpot identifies a contact. Without it "
                    "there is no record to create and the API returns an "
                    "error rather than a contact."),
            fix="Mark the email input required in the form itself, not only in the theme."))
    elif not EMAIL_PATTERN.match(email):
        findings.append(Finding(
            code="HS-BAD-EMAIL", severity=SEVERITY_CRITICAL,
            title=f"{email} is not a valid email address",
            detail=("HubSpot validates the format on its side, so this "
                    "submission is lost at the API rather than stored badly."),
            fix=("Validate in the browser as well, so the visitor sees the "
                 "problem instead of a success message hiding a rejection.")))

    cookie_present = bool(cookie)
    cookie_valid = bool(HUTK_PATTERN.match(cookie))

    if not cookie_present:
        findings.append(Finding(
            code="HS-NO-HUTK", severity=SEVERITY_WARN,
            title=f"No {TRACKING_COOKIE_NAME} cookie came with this submission",
            detail=("The submission still succeeds and the contact is still "
                    "created. What is lost is the join to the visitor's page "
                    "history, so this lead arrives with no source, no first "
                    "touch page and no session. Attribution reporting then "
                    "understates whichever channel actually works, quietly, "
                    "for as long as nobody checks."),
            fix=(f"Confirm the HubSpot tracking script loads before the form "
                 f"renders, then read document.cookie for "
                 f"{TRACKING_COOKIE_NAME} and pass it as context.hutk. If a "
                 f"consent banner is withholding it, this is expected rather "
                 f"than broken and the reporting gap should be stated.")))
    elif not cookie_valid:
        findings.append(Finding(
            code="HS-BAD-HUTK", severity=SEVERITY_WARN,
            title=f"The {TRACKING_COOKIE_NAME} value is not the right shape",
            detail=("The cookie is a 32 character hex token. A value of any "
                    "other shape is usually a different cookie read by "
                    "mistake, or a truncated one."),
            fix=("Read the cookie by exact name rather than by a prefix "
                 "match, which is what usually picks up the wrong one.")))
    else:
        findings.append(Finding(
            code="HS-HUTK-OK", severity=SEVERITY_OK,
            title="The tracking cookie is present and correctly shaped",
            detail="This submission will join to the visitor's page history.",
            fix=("Keep passing it in context.hutk on every form, including "
                 "the ones built later by somebody else.")))

    if missing_recommended:
        findings.append(Finding(
            code="HS-THIN", severity=SEVERITY_WARN,
            title=f"{len(missing_recommended)} useful field(s) are absent",
            detail=(f"{', '.join(missing_recommended)} are not in the "
                    f"payload. The contact is created either way, and an "
                    f"agent calling back has a first name or does not."),
            fix="Collect the phone number at least, for a property enquiry."))

    if missing_required:
        status = VALID_REJECTED
    elif not EMAIL_PATTERN.match(email):
        status = VALID_REJECTED
    elif cookie_valid:
        status = VALID_ACCEPTED
    else:
        status = VALID_ACCEPTED_UNATTRIBUTED

    payload = {
        "fields": [{"name": str(k), "value": str(v)} for k, v in data.items()],
        "context": {"hutk": cookie} if cookie_valid else {},
    }

    return EmbedValidation(
        status=status, fields_seen=seen, missing_required=missing_required,
        missing_recommended=missing_recommended,
        tracking_cookie_present=cookie_present,
        tracking_cookie_valid=cookie_valid,
        attribution_works=cookie_valid and status != VALID_REJECTED,
        payload=payload, findings=tuple(findings),
    )


# ---------------------------------------------------------------------------
# 3. Make lead routing, with idempotency
# ---------------------------------------------------------------------------

ROUTE_CREATED = "CONTACT AND ENQUIRY CREATED"
ROUTE_NEW_ENQUIRY = "CONTACT REUSED, NEW ENQUIRY"
ROUTE_DUPLICATE = "DUPLICATE SUPPRESSED"
ROUTE_REJECTED = "REJECTED"


def normalise_email(value: str) -> str:
    """Lowercase and trimmed. Nothing cleverer, on purpose.

    Folding plus addressing or stripping dots is correct for some providers
    and wrong for others, and a router that silently merges two real people
    is worse than one that lets a duplicate through. The plus address is
    reported as a finding instead, so a human decides.
    """
    return str(value or "").strip().lower()


def idempotency_key(lead_email: str, property_interest: str) -> str:
    """A deterministic key for one person asking about one property."""
    email = normalise_email(lead_email)
    interest = str(property_interest or "").strip().lower()
    digest = hashlib.sha256(f"{email}|{interest}".encode("utf-8")).hexdigest()
    return digest[:16]


def contact_key(lead_email: str) -> str:
    """The contact is keyed on the person alone, whatever they asked about."""
    return hashlib.sha256(
        normalise_email(lead_email).encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class RoutingResult:
    lead_email: str
    property_interest: str
    outcome: str
    idempotency_key: str
    contact_key: str
    contact_created: bool
    enquiry_created: bool
    crm_record: dict
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def severity(self) -> str:
        return _worst(self.findings)

    @property
    def wrote_anything(self) -> bool:
        return self.contact_created or self.enquiry_created


def simulate_make_lead_routing(lead_email: str, property_interest: str,
                               seen_enquiry_keys: tuple[str, ...] = (),
                               seen_contact_keys: tuple[str, ...] = ()
                               ) -> RoutingResult:
    """Route one lead, and refuse to write it twice.

    Two keys, because there are two records and they do not dedupe the same
    way. The same person enquiring about a second property is one contact and
    a second enquiry. The same person enquiring about the same property twice,
    which is what a webhook retry looks like, is neither.
    """
    email = normalise_email(lead_email)
    interest = str(property_interest or "").strip()

    findings: list[Finding] = []

    if not email or not EMAIL_PATTERN.match(email):
        findings.append(Finding(
            code="MAKE-EMAIL", severity=SEVERITY_CRITICAL,
            title=f"{lead_email or 'The lead'} has no usable email address",
            detail=("Email is the dedupe key. Routing a lead without one "
                    "means every retry creates another record, because there "
                    "is nothing to match the previous one against."),
            fix=("Reject at the webhook and return a clear error, rather than "
                 "creating a nameless contact that somebody has to merge.")))
        return RoutingResult(
            lead_email=email, property_interest=interest,
            outcome=ROUTE_REJECTED, idempotency_key="", contact_key="",
            contact_created=False, enquiry_created=False, crm_record={},
            findings=tuple(findings))

    if not interest:
        findings.append(Finding(
            code="MAKE-NO-INTEREST", severity=SEVERITY_CRITICAL,
            title="No property interest came with this lead",
            detail=("An enquiry about nothing cannot be routed to an agent, "
                    "and it cannot be deduplicated either, because the key "
                    "depends on what was asked about."),
            fix="Carry the listing id through the form as a hidden field."))
        return RoutingResult(
            lead_email=email, property_interest=interest,
            outcome=ROUTE_REJECTED, idempotency_key="",
            contact_key=contact_key(email), contact_created=False,
            enquiry_created=False, crm_record={}, findings=tuple(findings))

    enquiry = idempotency_key(email, interest)
    person = contact_key(email)
    enquiry_seen = enquiry in set(seen_enquiry_keys)
    contact_seen = person in set(seen_contact_keys)

    if "+" in email.split("@", 1)[0]:
        findings.append(Finding(
            code="MAKE-PLUS", severity=SEVERITY_WARN,
            title="This address uses plus addressing",
            detail=("Some providers deliver a plus address to the same "
                    "inbox and some treat it as separate. Folding it "
                    "automatically would merge two real people on the "
                    "providers where it is separate, so this router does not "
                    "fold it and says so instead."),
            fix=("Decide the rule for your own audience and apply it "
                 "explicitly at the webhook, where it is visible.")))

    if enquiry_seen:
        outcome = ROUTE_DUPLICATE
        contact_created = False
        enquiry_created = False
        findings.append(Finding(
            code="MAKE-IDEMPOTENT", severity=SEVERITY_OK,
            title="This exact enquiry has already been written",
            detail=(f"Key {enquiry} is already in the ledger, so this is a "
                    f"retry, a double tap on a slow form, or a scenario "
                    f"rerun. Nothing was written."),
            fix=("Keep the key in a store the scenario can read before it "
                 "writes. A Make scenario that checks HubSpot for a duplicate "
                 "after creating one has already created it.")))
    elif contact_seen:
        outcome = ROUTE_NEW_ENQUIRY
        contact_created = False
        enquiry_created = True
        findings.append(Finding(
            code="MAKE-SECOND-PROPERTY", severity=SEVERITY_OK,
            title="Known contact, new property, so one new record not two",
            detail=("The contact already exists and is reused. Creating a "
                    "second contact here is the most common duplicate in a "
                    "property CRM, because the enquiry genuinely is new and "
                    "it is easy to create both together."),
            fix=("Use create or update on the contact and always create on "
                 "the enquiry. Create on both is the bug.")))
    else:
        outcome = ROUTE_CREATED
        contact_created = True
        enquiry_created = True
        findings.append(Finding(
            code="MAKE-NEW", severity=SEVERITY_OK,
            title="New person and new enquiry, so both records are written",
            detail=f"Contact key {person} and enquiry key {enquiry} are both new.",
            fix="Write both keys to the ledger in the same step that writes the records."))

    record = {
        "contact": {
            "email": email,
            "contact_key": person,
            "action": "created" if contact_created else "matched on email",
        },
        "enquiry": {
            "property_interest": interest,
            "idempotency_key": enquiry,
            "action": "created" if enquiry_created else "suppressed as duplicate",
        },
    } if outcome != ROUTE_REJECTED else {}

    return RoutingResult(
        lead_email=email, property_interest=interest, outcome=outcome,
        idempotency_key=enquiry, contact_key=person,
        contact_created=contact_created, enquiry_created=enquiry_created,
        crm_record=record, findings=tuple(findings),
    )


def replay_ledger(events: tuple[tuple[str, str], ...]) -> dict:
    """Run a sequence of leads through the router and count what was written.

    This is the proof rather than the demonstration: feeding the same event
    twice must not move the counters the second time.
    """
    enquiries: list[str] = []
    contacts: list[str] = []
    rows: list[RoutingResult] = []
    for email, interest in events:
        result = simulate_make_lead_routing(
            email, interest, tuple(enquiries), tuple(contacts))
        if result.enquiry_created:
            enquiries.append(result.idempotency_key)
        if result.contact_created:
            contacts.append(result.contact_key)
        rows.append(result)
    return {
        "events": len(events),
        "contacts_created": len(contacts),
        "enquiries_created": len(enquiries),
        "duplicates_suppressed": sum(1 for r in rows
                                     if r.outcome == ROUTE_DUPLICATE),
        "rejected": sum(1 for r in rows if r.outcome == ROUTE_REJECTED),
        "rows": tuple(rows),
    }


SAMPLE_EVENTS: tuple[tuple[str, str], ...] = (
    ("aoife.kelly@example.com", "MLS-4471902"),
    ("aoife.kelly@example.com", "MLS-4471902"),
    ("aoife.kelly@example.com", "MLS-5580311"),
    ("AOIFE.KELLY@EXAMPLE.COM ", "MLS-4471902"),
    ("dermot.walsh@example.com", "MLS-4471902"),
    ("not an email", "MLS-4471902"),
)
