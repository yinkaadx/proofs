# Secure SharePoint Architecture Console

Entra ID authentication simulator, role based access matrix, and secure
deployment checklist.

Live at `/sharepoint-zero-trust-simulator` on the hub.

## The design decision that matters

All three features read one `TenantConfig`. The sign in decision, the access
matrix and the deployment audit cannot disagree with each other, because there
is only one set of switches for them to read. Turn external sharing off in the
audit tab and the guest is refused on the login tab and the guest column of the
matrix empties, in the same render.

A console whose three answers can drift apart is worse than no console, because
it certifies a posture the tenant does not actually have. The test suite pins
this: `test_one_switch_moves_the_audit_the_matrix_and_the_sign_in_together`.

## What it shows

**Entra ID login simulator.** Four user types against a conditional access
stack: directory lookup, legacy authentication, multi factor authentication,
device compliance, named location, sign in risk and external sharing. Every
policy is evaluated rather than short circuiting on the first block, because the
useful part is seeing which policies had an opinion. Admins are held to a harder
line than employees on device compliance, since an owner session from an
unmanaged machine is the highest value target in the tenant.

**Role based access matrix.** Owner, Member and Visitor across six document
libraries. The tenant has the last word over the permission level: a guest
holding Visitor still reaches nothing confidential, because the boundary is the
library rather than the person.

**Secure deployment audit.** Eleven controls, each with a severity, the current
value, why it matters and the PowerShell that closes it. The console opens on
the posture Microsoft hands over, which fails every one of them, and a
remediation script is generated for the open items only.

## Layout

| File | Purpose |
| --- | --- |
| `core.py` | The engine. No Streamlit import, so it can read a real tenant through Graph |
| `page.py` | The page, rendered by the hub |
| `../../tests/test_sharepoint_zero_trust_simulator.py` | Engine tests, 84 checks |
| `../../tests/test_sharepoint_zero_trust_simulator_page.py` | Page tests via AppTest, 35 checks |

## Rules worth keeping when this is extended

- Every table column holds one type, because the table widget serialises
  through Arrow and Arrow refuses a mixed column.
- The configuration controls sit in a form, so a half changed tenant is never
  evaluated. The callback reads live widget state out of `st.session_state`
  rather than arguments bound at render time, which are one interaction stale.
- Loading a whole posture deletes the widget keys bound to it. A key that
  survived would put the old setting back on the next interaction and silently
  undo the load. Pinned by `test_loading_a_posture_resets_the_controls_bound_to_it`.
