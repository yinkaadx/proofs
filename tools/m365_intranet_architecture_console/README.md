# Microsoft 365 Intranet Architecture Console

SharePoint role simulator, Power Apps PTO and timesheet logic, and Claude AI
project insights.

Live at `/m365-intranet-architecture-console` on the hub.

## Four quiet failures, treated as the product

An intranet on SharePoint, Power Apps and Power Automate rarely falls over. It
goes wrong quietly, and each of these is a finding in somebody's audit:

**A module hidden but still reachable.** Hiding a tile is a convenience, not a
control. The link still resolves, so every open is authorised by the same
function the back end uses and the navigation simply asks it. The console lets
you try a module by its key as an employee and shows the refusal.

**Two requests that together overdraw the balance.** A pending request holds its
days. Without that, two requests raised the same morning each pass validation
against the same balance, and payroll finds out rather than the app.

**A request approved by the person who raised it.** The flow refuses a decision
from the requester and from anyone who is not a manager, and refuses a second
decision on a request that already has one.

**A weekly report nobody reads.** The insight module turns hours and deadlines
into a stated risk with the arithmetic attached, and says what to do about each
one.

## Judgement encoded, not just rules

A project a week old has no trend: its burn ratio swings on a single day's work.
Flagging it produces a report that cries wolf on every new project and stops
being read, so nothing is flagged for burn until 15 percent of the schedule has
passed. Being early does not excuse being over budget, so that check still fires
first.

Every refusal names the rule it broke. A Power App that answers "invalid"
produces a support ticket. One that answers "you have 3.5 days available and
asked for 5" produces a corrected request.

## The AI module

The executive summary is computed from the same figures the table shows rather
than written about them. A real deployment sends those numbers to the Claude API
for the wording, and the prompt that would leave the tenant is shown on screen,
because that is the first question a security reviewer asks about an AI feature.
Project codes, clients, hours and dates go. No document content does.

Doing the arithmetic in the engine means the summary cannot describe a project
the table does not show, which is the failure that matters when somebody
forwards it to a client.

## Layout

| File | Purpose |
| --- | --- |
| `core.py` | The engine. No Streamlit import, so it can sit behind the real Graph and Dataverse calls |
| `page.py` | The page, rendered by the hub |
| `../../tests/test_m365_intranet_architecture_console.py` | Engine tests, 74 checks |
| `../../tests/test_m365_intranet_architecture_console_page.py` | Page tests via AppTest, 34 checks |

## Rules worth keeping when this is extended

- Every table column holds one type, because the table widget serialises
  through Arrow and Arrow refuses a mixed column.
- Callbacks read live widget state out of `st.session_state` rather than
  arguments bound at render time, which are one interaction stale.
- Access is decided by `can_open`, never by which tiles were rendered. A new
  module inherits the control by declaring its roles and nothing else.
