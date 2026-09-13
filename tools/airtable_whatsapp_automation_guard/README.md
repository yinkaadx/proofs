# Airtable WhatsApp Automation Guard

Idempotent Make webhook simulator, Twilio payload router, and error handling
guard.

Live at `/airtable-whatsapp-automation-guard` on the hub.

## The three failures this exists for

A record changes in Airtable, Make picks it up, a WhatsApp message goes out. It
breaks in the same three ways every time, and all three are silent until a
customer complains.

**A retry sends the message twice.** Make retries a scenario that timed out
after it had already succeeded. Without a ledger the customer gets the same
reminder again.

**One bad phone number stops the whole run.** The Phone Number field is free
text, so a real base holds local numbers, brackets, double zero prefixes and the
occasional word. A module that throws on the first one leaves every record after
it unprocessed.

**A message outside the twenty four hour window is rejected and retried
forever.** Twilio returns 63016, which a scenario treats as a generic failure
and queues again, spending an operation on each attempt.

## How each is handled

**Idempotency.** The key is the record id and a digest of the message together.
The record id alone would suppress a genuinely different second message, and the
body alone would collide between two records saying the same thing. A send that
failed is not recorded, so a retry after a real failure still goes out.

**Phone normalisation.** Every shape a person types is converted to E.164 before
Twilio sees it, and anything that cannot be is refused with the reason and a
note saying whether retrying would help. The national part is checked on its
own: prefixing the country code pushes a truncated six digit fragment over the
global minimum, which is how a broken number would otherwise pass.

**The window.** Inside it the request carries a free form Body. Outside it the
router sends ContentSid and ContentVariables for an approved template and omits
Body entirely, rather than sending something Twilio ignores.

**Errors.** Five Twilio codes, each classified as retryable or not. Only the
rate limit is worth retrying, and it is held on a queue rather than raised as an
alert, because it clears itself. The other four are alerts with the fix stated,
since retrying them fails identically every time.

**Nothing raises.** `process_record` catches everything, including an unforeseen
error inside the formatter, and returns a result so the batch carries on. A test
proves it with a template that throws.

## Layout

| File | Purpose |
| --- | --- |
| `core.py` | The engine. No Streamlit import, so it can sit behind the real Make webhook |
| `page.py` | The page, rendered by the hub |
| `../../tests/test_airtable_whatsapp_automation_guard.py` | Engine tests, 64 checks |
| `../../tests/test_airtable_whatsapp_automation_guard_page.py` | Page tests via AppTest, 34 checks |

## Placeholders

The Account SID and Content SID are written as Twilio's own documented
placeholder shape, `AC` followed by X characters rather than hex. A realistic
looking value is exactly what a real Account SID is, and GitHub's push
protection refuses a commit carrying one. It is right to: nothing in a
repository should look like a credential, even when it is not one. This was
caught by a blocked push during the build, and fixed by changing the value
rather than by clicking the link that allows it through.

## Rules worth keeping when this is extended

- Every table column holds one type, because the table widget serialises
  through Arrow and Arrow refuses a mixed column.
- Callbacks read live widget state out of `st.session_state` rather than
  arguments bound at render time, which are one interaction stale.
- Both Twilio addresses carry the `whatsapp:` prefix. Leaving it off the To
  address silently sends an SMS instead, at a different price and possibly to a
  phone the customer does not use for WhatsApp.
