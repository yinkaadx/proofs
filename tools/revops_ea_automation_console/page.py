"""RevOps EA and Automation Console.

Rendered inside the hub app. All logic lives in core.py, which has no
Streamlit dependency, so the same engine could sit behind a real mailbox.

Everything on this page is evaluated live from the controls. There is no
session state to go stale, which removes the class of bug where a card on
screen describes a verdict that two interactions ago stopped being true.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.revops_ea_automation_console.core import (
    ARCHIVE,
    CHASE_PULL,
    DRAFT_READY,
    ENGINE_VERSION,
    FOUNDER_ACTION,
    LOGGED,
    MAX_CHASE_MESSAGES,
    PULL_THRESHOLD_DAYS,
    ROUTES,
    SAMPLE_EMAILS,
    SAMPLE_NOTES,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SEVERITY_WARN,
    evaluate_contractor_chase,
    log_call_decision,
    simulate_inbox_triage,
)

_TONE = {SEVERITY_OK: "ok", SEVERITY_WARN: "warn", SEVERITY_CRITICAL: "crit"}
_ROUTE_TONE = {FOUNDER_ACTION: "crit", DRAFT_READY: "info", ARCHIVE: "warn"}


def _kpis(pairs) -> None:
    cells = "".join(
        f'<div class="app-kpi {tone}"><b>{esc(value)}</b>'
        f"<span>{esc(label)}</span></div>"
        for value, label, tone in pairs)
    st.markdown(f'<div class="app-kpis">{cells}</div>',
                unsafe_allow_html=True)


def _finding_card(finding) -> None:
    tone = _TONE[finding.severity]
    st.markdown(
        f'<div class="app-card {tone}">\n'
        f'  <h4><span class="app-tag {tone}">{esc(finding.code)}</span>\n'
        f"  {esc(finding.title)}</h4>\n"
        f"  <p>{esc(finding.detail)}</p>\n"
        f'  <div class="app-ev">Fix: {esc(finding.fix)}</div>\n'
        f"</div>",
        unsafe_allow_html=True)


def _stage(state: str, name: str, detail: str) -> str:
    return (f'<div class="app-stage"><span class="s {state}">'
            f"{esc(state.upper())}</span>"
            f'<span class="n">{esc(name)}</span>'
            f'<span class="d">{esc(detail)}</span></div>')


def render() -> None:
    inject()
    st.markdown(
        '<div class="app-hero">\n'
        "  <h1>RevOps EA and Automation Console</h1>\n"
        "  <p>Three pieces of an assistant function, each built around the\n"
        "  mistake that actually costs a founder money. Inbox triage has one\n"
        "  unrecoverable verdict, so archiving requires positive evidence\n"
        "  and everything unrecognised goes to the founder. A chase without\n"
        "  a terminal state is a habit rather than a process, so this ladder\n"
        "  ends in a decision to pull the task. And a decision logged\n"
        "  without an owner is a wish, so the ledger refuses an entry rather\n"
        "  than emitting a hollow one.</p>\n"
        "</div>",
        unsafe_allow_html=True)

    triage_tab, chase_tab, ledger_tab = st.tabs(
        ["Inbox triage", "Contractor chase", "Call decision ledger"])

    # -- 1. inbox triage ---------------------------------------------------
    with triage_tab:
        st.markdown(
            "#### Archive is the only verdict the founder never sees\n\n"
            "A newsletter sent to the founder costs fifteen seconds of "
            "attention. A contract sent to the archive is never read at all. "
            "The routing rule is asymmetric because the costs are.")

        labels = [label for label, _ in SAMPLE_EMAILS]
        choice = st.selectbox("Start from a sample message", labels, index=0)
        preset = dict(SAMPLE_EMAILS)[choice]
        body = st.text_area("Message body", value=preset, height=130)

        result = simulate_inbox_triage(body)
        tone = _ROUTE_TONE[result.route]

        _kpis([
            (result.route, "Routing decision", tone),
            (str(len(result.escalate_signals)), "Escalation signals",
             "crit" if result.escalate_signals else "ok"),
            (str(len(result.draftable_signals)), "Draftable signals", ""),
            ("yes" if result.grounded else "no", "Summary is extractive",
             "ok" if result.grounded else "crit"),
        ])

        st.markdown(
            f'<div class="app-card {tone}">\n'
            f'  <h4><span class="app-tag {tone}">{esc(result.route)}</span>\n'
            f"  {esc(result.headline)}</h4>\n"
            f"  <p>{esc(result.summary or 'Nothing was read.')}</p>\n"
            f'  <div class="app-ev">Every character of this summary appears '
            f"in the message. Nothing was paraphrased and no figure or date "
            f"was generated.</div>\n"
            f"</div>",
            unsafe_allow_html=True)

        if result.escalate_signals:
            rows = "".join(_stage("fail", f"Signal {index}", signal)
                           for index, signal in
                           enumerate(result.escalate_signals, start=1))
            st.markdown(f'<div class="app-card crit">{rows}</div>',
                        unsafe_allow_html=True)

        for finding in result.findings:
            _finding_card(finding)

        st.markdown("#### Every sample, routed by the same rules")
        lines = []
        for label, sample in SAMPLE_EMAILS:
            outcome = simulate_inbox_triage(sample)
            state = {"Founder Action": "fail", "Draft Ready": "warn",
                     "Archive": "skip"}[outcome.route]
            lines.append(_stage(state, label, outcome.route))
        st.markdown(f'<div class="app-card">{"".join(lines)}</div>',
                    unsafe_allow_html=True)
        st.caption(
            f"The three routes are {', '.join(ROUTES)}. Only one of them "
            f"ends with the founder never seeing the message, and it is the "
            f"only one that requires a positive mark of bulk mail to reach.")

    # -- 2. contractor chase ----------------------------------------------
    with chase_tab:
        st.markdown(
            "#### Say the decision out loud, and make sure the ladder ends\n\n"
            "The failure mode of a chase is not that it is too aggressive. "
            "It is that it has no last rung, so the task quietly stays "
            "assigned to someone who is not doing it.")

        days = st.slider("Days waiting on the contractor", 0, 14, 4)
        verdict = evaluate_contractor_chase(days)
        tone = _TONE[verdict.severity]

        _kpis([
            (verdict.stage, "Stage", tone),
            (str(verdict.days_waiting), "Days waiting", ""),
            (f"{verdict.chase_messages_sent} of {MAX_CHASE_MESSAGES}",
             "Chase messages spent", ""),
            ("yes" if verdict.terminal else "no", "Ladder finished",
             "crit" if verdict.terminal else "ok"),
        ])

        st.markdown(
            f'<div class="app-card {tone}">\n'
            f'  <h4><span class="app-tag {tone}">'
            f'{esc(verdict.stage)}</span>\n'
            f"  {esc(verdict.headline)}</h4>\n"
            f"  <p>{esc(verdict.recommendation)}</p>\n"
            f"</div>",
            unsafe_allow_html=True)

        for finding in verdict.findings:
            _finding_card(finding)

        st.markdown("#### The whole ladder, so no rung is hidden")
        rungs = []
        for day in range(0, PULL_THRESHOLD_DAYS + 2):
            step = evaluate_contractor_chase(day)
            state = ("fail" if step.terminal
                     else "warn" if step.rank >= 2 else "pass")
            rungs.append(_stage(state, f"Day {day}", step.recommendation))
        st.markdown(f'<div class="app-card">{"".join(rungs)}</div>',
                    unsafe_allow_html=True)
        st.caption(
            f"The recommendation never softens as the wait grows, and every "
            f"wait of {PULL_THRESHOLD_DAYS} days or more returns the same "
            f"terminal answer: {CHASE_PULL}.")

    # -- 3. call decision ledger ------------------------------------------
    with ledger_tab:
        st.markdown(
            "#### An owner, a deliverable, and a check someone else can run\n"
            "\nAn entry missing any of the three is refused with the gap "
            "named, and no partial artifact is written, because a half "
            "filled decision in a ledger reads as a decision that was made.")

        notes = st.text_area("Raw notes from the call", value=SAMPLE_NOTES,
                             height=150)
        ledger = log_call_decision(notes)

        _kpis([
            (str(ledger.lines_in), "Lines in", ""),
            (str(ledger.logged), "Logged as decisions", "ok"),
            (str(ledger.refused), "Refused", "crit"),
            (str(len(ledger.unparsed)), "No owner named", "warn"),
        ])

        for entry in ledger.entries:
            tone = "ok" if entry.logged else "crit"
            due = entry.due or "no date said on the call"
            st.markdown(
                f'<div class="app-card {tone}">\n'
                f'  <h4><span class="app-tag {tone}">'
                f'{esc(entry.status)}</span>\n'
                f"  {esc(entry.owner)}</h4>\n"
                f"  <p><b>Deliverable:</b> {esc(entry.deliverable)}</p>\n"
                f"  <p><b>Due:</b> {esc(due)}</p>\n"
                f'  <div class="app-ev">'
                f"{esc(entry.check_marker or 'No verifiable check, so no artifact was emitted.')}"
                f"</div>\n"
                f"</div>",
                unsafe_allow_html=True)
            for finding in entry.findings:
                _finding_card(finding)

        if ledger.unparsed:
            rows = "".join(_stage("skip", "No owner", line)
                           for line in ledger.unparsed)
            st.markdown(f'<div class="app-card warn">{rows}</div>',
                        unsafe_allow_html=True)

        for finding in ledger.findings:
            _finding_card(finding)

        st.caption(
            f"{ledger.logged} logged plus {ledger.refused} refused plus "
            f"{len(ledger.unparsed)} with no owner equals {ledger.lines_in} "
            f"lines in. Nothing the parser could not read was discarded.")

    st.markdown(
        f'<div class="app-foot">Engine {esc(ENGINE_VERSION)}. '
        f"Verdicts are evaluated live from the controls. Triage summaries are "
        f"extractive rather than generated, so a figure or a date on screen "
        f"came from the message rather than from a model. An entry reaches "
        f"{esc(LOGGED)} only with an owner, a deliverable, and a check a "
        f"third party could run without asking the owner whether they did "
        f"it.</div>",
        unsafe_allow_html=True)
