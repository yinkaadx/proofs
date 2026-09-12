"""Engine tests for the Zero Trust Remote Access Console.

Written for pytest. Every case is deterministic: the clock and the random source
are injected, so a seeded run reproduces exactly.

Run: pytest tests/test_zero_trust_rmm_console.py
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.zero_trust_rmm_console.core import (  # noqa: E402
    AUDITOR,
    CODE_EXPIRED,
    CODE_LENGTH,
    CODE_TTL_MINUTES,
    CODE_UNKNOWN,
    CODE_USED,
    CODE_VALID,
    COMPANY_A,
    COMPANY_B,
    MFA_NOT_ENROLLED,
    MFA_OK,
    MFA_TIMEOUT,
    MFA_WRONG_CODE,
    MSP_ADMIN,
    SAMPLE_MACHINES,
    SAMPLE_USERS,
    TECHNICIAN,
    TENANT_ADMIN,
    CodeVault,
    Machine,
    MfaLedger,
    User,
    access_matrix,
    attempt_login,
    can_connect_to,
    compliance,
    user_by_username,
    visible_machines,
)

NOW = "2026-09-12T10:00:00Z"
LATER = "2026-09-12T10:14:59Z"
PAST_EXPIRY = "2026-09-12T10:15:01Z"


def seeded(seed: int = 1234) -> random.Random:
    return random.Random(seed)


def user(name: str) -> User:
    found = user_by_username(name)
    assert found is not None, f"fixture user {name!r} is missing"
    return found


# ---------------------------------------------------------------------------
# RBAC filtering
# ---------------------------------------------------------------------------

def test_tenant_boundary_is_absolute_for_an_admin():
    ada = user("a.admin")
    visible = visible_machines(ada)
    assert visible, "a tenant admin should see their own estate"
    assert {m.tenant for m in visible} == {COMPANY_A}
    assert all(m.tenant != COMPANY_B for m in visible)


def test_each_tenant_admin_sees_only_their_own_machines():
    a_visible = {m.machine_id for m in visible_machines(user("a.admin"))}
    b_visible = {m.machine_id for m in visible_machines(user("b.admin"))}
    assert a_visible and b_visible
    assert a_visible.isdisjoint(b_visible), (
        "two tenants must never share a visible machine")


def test_tenant_admin_sees_every_machine_in_their_tenant():
    expected = {m.machine_id for m in SAMPLE_MACHINES if m.tenant == COMPANY_A}
    assert {m.machine_id for m in visible_machines(user("a.admin"))} == expected


def test_msp_admin_crosses_tenants_because_it_holds_that_capability():
    msp = user("msp.oncall")
    assert msp.can("cross_tenant")
    visible = visible_machines(msp)
    assert {m.machine_id for m in visible} == {m.machine_id for m in SAMPLE_MACHINES}
    assert {m.tenant for m in visible} == {COMPANY_A, COMPANY_B}


def test_technician_is_narrowed_to_their_sites():
    femi = user("a.tech.london")
    visible = visible_machines(femi)
    assert visible, "a technician with a site should see that site"
    assert {m.site for m in visible} == {"London HQ"}
    assert all(m.tenant == COMPANY_A for m in visible)
    # Narrower than their own admin, which is the point of site scoping.
    assert len(visible) < len(visible_machines(user("a.admin")))


def test_technician_with_no_sites_sees_nothing():
    stranded = User("x.tech", "Technician with no sites", COMPANY_A, TECHNICIAN,
                    sites=())
    assert visible_machines(stranded) == []


def test_auditor_sees_the_estate_but_cannot_connect():
    priya = user("a.auditor")
    visible = visible_machines(priya)
    assert visible, "an auditor must be able to review the estate"
    assert not priya.can("connect")
    for machine in visible:
        allowed, reason = can_connect_to(priya, machine)
        assert allowed is False
        assert "read only" in reason


def test_connect_is_refused_across_the_tenant_boundary_with_a_clear_reason():
    ada = user("a.admin")
    other = next(m for m in SAMPLE_MACHINES if m.tenant == COMPANY_B)
    allowed, reason = can_connect_to(ada, other)
    assert allowed is False
    assert COMPANY_B in reason and "boundary is absolute" in reason


def test_connect_is_refused_outside_a_technicians_sites():
    femi = user("a.tech.london")
    elsewhere = next(m for m in SAMPLE_MACHINES
                     if m.tenant == COMPANY_A and m.site != "London HQ")
    allowed, reason = can_connect_to(femi, elsewhere)
    assert allowed is False
    assert "outside the sites" in reason


def test_connect_is_allowed_inside_scope():
    femi = user("a.tech.london")
    inside = next(m for m in visible_machines(femi))
    allowed, reason = can_connect_to(femi, inside)
    assert allowed is True
    assert inside.machine_id in reason


@pytest.mark.parametrize("username", [u.username for u in SAMPLE_USERS])
def test_no_user_ever_sees_a_machine_outside_their_tenant(username):
    acting = user(username)
    if acting.can("cross_tenant"):
        pytest.skip("the MSP role is the declared exception")
    assert all(m.tenant == acting.tenant for m in visible_machines(acting))


def test_access_matrix_has_one_row_per_user_and_uniform_columns():
    rows = access_matrix()
    assert len(rows) == len(SAMPLE_USERS)
    for column in {key for row in rows for key in row}:
        kinds = {type(row[column]).__name__ for row in rows}
        assert len(kinds) == 1, f"column {column!r} mixes types: {sorted(kinds)}"
    assert all(isinstance(r["Machines visible"], int) for r in rows)


def test_access_matrix_counts_match_the_filter():
    by_user = {r["User"]: r["Machines visible"] for r in access_matrix()}
    for acting in SAMPLE_USERS:
        assert by_user[acting.username] == len(visible_machines(acting))


def test_an_empty_estate_is_handled_without_error():
    assert visible_machines(user("a.admin"), machines=()) == []


# ---------------------------------------------------------------------------
# MFA enforcement ledger
# ---------------------------------------------------------------------------

def test_an_unenrolled_account_is_refused_outright():
    ledger = MfaLedger()
    tom = user("b.tech.nomfa")
    assert tom.mfa_enrolled is False
    attempt = attempt_login(tom, ledger, seeded(), NOW)
    assert attempt.outcome == MFA_NOT_ENROLLED
    assert attempt.granted is False
    assert "refused outright" in attempt.detail


def test_an_unenrolled_account_is_refused_however_the_dice_fall():
    tom = user("b.tech.nomfa")
    for seed in range(25):
        ledger = MfaLedger()
        attempt = attempt_login(tom, ledger, random.Random(seed), NOW)
        assert attempt.outcome == MFA_NOT_ENROLLED


def test_a_forced_success_is_recorded_as_granted():
    ledger = MfaLedger()
    attempt = attempt_login(user("a.admin"), ledger, seeded(), NOW,
                            force_outcome=MFA_OK)
    assert attempt.granted is True
    assert attempt.outcome == MFA_OK
    assert ledger.granted == [attempt]
    assert ledger.denied == []


@pytest.mark.parametrize("outcome", [MFA_TIMEOUT, MFA_WRONG_CODE])
def test_a_failed_second_factor_opens_no_session(outcome):
    ledger = MfaLedger()
    attempt = attempt_login(user("a.admin"), ledger, seeded(), NOW,
                            force_outcome=outcome)
    assert attempt.granted is False
    assert "No session was opened" in attempt.detail
    assert ledger.granted == []
    assert len(ledger.denied) == 1


def test_the_ledger_is_append_only_and_contiguous():
    ledger = MfaLedger()
    for index in range(6):
        attempt_login(user("a.admin"), ledger, random.Random(index), NOW)
    assert [a.sequence for a in ledger.attempts] == [1, 2, 3, 4, 5, 6]
    assert len(ledger.attempts) == 6


def test_the_ledger_records_who_tried_and_from_where():
    ledger = MfaLedger()
    attempt = attempt_login(user("b.admin"), ledger, seeded(), NOW,
                            source_ip="198.51.100.7", force_outcome=MFA_OK)
    assert attempt.username == "b.admin"
    assert attempt.tenant == COMPANY_B
    assert attempt.role == TENANT_ADMIN
    assert attempt.source_ip == "198.51.100.7"
    assert attempt.timestamp == NOW


def test_the_simulated_outcome_is_reproducible_for_a_given_seed():
    first = MfaLedger()
    second = MfaLedger()
    for seed in range(12):
        a = attempt_login(user("a.admin"), first, random.Random(seed), NOW)
        b = attempt_login(user("a.admin"), second, random.Random(seed), NOW)
        assert a.outcome == b.outcome


def test_the_simulator_produces_both_successes_and_failures():
    ledger = MfaLedger()
    for seed in range(40):
        attempt_login(user("a.admin"), ledger, random.Random(seed), NOW)
    outcomes = {a.outcome for a in ledger.attempts}
    assert MFA_OK in outcomes, "a simulator that never succeeds proves nothing"
    assert outcomes & {MFA_TIMEOUT, MFA_WRONG_CODE}, (
        "a simulator that never fails proves nothing either")


def test_compliance_never_reports_a_session_granted_without_mfa():
    ledger = MfaLedger()
    for seed in range(30):
        attempt_login(user("a.admin"), ledger, random.Random(seed), NOW)
    attempt_login(user("b.tech.nomfa"), ledger, seeded(), NOW)
    stats = compliance(ledger)
    assert stats["granted_without_mfa"] == 0
    assert stats["attempts"] == len(ledger.attempts)
    assert stats["granted"] + stats["denied"] == stats["attempts"]
    assert 0.0 <= stats["grant_rate"] <= 1.0


def test_compliance_names_the_unenrolled_accounts():
    stats = compliance(MfaLedger())
    assert "b.tech.nomfa" in stats["unenrolled"]
    assert stats["users_enrolled"] == stats["users_total"] - len(stats["unenrolled"])
    assert 0.0 <= stats["enrolment_rate"] <= 1.0


def test_compliance_on_an_empty_ledger_does_not_divide_by_zero():
    stats = compliance(MfaLedger())
    assert stats["attempts"] == 0
    assert stats["grant_rate"] == 0.0


def test_ledger_rows_are_uniform_for_the_table():
    ledger = MfaLedger()
    attempt_login(user("a.admin"), ledger, seeded(), NOW, force_outcome=MFA_OK)
    attempt_login(user("b.tech.nomfa"), ledger, seeded(), NOW)
    rows = ledger.rows()
    assert len(rows) == 2
    for column in {key for row in rows for key in row}:
        kinds = {type(row[column]).__name__ for row in rows}
        assert len(kinds) == 1, f"column {column!r} mixes types: {sorted(kinds)}"


# ---------------------------------------------------------------------------
# Ad hoc session codes
# ---------------------------------------------------------------------------

def test_a_code_is_exactly_six_digits():
    vault = CodeVault()
    issued = vault.issue(user("a.admin"), seeded(), NOW)
    assert len(issued.code) == CODE_LENGTH == 6
    assert issued.code.isdigit()


def test_leading_zeros_are_preserved():
    class LowRng:
        def randrange(self, upper):  # noqa: D102
            return 42

    issued = CodeVault().issue(user("a.admin"), LowRng(), NOW)
    assert issued.code == "000042"
    assert len(issued.code) == 6


def test_a_code_expires_fifteen_minutes_after_issue_by_default():
    issued = CodeVault().issue(user("a.admin"), seeded(), NOW)
    assert issued.issued_at == NOW
    assert issued.expires_at == "2026-09-12T10:15:00Z"
    assert CODE_TTL_MINUTES == 15


def test_the_lifetime_is_configurable():
    issued = CodeVault().issue(user("a.admin"), seeded(), NOW, ttl_minutes=30)
    assert issued.expires_at == "2026-09-12T10:30:00Z"


@pytest.mark.parametrize("ttl", [0, -1, -60])
def test_a_non_positive_lifetime_is_refused(ttl):
    with pytest.raises(ValueError):
        CodeVault().issue(user("a.admin"), seeded(), NOW, ttl_minutes=ttl)


def test_an_auditor_cannot_issue_a_code():
    with pytest.raises(PermissionError) as raised:
        CodeVault().issue(user("a.auditor"), seeded(), NOW)
    assert "cannot issue session codes" in str(raised.value)


@pytest.mark.parametrize("username", ["msp.oncall", "a.admin", "a.tech.london"])
def test_roles_that_hold_the_capability_can_issue(username):
    issued = CodeVault().issue(user(username), seeded(), NOW)
    assert issued.issued_by == username


def test_a_code_is_valid_until_the_moment_it_expires():
    vault = CodeVault()
    issued = vault.issue(user("a.admin"), seeded(), NOW)
    status, record = vault.redeem(issued.code, LATER)
    assert status == CODE_VALID
    assert record is not None and record.used is True


def test_a_code_is_refused_once_expired():
    vault = CodeVault()
    issued = vault.issue(user("a.admin"), seeded(), NOW)
    status, _ = vault.redeem(issued.code, PAST_EXPIRY)
    assert status == CODE_EXPIRED


def test_expiry_is_checked_before_use_so_an_expired_code_is_not_spent():
    vault = CodeVault()
    issued = vault.issue(user("a.admin"), seeded(), NOW)
    vault.redeem(issued.code, PAST_EXPIRY)
    assert vault.find(issued.code).used is False


def test_a_code_is_single_use():
    vault = CodeVault()
    issued = vault.issue(user("a.admin"), seeded(), NOW)
    assert vault.redeem(issued.code, LATER)[0] == CODE_VALID
    assert vault.redeem(issued.code, LATER)[0] == CODE_USED


def test_an_unknown_code_is_refused():
    status, record = CodeVault().redeem("000000", NOW)
    assert status == CODE_UNKNOWN
    assert record is None


def test_active_codes_exclude_the_expired_and_the_spent():
    vault = CodeVault()
    first = vault.issue(user("a.admin"), random.Random(1), NOW)
    second = vault.issue(user("a.admin"), random.Random(2), NOW)
    assert len(vault.active(NOW)) == 2
    vault.redeem(first.code, LATER)
    assert {c.code for c in vault.active(LATER)} == {second.code}
    assert vault.active(PAST_EXPIRY) == []


def test_codes_issued_together_are_unique():
    vault = CodeVault()
    rng = seeded()
    issued = [vault.issue(user("a.admin"), rng, NOW) for _ in range(40)]
    codes = [c.code for c in issued]
    assert len(set(codes)) == len(codes), "two live codes must never collide"


def test_a_collision_is_retried_rather_than_returned():
    """An rng that keeps repeating itself must not produce a duplicate."""
    class StubbornRng:
        def __init__(self):
            self.calls = 0

        def randrange(self, upper):
            self.calls += 1
            # The same value twice, then a different one.
            return 7 if self.calls <= 2 else 8

    vault = CodeVault()
    rng = StubbornRng()
    first = vault.issue(user("a.admin"), rng, NOW)
    second = vault.issue(user("a.admin"), rng, NOW)
    assert first.code == "000007"
    assert second.code == "000008"
    assert first.code != second.code


def test_code_generation_is_reproducible_for_a_given_seed():
    a = CodeVault().issue(user("a.admin"), random.Random(99), NOW)
    b = CodeVault().issue(user("a.admin"), random.Random(99), NOW)
    assert a.code == b.code


def test_vault_rows_report_status_and_stay_uniform():
    vault = CodeVault()
    spent = vault.issue(user("a.admin"), random.Random(1), NOW)
    vault.issue(user("a.admin"), random.Random(2), NOW)
    vault.redeem(spent.code, LATER)
    rows = vault.rows(LATER)
    assert {r["Status"] for r in rows} == {"Redeemed", "Active"}
    for column in {key for row in rows for key in row}:
        kinds = {type(row[column]).__name__ for row in rows}
        assert len(kinds) == 1, f"column {column!r} mixes types: {sorted(kinds)}"
    assert {r["Status"] for r in vault.rows(PAST_EXPIRY)} == {"Redeemed", "Expired"}


def test_a_code_records_the_tenant_it_belongs_to():
    issued = CodeVault().issue(user("b.admin"), seeded(), NOW)
    assert issued.tenant == COMPANY_B


# ---------------------------------------------------------------------------
# House style
# ---------------------------------------------------------------------------

def test_no_em_or_en_dashes_in_user_facing_prose():
    banned = ("—", "–")
    ledger = MfaLedger()
    vault = CodeVault()
    prose: list[str] = []
    for outcome in (MFA_OK, MFA_TIMEOUT, MFA_WRONG_CODE):
        prose.append(attempt_login(user("a.admin"), ledger, seeded(), NOW,
                                   force_outcome=outcome).detail)
    prose.append(attempt_login(user("b.tech.nomfa"), ledger, seeded(), NOW).detail)
    prose.append(vault.issue(user("a.admin"), seeded(), NOW).note)
    prose += [m.hostname for m in SAMPLE_MACHINES]
    prose += [u.display_name for u in SAMPLE_USERS]
    for acting in SAMPLE_USERS:
        for machine in SAMPLE_MACHINES:
            prose.append(can_connect_to(acting, machine)[1])
    offenders = [p for p in prose if any(b in p for b in banned)]
    assert not offenders, f"em or en dashes found: {offenders[:5]}"
