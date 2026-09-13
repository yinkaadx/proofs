"""Gridwise B2B network architecture engine.

Three modelling decisions decide whether a HubSpot portal that has to hold a
network of networks stays clean or turns into a duplicate farm within a
quarter:

  A company belongs to more than one Gridwise network at once, and the naive
  model gives each network its own company record. A prospect works at one
  company and is a target at another, and the naive model picks one and loses
  the other. A LinkedIn sync backfills the entire message history on first
  connect, and the naive model logs every historical message as a fresh
  engagement.

Each one is modelled here against HubSpot's real behaviour, with the write log
the portal would receive, so the difference between the clean model and the
duplicate farm is visible as a record count rather than as an opinion.

Pure logic, no Streamlit import, so this is unit testable on its own and can be
reused behind a real integration. Deterministic: nothing reads the clock or a
random source, and every timestamp arrives as an argument.

The HubSpot behaviour encoded here was verified against HubSpot's own
documentation and community answers rather than recalled:

  A contact associated with a primary company produces TWO association rows,
  the unlabeled contact_to_company (typeId 279) and contact_to_company_primary
  (typeId 1). A contact may hold many company associations and exactly one of
  them is primary.

  An association typeId is only unique within its category. A USER_DEFINED
  label can carry the same number as a HUBSPOT_DEFINED type, so a bare typeId
  is ambiguous and labels are resolved from the labels endpoint at runtime.

  A multiple checkboxes property stores its values as one semicolon delimited
  string. A write whose value starts with a semicolon appends to what is
  already there; a write without one replaces it.

  Companies are deduplicated on the company domain. Domain is the identifier
  the integration has to search on, because a second POST is a second record.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

ENGINE_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# Findings, shared by all three parts
# ---------------------------------------------------------------------------

SEVERITY_CRITICAL = "Critical"
SEVERITY_HIGH = "High"
SEVERITY_OK = "Healthy"


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    title: str
    detail: str
    fix: str


CODE_CLEAN = "NO_FINDINGS"


# ---------------------------------------------------------------------------
# Part one: Gridwise network categorization
# ---------------------------------------------------------------------------

NETWORK_CLIENT = "client_network"
NETWORK_MEMBER = "member_network"
NETWORK_RELATIONSHIP = "relationship_network"


@dataclass(frozen=True)
class NetworkOption:
    value: str
    label: str
    meaning: str


NETWORKS: tuple[NetworkOption, ...] = (
    NetworkOption(
        NETWORK_CLIENT, "Client Network",
        "Gridwise holds a paid agreement with this company."),
    NetworkOption(
        NETWORK_MEMBER, "Member Network",
        "People at this company hold Gridwise memberships."),
    NetworkOption(
        NETWORK_RELATIONSHIP, "Relationship Network",
        "Known to Gridwise, introduced or referred, nothing signed."),
)

NETWORK_VALUES: tuple[str, ...] = tuple(n.value for n in NETWORKS)
NETWORK_LABELS: dict[str, str] = {n.value: n.label for n in NETWORKS}

# The custom property that carries the categorization. One property of type
# enumeration with fieldType checkbox, which is HubSpot's multiple checkboxes,
# rather than one company record per network or one pipeline per network.
PROPERTY_NAME = "gridwise_network"
PROPERTY_TYPE = "enumeration"
PROPERTY_FIELD_TYPE = "checkbox"
DELIMITER = ";"

# How the integration writes the property.
MODE_SEARCH_APPEND = "Search on domain, then append"
MODE_SEARCH_REPLACE = "Search on domain, then replace"
MODE_BLIND_CREATE = "Create a company for every assignment"

MODES: tuple[str, ...] = (MODE_SEARCH_APPEND, MODE_SEARCH_REPLACE,
                          MODE_BLIND_CREATE)

OUTCOME_CREATED = "Record created"
OUTCOME_UPDATED = "Record updated"
OUTCOME_UNCHANGED = "Already categorised, no write"
OUTCOME_DUPLICATE = "Duplicate record created"
OUTCOME_REPLACED = "Earlier networks overwritten"

CODE_DUPLICATE = "DUP_COMPANY"
CODE_REPLACED = "VALUES_REPLACED"
CODE_DOMAIN_VARIANT = "DOMAIN_VARIANT"

SEARCH_ENDPOINT = "POST /crm/v3/objects/companies/search"
CREATE_ENDPOINT = "POST /crm/v3/objects/companies"


def normalise_domain(raw: str) -> str:
    """Reduce anything a rep might paste to the domain HubSpot dedupes on.

    A duplicate rarely arrives as an obvious second copy. It arrives as
    https://www.gridwise.io/pricing on a Tuesday when gridwise.io is already in
    the portal, and the search that was supposed to find the existing record
    misses by a prefix.
    """
    text = (raw or "").strip().lower()
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text)
    text = text.split("/", 1)[0]
    text = text.split("?", 1)[0]
    text = text.split("#", 1)[0]
    text = text.split("@")[-1]          # an address pasted instead of a site
    text = text.split(":", 1)[0]        # a port
    text = text.rstrip(".")
    if text.startswith("www."):
        text = text[4:]
    return text


@dataclass(frozen=True)
class CompanySeed:
    """A company as it arrives, before the portal has an opinion about it."""
    name: str
    website: str
    industry: str
    headcount: int


SAMPLE_COMPANIES: tuple[CompanySeed, ...] = (
    CompanySeed("Northwind Freight", "https://www.northwindfreight.com",
                "Logistics", 420),
    CompanySeed("Halyard Capital Partners", "halyardcapital.co",
                "Private equity", 90),
    CompanySeed("Beacon Health Group", "http://beaconhealth.group/about",
                "Healthcare", 1250),
    CompanySeed("Torsion Robotics", "www.torsionrobotics.ai",
                "Industrial robotics", 160),
    CompanySeed("Meridian Grid Services", "meridiangrid.energy",
                "Utilities", 780),
)


@dataclass
class CompanyRecord:
    """One company as the portal holds it."""
    record_id: str
    name: str
    domain: str
    networks: tuple[str, ...] = ()

    @property
    def property_value(self) -> str:
        """What the multiple checkboxes property actually stores."""
        return DELIMITER.join(self.networks)

    @property
    def network_labels(self) -> tuple[str, ...]:
        return tuple(NETWORK_LABELS.get(v, v) for v in self.networks)


@dataclass(frozen=True)
class Write:
    """One request the integration sent, and what the portal did with it."""
    step: int
    verb: str
    endpoint: str
    body: dict
    outcome: str
    record_id: str
    note: str

    @property
    def pretty_body(self) -> str:
        return json.dumps(self.body, indent=2)


@dataclass
class Portal:
    """The simulated HubSpot portal: records, and every write it received."""
    records: list[CompanyRecord] = field(default_factory=list)
    writes: list[Write] = field(default_factory=list)
    next_id: int = 8001

    def find_by_domain(self, domain: str) -> CompanyRecord | None:
        for record in self.records:
            if record.domain == domain:
                return record
        return None

    def all_domains(self) -> list[str]:
        return [record.domain for record in self.records]

    @property
    def duplicate_domains(self) -> list[str]:
        seen: dict[str, int] = {}
        for record in self.records:
            seen[record.domain] = seen.get(record.domain, 0) + 1
        return sorted(domain for domain, count in seen.items() if count > 1)

    @property
    def duplicate_count(self) -> int:
        """Records beyond the first for any domain."""
        seen: dict[str, int] = {}
        for record in self.records:
            seen[record.domain] = seen.get(record.domain, 0) + 1
        return sum(count - 1 for count in seen.values() if count > 1)

    def rows(self) -> list[dict]:
        return [
            {
                "Record ID": record.record_id,
                "Company": record.name,
                "Domain": record.domain,
                PROPERTY_NAME: record.property_value or "(empty)",
                "Networks": str(len(record.networks)),
            }
            for record in self.records
        ]

    def write_rows(self) -> list[dict]:
        return [
            {
                "Step": str(write.step),
                "Request": f"{write.verb} {write.endpoint.split(' ', 1)[-1]}",
                "Outcome": write.outcome,
                "Record ID": write.record_id,
                "Note": write.note,
            }
            for write in self.writes
        ]


def _patch_endpoint(record_id: str) -> str:
    return f"PATCH /crm/v3/objects/companies/{record_id}"


def assign_network(portal: Portal, seed: CompanySeed, network: str,
                   mode: str = MODE_SEARCH_APPEND,
                   normalise: bool = True) -> Write:
    """Put one company into one Gridwise network and return the write.

    The three modes are the three things an integration actually does, not
    three hypotheticals. Blind create is what a first pass does because it is
    one call. Search then replace is what a careful second pass does, and it
    keeps the record count right while quietly dropping the network the
    company was already in. Search then append is the only one that holds.
    """
    if network not in NETWORK_VALUES:
        raise ValueError(f"unknown network {network!r}")
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")

    domain = (normalise_domain(seed.website) if normalise
              else (seed.website or "").strip().lower())
    step = len(portal.writes) + 1

    existing = None if mode == MODE_BLIND_CREATE else portal.find_by_domain(domain)

    if existing is None:
        record = CompanyRecord(record_id=str(portal.next_id), name=seed.name,
                               domain=domain, networks=(network,))
        portal.next_id += 1
        duplicate = any(other.domain == domain for other in portal.records)
        portal.records.append(record)
        write = Write(
            step=step, verb="POST", endpoint=CREATE_ENDPOINT,
            body={"properties": {"name": seed.name, "domain": domain,
                                 PROPERTY_NAME: network}},
            outcome=OUTCOME_DUPLICATE if duplicate else OUTCOME_CREATED,
            record_id=record.record_id,
            note=(f"A record for {domain} already existed. This is a second "
                  f"one." if duplicate else
                  f"No record matched {domain}, so one was created."),
        )
        portal.writes.append(write)
        return write

    if network in existing.networks and mode != MODE_SEARCH_REPLACE:
        write = Write(
            step=step, verb="GET", endpoint=SEARCH_ENDPOINT,
            body={"filters": [{"propertyName": "domain", "operator": "EQ",
                               "value": domain}]},
            outcome=OUTCOME_UNCHANGED, record_id=existing.record_id,
            note=(f"{existing.record_id} is already in "
                  f"{NETWORK_LABELS[network]}, so nothing was written."),
        )
        portal.writes.append(write)
        return write

    if mode == MODE_SEARCH_REPLACE:
        lost = tuple(v for v in existing.networks if v != network)
        existing.networks = (network,)
        value = network
        outcome = OUTCOME_REPLACED if lost else OUTCOME_UPDATED
        note = (
            f"The value was sent without a leading semicolon, so it replaced "
            f"{', '.join(NETWORK_LABELS[v] for v in lost)} rather than "
            f"joining it." if lost else
            f"{existing.record_id} moved into {NETWORK_LABELS[network]}."
        )
    else:
        existing.networks = existing.networks + (network,)
        value = DELIMITER + network
        outcome = OUTCOME_UPDATED
        note = (f"The leading semicolon appends, so {existing.record_id} is "
                f"now in {len(existing.networks)} networks on one record.")

    write = Write(
        step=step, verb="PATCH", endpoint=_patch_endpoint(existing.record_id),
        body={"properties": {PROPERTY_NAME: value}},
        outcome=outcome, record_id=existing.record_id, note=note,
    )
    portal.writes.append(write)
    return write


def categorise(seed: CompanySeed, networks, mode: str = MODE_SEARCH_APPEND,
               normalise: bool = True, portal: Portal | None = None) -> Portal:
    """Assign one company to several networks in order and return the portal."""
    portal = portal if portal is not None else Portal()
    for network in networks:
        assign_network(portal, seed, network, mode=mode, normalise=normalise)
    return portal


def audit_portal(portal: Portal, requested: int = 0) -> list[Finding]:
    """What went wrong, named the way the portal would show it."""
    findings: list[Finding] = []

    if portal.duplicate_count:
        findings.append(Finding(
            code=CODE_DUPLICATE, severity=SEVERITY_CRITICAL,
            title=(f"{portal.duplicate_count} duplicate company "
                   f"record(s) for {', '.join(portal.duplicate_domains)}"),
            detail=("Every assignment posted a new company instead of "
                    "searching for the domain first. The reports now count "
                    "the same company once per network, and an owner looking "
                    "at one record cannot see the deals on the others."),
            fix=(f"Search {SEARCH_ENDPOINT} on domain EQ the normalised "
                 f"domain, then PATCH the record it returns. Only POST when "
                 f"the search comes back empty."),
        ))

    replaced = [w for w in portal.writes if w.outcome == OUTCOME_REPLACED]
    if replaced:
        findings.append(Finding(
            code=CODE_REPLACED, severity=SEVERITY_HIGH,
            title=f"{len(replaced)} write(s) overwrote an earlier network",
            detail=("The record count stayed right, which is why this one "
                    "survives review. A multiple checkboxes property holds "
                    "its values as one semicolon delimited string, and a "
                    "write that does not begin with a semicolon replaces the "
                    "whole string rather than adding to it."),
            fix=(f'Send "{DELIMITER}client_network" rather than '
                 f'"client_network". The leading delimiter is what makes the '
                 f'write an append.'),
        ))

    if requested and len(portal.records) == 1 and not replaced:
        record = portal.records[0]
        findings.append(Finding(
            code=CODE_CLEAN, severity=SEVERITY_OK,
            title=(f"One record, {len(record.networks)} network(s), "
                   f"{requested} assignment(s)"),
            detail=(f"{record.record_id} carries "
                    f"{record.property_value or '(empty)'} in "
                    f"{PROPERTY_NAME}. A company in three networks is one row "
                    f"in a report, not three."),
            fix=("Keep the categorization on the company itself. A network "
                 "is a property of the relationship, not a separate company."),
        ))

    return findings


def domain_variants(seed: CompanySeed) -> list[dict]:
    """The same company as five people would paste it."""
    base = normalise_domain(seed.website)
    pasted = [
        seed.website,
        f"https://www.{base}/pricing",
        f"WWW.{base.upper()}",
        f"hello@{base}",
        f"http://{base}:443/",
    ]
    return [
        {"Pasted": value, "Normalised": normalise_domain(value),
         "Matches": "yes" if normalise_domain(value) == base else "no"}
        for value in pasted
    ]


# ---------------------------------------------------------------------------
# Part two: the prospect association map
# ---------------------------------------------------------------------------

CATEGORY_HUBSPOT = "HUBSPOT_DEFINED"
CATEGORY_USER = "USER_DEFINED"

# Verified HubSpot defined types. A contact given a primary company gets both
# of these, not one: the unlabeled association and the primary marker.
TYPE_CONTACT_TO_COMPANY = 279
TYPE_CONTACT_TO_COMPANY_PRIMARY = 1

LABEL_TARGET_FOR = "Target For"
LABEL_INTRODUCED_BY = "Introduced By"

LABELS_ENDPOINT = "GET /crm/v4/associations/contacts/companies/labels"
ASSOCIATE_ENDPOINT = ("PUT /crm/v4/objects/contacts/{contactId}/associations/"
                      "companies/{companyId}")

CODE_AMBIGUOUS_TYPE = "TYPEID_AMBIGUOUS"
CODE_PRIMARY_LOST = "PRIMARY_OVERWRITTEN"
CODE_MAP_CLEAN = "ASSOCIATIONS_CLEAN"


@dataclass(frozen=True)
class AssociationType:
    type_id: int
    category: str
    label: str

    @property
    def descriptor(self) -> str:
        return f"{self.category} {self.type_id}"


# The portal's own label table, as the labels endpoint would return it. The
# custom labels carry small numbers on purpose: that is what a real portal
# returns, and it is why a bare typeId is not enough to identify a type.
PORTAL_LABELS: tuple[AssociationType, ...] = (
    AssociationType(TYPE_CONTACT_TO_COMPANY, CATEGORY_HUBSPOT, ""),
    AssociationType(TYPE_CONTACT_TO_COMPANY_PRIMARY, CATEGORY_HUBSPOT,
                    "Primary"),
    AssociationType(1, CATEGORY_USER, LABEL_TARGET_FOR),
    AssociationType(2, CATEGORY_USER, LABEL_INTRODUCED_BY),
)


def resolve_label(name: str,
                  labels: tuple[AssociationType, ...] = PORTAL_LABELS
                  ) -> AssociationType:
    """Look a custom label up the way the integration has to, by name.

    A typeId is unique only inside its category, so the USER_DEFINED label
    Target For sits on typeId 1 in this portal while HUBSPOT_DEFINED 1 is the
    primary company marker. Hardcoding 1 therefore writes the wrong thing, and
    it writes it silently.
    """
    for label in labels:
        if label.category == CATEGORY_USER and label.label == name:
            return label
    raise KeyError(f"no user defined label named {name!r}")


@dataclass(frozen=True)
class Party:
    record_id: str
    name: str
    detail: str


@dataclass(frozen=True)
class AssociationRow:
    from_party: Party
    to_party: Party
    association: AssociationType
    reason: str


PROSPECT = Party("C-4417", "Dara Mensah",
                 "Head of Fleet Operations, the person Gridwise is selling to")
EMPLOYER = Party("8001", "Northwind Freight",
                 "Where the prospect actually works, the primary company")
GRIDWISE_CLIENT = Party("8044", "Halyard Capital Partners",
                        "A Gridwise client, the reason this prospect matters")


def build_association_rows(prospect: Party = PROSPECT,
                           employer: Party = EMPLOYER,
                           client: Party = GRIDWISE_CLIENT,
                           label_name: str = LABEL_TARGET_FOR,
                           hardcode_type_id: bool = False,
                           ) -> list[AssociationRow]:
    """Every association row the portal ends up holding.

    Three rows, not two. Associating a primary company writes the unlabeled
    association and the primary marker, which is why a count of associations
    never matches a count of companies.
    """
    rows = [
        AssociationRow(
            prospect, employer,
            AssociationType(TYPE_CONTACT_TO_COMPANY, CATEGORY_HUBSPOT, ""),
            "The unlabeled association. Written for every company link."),
        AssociationRow(
            prospect, employer,
            AssociationType(TYPE_CONTACT_TO_COMPANY_PRIMARY, CATEGORY_HUBSPOT,
                            "Primary"),
            "The primary marker. One company per contact may hold it."),
    ]

    if hardcode_type_id:
        wrong = AssociationType(1, CATEGORY_HUBSPOT, "Primary")
        rows.append(AssociationRow(
            prospect, client, wrong,
            "Meant to be Target For. The integration sent typeId 1 without a "
            "category, so the portal read the HubSpot defined primary marker."))
    else:
        rows.append(AssociationRow(
            prospect, client, resolve_label(label_name),
            f"{label_name}. The prospect does not work here. This company is "
            f"the Gridwise client the prospect is being worked for."))

    return rows


@dataclass
class ProspectMap:
    rows: list[AssociationRow]
    findings: list[Finding]
    hardcoded: bool

    @property
    def primary_company_ids(self) -> list[str]:
        return [row.to_party.record_id for row in self.rows
                if row.association.category == CATEGORY_HUBSPOT
                and row.association.type_id == TYPE_CONTACT_TO_COMPANY_PRIMARY]

    @property
    def labelled_rows(self) -> list[AssociationRow]:
        return [row for row in self.rows
                if row.association.category == CATEGORY_USER]

    def table_rows(self) -> list[dict]:
        return [
            {
                "From": f"{row.from_party.name} ({row.from_party.record_id})",
                "To": f"{row.to_party.name} ({row.to_party.record_id})",
                "Category": row.association.category,
                "typeId": str(row.association.type_id),
                "Label": row.association.label or "(none)",
            }
            for row in self.rows
        ]

    def payload(self) -> dict:
        """The v4 request body, as the integration would send it."""
        return {
            "inputs": [
                {
                    "from": {"id": row.from_party.record_id},
                    "to": {"id": row.to_party.record_id},
                    "types": [{"associationCategory": row.association.category,
                               "associationTypeId": row.association.type_id}],
                }
                for row in self.rows
            ]
        }

    def pretty_payload(self) -> str:
        return json.dumps(self.payload(), indent=2)


def map_prospect(prospect: Party = PROSPECT, employer: Party = EMPLOYER,
                 client: Party = GRIDWISE_CLIENT,
                 label_name: str = LABEL_TARGET_FOR,
                 hardcode_type_id: bool = False) -> ProspectMap:
    """Associate a prospect with their employer and with a Gridwise client."""
    rows = build_association_rows(prospect, employer, client, label_name,
                                  hardcode_type_id)
    findings: list[Finding] = []

    primaries = [row for row in rows
                 if row.association.category == CATEGORY_HUBSPOT
                 and row.association.type_id == TYPE_CONTACT_TO_COMPANY_PRIMARY]

    if hardcode_type_id:
        findings.append(Finding(
            code=CODE_AMBIGUOUS_TYPE, severity=SEVERITY_CRITICAL,
            title=f"typeId 1 was sent without resolving {label_name}",
            detail=(f"A typeId is unique only within its category. In this "
                    f"portal HUBSPOT_DEFINED 1 is the primary company marker "
                    f"and USER_DEFINED 1 is {label_name}. Sending the number "
                    f"alone wrote the marker."),
            fix=(f"Call {LABELS_ENDPOINT} once at start up, find the "
                 f"USER_DEFINED entry named {label_name}, and send its "
                 f"associationCategory alongside its associationTypeId."),
        ))
    if len(primaries) > 1:
        findings.append(Finding(
            code=CODE_PRIMARY_LOST, severity=SEVERITY_CRITICAL,
            title=f"{len(primaries)} companies are marked primary",
            detail=("A contact may hold many company associations and exactly "
                    "one primary. A second primary write moves the marker and "
                    "the first company silently stops being the employer."),
            fix=("Write the labelled association to the client and leave the "
                 "primary marker on the employer."),
        ))
    if not findings:
        findings.append(Finding(
            code=CODE_MAP_CLEAN, severity=SEVERITY_OK,
            title=(f"{len(rows)} association rows, one primary, "
                   f"one {label_name} label"),
            detail=(f"{prospect.name} stays employed by {employer.name} and "
                    f"is readable from {client.name} as a target. Neither "
                    f"company had to be duplicated to say so."),
            fix=("Keep the relationship on the association rather than on a "
                 "property, so it can be reported from either end."),
        ))

    return ProspectMap(rows=rows, findings=findings, hardcoded=hardcode_type_id)


# ---------------------------------------------------------------------------
# Part three: the LinkedIn deduplication ledger
# ---------------------------------------------------------------------------

SOURCE_HUBLEAD = "Hublead"
SOURCE_SURFE = "Surfe"
SOURCES: tuple[str, ...] = (SOURCE_HUBLEAD, SOURCE_SURFE)

LOGGED = "Logged as a LinkedIn activity"
SKIPPED_SEEN = "Skipped, fingerprint already in the ledger"
SKIPPED_HISTORICAL = "Skipped, older than the connection"

CODE_BACKFILL = "HISTORY_BACKFILL"
CODE_REDELIVERY = "REDELIVERED"
CODE_LEDGER_CLEAN = "LEDGER_CLEAN"

# A connection made at a definite second, so a test and a run agree.
CONNECTED_AT = 1_789_000_000
HOUR = 3600
DAY = 86_400


@dataclass(frozen=True)
class LinkedInMessage:
    message_urn: str
    conversation_urn: str
    sender: str
    recipient: str
    sent_at: int
    body: str

    @property
    def outbound(self) -> bool:
        return self.sender == "Gridwise"


def canonical(message: LinkedInMessage) -> str:
    """The exact string that gets hashed.

    Whitespace is collapsed because the same message comes back from a second
    sync with different line endings, and a fingerprint that changes with a
    carriage return is a fingerprint that lets the duplicate through. The
    conversation, the message id and the send time are all in the string so
    two different messages with identical text still hash apart.
    """
    body = re.sub(r"\s+", " ", (message.body or "")).strip()
    return "|".join([message.conversation_urn, message.message_urn,
                     str(message.sent_at), body])


def fingerprint(message: LinkedInMessage) -> str:
    """A stable sha256 of the canonical string, hex, first sixteen characters.

    Sixteen hex characters is sixty four bits. At the volume one sales team
    produces that is far past the point where a collision is worth modelling,
    and it stays readable in a table, which matters because somebody has to be
    able to match a row on screen to a row in the portal.
    """
    return hashlib.sha256(canonical(message).encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class LedgerResult:
    message: LinkedInMessage
    outcome: str
    digest: str
    detail: str

    @property
    def logged(self) -> bool:
        return self.outcome == LOGGED


@dataclass
class Ledger:
    """Which fingerprints this portal has already seen."""
    seen: set = field(default_factory=set)
    results: list = field(default_factory=list)
    source: str = SOURCE_HUBLEAD

    def count(self, outcome: str) -> int:
        return sum(1 for result in self.results if result.outcome == outcome)

    @property
    def logged(self) -> int:
        return self.count(LOGGED)

    @property
    def skipped(self) -> int:
        return len(self.results) - self.logged

    @property
    def workflow_triggers(self) -> int:
        """One enrolment per logged activity. Skips cost nothing."""
        return self.logged

    def rows(self) -> list[dict]:
        return [
            {
                "Sent": stamp(result.message.sent_at),
                "Direction": ("Gridwise" if result.message.outbound
                              else result.message.sender),
                "Message": result.message.message_urn,
                "Fingerprint": result.digest,
                "Outcome": result.outcome,
            }
            for result in self.results
        ]


def stamp(epoch: int) -> str:
    """A readable, timezone free stamp relative to the connection moment.

    Deliberately not a wall clock date. The engine takes every time as an
    argument so a test and a run agree, and a date rendered from the machine's
    own zone would have broken that at the last step.
    """
    delta = epoch - CONNECTED_AT
    if delta == 0:
        return "at connection"
    sign = "after" if delta > 0 else "before"
    seconds = abs(delta)
    if seconds >= DAY:
        return f"{seconds // DAY}d {sign} connection"
    if seconds >= HOUR:
        return f"{seconds // HOUR}h {sign} connection"
    return f"{seconds // 60}m {sign} connection"


def process_message(ledger: Ledger, message: LinkedInMessage,
                    connected_at: int = CONNECTED_AT,
                    skip_history: bool = True) -> LedgerResult:
    """Decide what happens to one incoming message, and record it.

    The order matters. The fingerprint is checked first, because a message
    that has already been logged must not be logged again whatever its date.
    The history cutoff is second, because it is a policy about the first sync
    rather than a fact about the message.
    """
    digest = fingerprint(message)

    if digest in ledger.seen:
        result = LedgerResult(
            message, SKIPPED_SEEN, digest,
            "This exact message is already on the timeline. The payload was "
            "delivered again, which is what a reconnect does.")
    elif skip_history and message.sent_at < connected_at:
        ledger.seen.add(digest)
        result = LedgerResult(
            message, SKIPPED_HISTORICAL, digest,
            "Sent before the integration was connected. It is recorded in the "
            "ledger so a later delivery is recognised, and it is not written "
            "to the timeline.")
    else:
        ledger.seen.add(digest)
        result = LedgerResult(
            message, LOGGED, digest,
            "New since the connection and not seen before, so it is written "
            "as a LinkedIn activity on the contact.")

    ledger.results.append(result)
    return result


def process_payload(messages, ledger: Ledger | None = None,
                    connected_at: int = CONNECTED_AT,
                    skip_history: bool = True,
                    source: str = SOURCE_HUBLEAD) -> Ledger:
    """Run a whole incoming payload through the ledger."""
    ledger = ledger if ledger is not None else Ledger(source=source)
    ledger.source = source
    for message in messages:
        process_message(ledger, message, connected_at, skip_history)
    return ledger


def sample_messages(connected_at: int = CONNECTED_AT) -> list[LinkedInMessage]:
    """A payload with every case in it that the ledger has to survive.

    Two historical messages from the backfill, two genuinely new ones, the
    same new message delivered twice, and the same new message again with the
    line endings a second sync gives it.
    """
    conversation = "urn:li:conversation:2-Yzk4MTc"
    first = LinkedInMessage(
        "urn:li:message:6611", conversation, "Dara Mensah", "Gridwise",
        connected_at - 14 * DAY,
        "Thanks for the intro at the fleet summit, happy to keep talking.")
    second = LinkedInMessage(
        "urn:li:message:6612", conversation, "Gridwise", "Dara Mensah",
        connected_at - 9 * DAY,
        "Good to meet you. Sending the grid utilisation deck this week.")
    third = LinkedInMessage(
        "urn:li:message:6613", conversation, "Dara Mensah", "Gridwise",
        connected_at + 2 * HOUR,
        "Deck landed, thank you. Halyard asked for the same numbers.")
    fourth = LinkedInMessage(
        "urn:li:message:6614", conversation, "Gridwise", "Dara Mensah",
        connected_at + 3 * DAY,
        "Happy to run it for Halyard too. Does Thursday work for a call?")
    redelivered = LinkedInMessage(
        third.message_urn, third.conversation_urn, third.sender,
        third.recipient, third.sent_at, third.body)
    rewrapped = LinkedInMessage(
        fourth.message_urn, fourth.conversation_urn, fourth.sender,
        fourth.recipient, fourth.sent_at,
        "Happy to run it for Halyard too.\r\n  Does Thursday work for a call?")
    return [first, second, third, fourth, redelivered, rewrapped]


def payload_json(messages, source: str = SOURCE_HUBLEAD,
                 connected_at: int = CONNECTED_AT) -> str:
    """The incoming webhook body, in the shape these integrations post."""
    return json.dumps({
        "source": source,
        "connectedAt": connected_at,
        "conversation": messages[0].conversation_urn if messages else "",
        "messages": [
            {
                "id": message.message_urn,
                "conversationId": message.conversation_urn,
                "from": message.sender,
                "to": message.recipient,
                "sentAt": message.sent_at,
                "body": message.body,
            }
            for message in messages
        ],
    }, indent=2)


def audit_ledger(ledger: Ledger, skip_history: bool = True) -> list[Finding]:
    """What the ledger prevented, or what it let through."""
    findings: list[Finding] = []
    historical = ledger.count(SKIPPED_HISTORICAL)
    repeats = ledger.count(SKIPPED_SEEN)

    if not skip_history:
        backfilled = sum(1 for result in ledger.results
                         if result.logged
                         and result.message.sent_at < CONNECTED_AT)
        if backfilled:
            findings.append(Finding(
                code=CODE_BACKFILL, severity=SEVERITY_CRITICAL,
                title=f"{backfilled} historical message(s) logged as new",
                detail=("These integrations pull the entire existing "
                        "conversation on first connect, not only what arrives "
                        "afterwards. Written straight to the timeline they "
                        "read as a burst of fresh activity, every one of them "
                        "enrols the contact in whatever workflow watches for "
                        "a LinkedIn reply, and last touch dates jump to "
                        "today across the whole database."),
                fix=("Keep the cutoff. Record the fingerprint so a later "
                     "delivery is recognised, and do not write the activity."),
            ))
    if repeats:
        findings.append(Finding(
            code=CODE_REDELIVERY, severity=SEVERITY_OK,
            title=f"{repeats} redelivered message(s) refused",
            detail=("The same conversation arrived twice, once byte for byte "
                    "and once with the line endings a second sync gives it. "
                    "The fingerprint collapses whitespace before hashing, so "
                    "both matched what was already recorded."),
            fix=("Hash the conversation id, the message id, the send time and "
                 "the collapsed body. A hash over the raw body alone changes "
                 "with a carriage return."),
        ))
    if historical and skip_history:
        findings.append(Finding(
            code=CODE_LEDGER_CLEAN, severity=SEVERITY_OK,
            title=(f"{ledger.logged} activity(s) written, "
                   f"{ledger.skipped} skipped"),
            detail=(f"{historical} message(s) predate the connection and were "
                    f"recorded without being written. The timeline shows the "
                    f"conversation from the day Gridwise connected, which is "
                    f"the only date anyone can defend."),
            fix=("Load the ledger before the first sync, not after the first "
                 "complaint."),
        ))
    return findings


def ledger_comparison(messages, connected_at: int = CONNECTED_AT) -> list[dict]:
    """The same payload with the cutoff on and off, side by side."""
    guarded = process_payload(messages, connected_at=connected_at,
                              skip_history=True)
    unguarded = process_payload(messages, connected_at=connected_at,
                                skip_history=False)
    return [
        {"Measure": "Activities written to the timeline",
         "Ledger on": str(guarded.logged), "Ledger off": str(unguarded.logged)},
        {"Measure": "Messages skipped",
         "Ledger on": str(guarded.skipped),
         "Ledger off": str(unguarded.skipped)},
        {"Measure": "Workflow enrolments triggered",
         "Ledger on": str(guarded.workflow_triggers),
         "Ledger off": str(unguarded.workflow_triggers)},
        {"Measure": "Messages received",
         "Ledger on": str(len(guarded.results)),
         "Ledger off": str(len(unguarded.results))},
    ]
