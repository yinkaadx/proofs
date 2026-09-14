# Pipedrive API & Integration Console

A console for the Pipedrive integration: a stage change calls Sinch directly,
inbound replies are answered against the person and deal record and handed to a
sales rep the moment the customer asks for one, a STOP keyword unsubscribes the
person and blocks every later dispatch, and the whole pipeline exports to Power
BI with a change hash per row.

Live at `/pipedrive-integration-engine` on the hub.

## The four problems it answers

**Why is Zapier in the path at all?** It does not need to be. Pipedrive posts a
webhook the moment a deal stage is written, and the endpoint that receives it
can call the Sinch XMS batches API in the same request. That is four hops. The
same job through Zapier is six: Zapier polls Pipedrive on an interval, the
trigger fires and consumes a task, the action formats the message and consumes
a second task, and only then does Sinch hear about it. The console shows both
paths side by side, builds the real Sinch request, and states plainly that no
broker was used. At 600 stage changes a month the direct path removes 1,200
billed tasks and a third vendor holding the customer's mobile number in
transit.

**Who is this message from, and what is it about?** An inbound SMS carries a
number, not an ID, so the number is normalised to E.164 and joined to the
Pipedrive person. The person's most recently touched open deal is attached, and
both go into the prompt the assistant answers from. That is what makes a reply
useful rather than generic: the assistant quotes the deal's own value and
stage, because it is reading the record.

**When does a person take over?** Three ways, and all three are explicit. The
customer asks for one, in any of the phrasings people actually use. The deal is
above the value where a price conversation is always a rep's job. Or the
assistant has answered the same kind of question twice already and is about to
try a third time. A handover is not a flag nobody sees: it creates a dated,
owned call activity on the Pipedrive deal, and the assistant stops replying,
because two voices answering one customer is how a customer gets told two
different things.

**What does STOP actually have to do?** Three things together, or the opt out
is not real. The Pipedrive person is marked `marketing_status: unsubscribed`
with the keyword and the timestamp recorded, exactly one confirmation goes back
to the handset, and every later automated dispatch to that number is refused at
the single gate every send passes through. A second STOP changes nothing and
sends nothing. START puts consent back, because an opt out with no way back is
a trap.

## The Power BI feed

Every row in `fact_deal_state` carries a SHA256 of its own business fields.
Power BI keeps the previous hashes and the `updated_at` watermark, so a refresh
pulls only the rows whose hash moved and leaves the rest alone. The hash
deliberately excludes anything that ticks on its own, so an extraction
timestamp cannot make an unchanged row look changed.

Rows carry the deal, the person, the stage, the status, the value, the owner,
days in the current stage, days open, and the SMS consent state, so a consent
change is visible in the report rather than only in the CRM. Stage durations
are measured from recorded stage changes. The stage a deal started in has no
entry event, so its duration is unknowable and is left out rather than guessed
at.

## Layout

| File | Purpose |
| --- | --- |
| `core.py` | The engine: webhook translation, thread routing, handover detection, the opt out synchronizer and the ledger. No Streamlit import |
| `page.py` | The Streamlit page, exposing `render()` |

## Safety

- Every Pipedrive and Sinch request is built in full and displayed rather than
  sent, so the console cannot touch a live account.
- API tokens are redacted in the generator, not at the point of display, so a
  caller that forgets cannot leak one.
- Anything typed into the page is HTML escaped before it reaches an
  `unsafe_allow_html` block.
- Consent is checked at one gate that every automated dispatch passes through,
  so an opt out cannot be bypassed by adding another campaign.

## Tests

```bash
python3 -m pytest tests/test_pipedrive_integration_engine.py tests/test_pipedrive_integration_engine_page.py -q
```

Both suites also run standalone with `python3`, printing
`PIPEDRIVE RESULT: PASS <n>/<n>` and `PIPEDRIVE UI RESULT: PASS <n>/<n>`.
