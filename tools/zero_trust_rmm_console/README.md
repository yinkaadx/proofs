# Zero Trust Remote Access Console

Two tenants on one self hosted remote access platform. Pick a user and the
estate shrinks to exactly what their role and sites permit, a login only
completes when a second factor answers, and an ad hoc support code is six digits
that expire.

Live at `/zero-trust-rmm-console` on the hub.

## The three guarantees

**The tenant boundary is absolute.** A Company A account can never enumerate a
Company B endpoint, whatever its role. Only an explicit cross tenant capability,
held by the managing provider, crosses it. A technician is narrowed further to
the sites they support, because a technician who can see every endpoint is an
admin by another name. An auditor sees the estate and can never connect to it,
which is the point of the role: review access without holding it.

**No session is granted without a second factor.** An account with no factor
enrolled is refused outright rather than downgraded to a password, because an
unenrolled account would otherwise be the way in. Every attempt lands in an
append only ledger with the account, tenant, role, outcome and source address,
so the claim that every granted session passed MFA is evidenced rather than
asserted.

**An ad hoc code is short, short lived and single use.** Six digits because
someone reads it down a phone line, which is exactly why it must expire and why
it must not work twice. Expiry is checked before the code is spent, so an
expired code is not consumed. Collisions are retried rather than ignored: two
live sessions sharing a code would route a stranger to the wrong machine. In
production the generator draws from `secrets.SystemRandom()`, because a
predictable code hands out remote access.

## Layout

| File | Purpose |
| --- | --- |
| `core.py` | RBAC filtering, the MFA ledger and the code vault. No Streamlit import |
| `page.py` | The three tab console |
| `../../tests/test_zero_trust_rmm_console.py` | Engine tests, pytest |
| `../../tests/test_zero_trust_rmm_console_page.py` | Page tests via AppTest, pytest |

These two suites are written for pytest rather than the standalone script style
used by the older tools:

```bash
python3 -m pytest tests/test_zero_trust_rmm_console.py \
                 tests/test_zero_trust_rmm_console_page.py -q
```

The clock and the random source are both injected, so a seeded run reproduces
exactly and the same engine can take a real CSPRNG in production.
