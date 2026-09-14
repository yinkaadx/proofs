# 10DLC Campaign Registry Compliance Validator

A2P 10DLC brand and campaign registration simulator ensuring exact TCR approval
standards.

Live at `/ten-dlc-compliance-validator` on the hub.

## What it is for

A 10DLC registration fails on details that look like nothing. A legal name that
reads the same to a person but differs from the IRS record by one full stop. A
consent page missing one sentence. A sample message with no opt out in it. Each
one is a rejection, a vetting fee spent and days of undeliverable texts while
the client asks why their campaign is not running.

So the validator says which characters are wrong, rather than handing back a
similarity score. A score tells you nothing you can act on: the person filling
in the form needs to know that the registry wants a full stop after Inc.

## The three checks

**Brand identity matcher.** Compares the submitted legal name against the EIN
record and names the specific divergence: missing or extra punctuation, a
missing or wrong entity suffix, an ampersand spelled out, a dropped leading The,
or a trade name used instead of the legal one. Capitalisation alone is accepted,
which the tool says rather than leaving you to guess. When the two names share
no distinctive word it stops reporting finer differences and says the name is
simply wrong, because a suffix note on the wrong company sends someone off to
fix a comma.

**Opt in consent scanner.** Eleven clauses: seven mandatory, two recommended and
two disqualifiers. Naming a privacy policy without linking to one fails, because
the reviewer follows the link. Paraphrasing the rates disclosure fails, because
it is checked for by name. Sharing language disqualifies the page whatever else
it says. Every clause the page is missing produces the exact sentence to paste,
and a test proves that pasting all of them produces a page that passes.

**Sample message formatter.** Opt out, brand name, HELP, public link shorteners,
restricted content and segment count. Encoding is computed from the GSM 03.38
alphabet rather than guessed, so a single emoji or a curly quote pasted from a
document correctly drops the single segment length from 160 to 70. When adding
the missing opt out pushes the message into a second segment, the page says so:
that doubles what every send costs, forever, and it is normally cheaper to
shorten the body.

## One brand through all three

The brand matched on the first tab is carried into the other two. A consent page
for one company does not register a campaign for another, and a message that
never names the sender is reported as spam by the person receiving it.

## Layout

| File | Purpose |
| --- | --- |
| `core.py` | The engine. No Streamlit import, so it can gate a real TCR submission script |
| `page.py` | The page, rendered by the hub |
| `../../tests/test_ten_dlc_compliance_validator.py` | Engine tests, 101 checks |
| `../../tests/test_ten_dlc_compliance_validator_page.py` | Page tests via AppTest, 39 checks |

## Rules worth keeping when this is extended

- Every table column holds one type, because the table widget serialises
  through Arrow and Arrow refuses a mixed column.
- Nothing on this page is stored between reruns. Everything is evaluated live
  from the controls, which removes the class of bug where a card describes an
  input that was replaced two interactions ago.
- A widget that carries a `value=` default and is also written through
  `st.session_state` is unsupported and Streamlit warns about it. The two brand
  fields are seeded once with `setdefault` and take no default.
- A clause matched case insensitively cannot also require a capital letter.
  `[A-Z]` under `IGNORECASE` is how "from us" first passed as a brand name.
