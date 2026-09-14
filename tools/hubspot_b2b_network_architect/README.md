# HubSpot B2B Network Architect

Gridwise entity relationship model, network categorization logic, and LinkedIn
deduplication ledger.

Live at `/hubspot-b2b-network-architect` on the hub.

## Three modelling decisions, each with an obvious wrong answer

**A company belongs to several Gridwise networks at once.** The obvious model
gives each network its own company record, and the portal ends up holding
Northwind Freight three times. The correct model is one company keyed on its
domain and one custom property, `gridwise_network`, of type `enumeration` with
fieldType `checkbox`, which is HubSpot's multiple checkboxes. Every network the
company belongs to lives in that one property as a semicolon delimited string.

There is a second, quieter wrong answer. A multiple checkboxes property is
written as one string, and a write that does not begin with a semicolon
replaces the whole string rather than adding to it. So an integration can hold
the record count at one, pass review, and still lose the Client Network the
moment it writes Member Network. The tool shows all three modes side by side
and the record count is the thing that moves.

**A prospect belongs to two companies at once.** They work at their employer,
which is the primary company, and they matter because of a Gridwise client they
are being worked for, which is a labelled association to a different company.
HubSpot allows many company associations per contact and exactly one primary,
so neither company has to be duplicated to hold both facts.

Associating a primary company writes two association rows, not one: the
unlabeled `contact_to_company` and `contact_to_company_primary`. An association
typeId is unique only inside its category, so a USER_DEFINED label can carry the
same number as a HubSpot defined type. In the simulated portal HUBSPOT_DEFINED 1
is the primary marker and USER_DEFINED 1 is `Target For`, which is why the
integration resolves labels by name at start up rather than carrying a number
in its source. Turn the hardcode toggle on and watch the label land on the
wrong company without an error.

**A LinkedIn sync arrives carrying every message ever sent.** Hublead and Surfe
both pull the entire existing conversation the first time a rep syncs it, not
only what arrives afterwards. Written straight to the timeline that reads as a
burst of fresh activity on a day when nobody spoke to anybody, every one of them
enrols the contact in whatever workflow watches for a LinkedIn reply, and last
touch dates jump to today across the database.

The ledger fingerprints each message as a sha256 over the conversation id, the
message id, the send time and the body with whitespace collapsed, then applies
two rules in this order: a fingerprint already in the ledger is never written
again whatever its date, and anything sent before the connection is recorded
without being written. Collapsing whitespace is what makes the second delivery
match, because a resync returns the same message with different line endings.

## The HubSpot behaviour encoded here

Verified against HubSpot's documentation and community answers rather than
recalled, before any of this was written:

- A contact given a primary company produces two association rows, typeId 279
  (`contact_to_company`) and typeId 1 (`contact_to_company_primary`).
- A contact may hold many company associations and exactly one primary.
- An association typeId is unique only within its category, so custom labels
  are read from `/crm/v4/associations/contacts/companies/labels` at runtime.
- A multiple checkboxes property stores its values as one semicolon delimited
  string, and a value beginning with a semicolon appends rather than replaces.
- Companies are deduplicated on the company domain, which is why the
  integration searches on a normalised domain before it posts.

## Layout

- `core.py` holds the engine. No Streamlit import, so it is unit testable on
  its own and could sit behind the real integration. Deterministic: nothing
  reads the clock or a random source, and every timestamp arrives as an
  argument, which is why `stamp()` renders times relative to the connection
  rather than in the machine's own zone.
- `page.py` draws it with `shared/theme.py`. Everything is evaluated live from
  the controls, with no session state to go stale.

## Tests

    pytest tests/test_hubspot_b2b_network_architect.py \
           tests/test_hubspot_b2b_network_architect_page.py

103 engine checks and 44 page checks. Nothing here touches a live portal: no
HubSpot token is held, no record or association is written, and no LinkedIn
account is read.
