"""C08 — the order of a product deactivation, observed rather than asserted.

ACCESS-MODEL section 7: deactivating a person in the product UI first closes
their membership in PostgreSQL and only then revokes their Keycloak sessions.

The requirement is that this is *proved by a test that observes the order*.
Every test in this file does the same thing: the stand-in revoker opens its own
connection to the real database and reads ``kb.membership.status`` at the moment
it is called. It therefore sees what any other process in the world would see —
committed state — and the assertion is on what it saw.

``test_the_ordering_test_would_notice_a_reversal`` is the non-vacuity check: it
performs the unsafe sequence — provider first, database second — with the same
public operations and asserts the probe reads ``active``. Swap the two blocks in
:func:`kb.access.membership_deactivation.deactivate_member` and the tests here
fail. That is the difference between a comment and a proof.

The stand-in is not Keycloak and does not claim to be. There is no Keycloak in
this environment (E03 pending); what it contributes is a second connection and
a read.
"""

from __future__ import annotations

import uuid

import pytest

from kb.access.membership import block_state
from kb.access.membership_deactivation import (
    _close_membership,
    deactivate_member,
    outstanding_revocations,
    retry_outstanding_revocations,
)

pytestmark = pytest.mark.integration


# ------------------------------------------------------- the observation


def test_postgresql_closes_before_the_provider_is_touched(
    organisation, world, people, db, observing_revoker
):
    """The probe must read ``deactivated`` while the provider call happens.

    Anything else would mean the membership was still open at the instant
    Keycloak was asked to end the sessions — precisely the window the ordering
    rule exists to close.
    """
    who = people.colleague
    world.membership(who)
    world.browser_session(who)

    revoker = observing_revoker
    result = deactivate_member(
        db,
        people.principal(people.admin),
        organisation_id=organisation,
        target=who,
        revoker=revoker,
        reason="left the company",
    )

    assert len(revoker.observed) == 1, "the provider was not called exactly once"
    seen = revoker.observed[0]
    assert seen["status"] == "deactivated", seen
    assert seen["deactivated_at"] is not None, seen
    # Read on a connection that never took part in the deactivating
    # transaction, so this is the value any other process would read — not a
    # local echo of a local write.
    assert seen["deactivated_by"] == people.admin, seen
    assert result.membership_status == "deactivated"
    assert result.provider_revocated is True
    assert result.revocation_outstanding is False


def test_the_ordering_test_would_notice_a_reversal(
    organisation, world, people, db, observing_revoker_factory, run_sql
):
    """The control: the WRONG order, and the probe says so.

    If a test passed in both orders it would prove nothing about ordering. This
    one performs the unsafe sequence on purpose and asserts the observer reads
    ``active``.
    """
    who = uuid.uuid4()
    world.membership(who)
    rc, out = run_sql(
        "SELECT issuer, subject FROM kb.membership "
        "WHERE organisation_id = %s AND principal_id = %s",
        (organisation, who),
        principal=people.admin,
    )
    assert rc == 0, out
    issuer, subject = out.split("\t")

    revoker = observing_revoker_factory()

    # --- the unsafe order, on purpose ---
    revoker.revoke_sessions(issuer=issuer, subject=subject)
    _close_membership(
        db,
        people.principal(people.admin),
        organisation_id=organisation,
        target=who,
        reason="reversed on purpose",
    )

    assert revoker.observed[0]["status"] == "active", (
        "the probe read 'active' when the provider ran first — this is the "
        "signal the ordering assertion is built on"
    )


def test_both_steps_happened_and_in_that_order(organisation, world, people, db, observing_revoker):
    """The journal records two facts, the close before the provider's answer."""
    who = uuid.uuid4()
    world.membership(who)

    result = deactivate_member(
        db,
        people.principal(people.admin),
        organisation_id=organisation,
        target=who,
        revoker=observing_revoker,
    )

    actions = [row[1] for row in world.journal()]
    assert "membership_deactivated" in actions, actions
    assert "sessions_revoked" in actions, actions
    assert actions.index("membership_deactivated") < actions.index("sessions_revoked"), actions

    # the revision the close committed under is returned to the caller, so a
    # client can tell that the policy it was admitted under is no longer current
    assert result.policy_revision >= 1
    assert world.membership_row(who)[0] == "deactivated"


def test_the_gateway_sessions_die_in_the_same_transaction(
    organisation, world, people, db, observing_revoker, run_sql
):
    """A still-valid browser cookie stops working at the next request.

    The membership close and the session revocation are one transaction, which
    matters because the cookie stays cryptographically valid for the rest of its
    lifetime: what changes is that the row it resolves to is dead.
    """
    who = uuid.uuid4()
    world.membership(who)
    live = world.browser_session(who)
    other = world.browser_session(people.stranger)

    result = deactivate_member(
        db,
        people.principal(people.admin),
        organisation_id=organisation,
        target=who,
        revoker=observing_revoker,
    )

    assert result.browser_sessions_closed == 1
    rc, out = run_sql(
        "SELECT revoked_at IS NOT NULL, revoked_reason FROM kb.browser_session WHERE id = %s",
        (live,),
        role=None,
    )
    assert rc == 0, out
    assert out.split("\t") == ["True", "membership_deactivated"], out

    # and only that person's sessions: a colleague keeps theirs
    rc, out = run_sql(
        "SELECT revoked_at IS NULL FROM kb.browser_session WHERE id = %s", (other,), role=None
    )
    assert out.strip() == "True", out


# ------------------------------------------- a provider that does not answer


def test_a_failed_provider_does_not_reopen_the_membership(
    organisation, world, people, db, run_sql, failing_revoker
):
    """The whole reason for the order: PostgreSQL does not wait for Keycloak."""
    who = uuid.uuid4()
    world.membership(who)
    lib = world.library()
    world.grant(lib, who, "reader")

    result = deactivate_member(
        db,
        people.principal(people.admin),
        organisation_id=organisation,
        target=who,
        revoker=failing_revoker,
        reason="offboarded",
    )

    assert failing_revoker.observed[0]["status"] == "deactivated"
    assert result.membership_status == "deactivated"
    assert result.provider_revocated is False
    assert result.revocation_outstanding is True
    assert world.membership_row(who)[0] == "deactivated"

    # the member is refused at the database even with the provider unreachable
    rc, out = run_sql("SELECT count(*) FROM kb.library", (), principal=who)
    assert rc == 0 and out.strip() == "0", out


def test_a_provider_that_raises_is_recorded_not_swallowed(
    organisation, world, people, db, exploding_revoker
):
    """A timeout is outstanding work, not a silent success and not a crash."""
    who = uuid.uuid4()
    world.membership(who)

    result = deactivate_member(
        db,
        people.principal(people.admin),
        organisation_id=organisation,
        target=who,
        revoker=exploding_revoker,
    )

    assert exploding_revoker.observed[0]["status"] == "deactivated"
    assert result.provider_revocated is False
    assert result.provider_reason == "provider_unreachable"
    actions = [row[1] for row in world.journal()]
    assert "sessions_revocation_failed" in actions, actions
    assert "sessions_revoked" not in actions, actions


def test_a_retry_after_a_failure_does_not_repeat_the_close(
    organisation, world, people, db, observing_revoker_factory, failing_revoker
):
    """The retry is the second half only.

    Re-running the first half would fail against a membership that is no longer
    active, which is correct behaviour and would read like a new problem to
    whoever saw the error.
    """
    who = uuid.uuid4()
    world.membership(who)
    admin = people.principal(people.admin)

    deactivate_member(db, admin, organisation_id=organisation, target=who, revoker=failing_revoker)
    assert outstanding_revocations(db, admin, organisation) == [who]

    # the second attempt, this time answered
    good = observing_revoker_factory()
    results = retry_outstanding_revocations(db, admin, organisation_id=organisation, revoker=good)

    assert [r.principal_id for r in results] == [who]
    assert all(r.provider_revocated for r in results)
    assert outstanding_revocations(db, admin, organisation) == []
    actions = [row[1] for row in world.journal()]
    assert actions.count("membership_deactivated") == 1, actions
    assert actions.count("sessions_revoked") == 1, actions


def test_the_journal_never_carries_an_upstream_body(
    organisation, world, people, db, chatty_revoker
):
    """Only bounded, flat values are journaled.

    A provider's response body is where a token, a client id or a realm name
    would end up, and the journal is readable by an administrator and outlives
    the request that wrote it.
    """
    who = uuid.uuid4()
    world.membership(who)
    deactivate_member(
        db,
        people.principal(people.admin),
        organisation_id=organisation,
        target=who,
        revoker=chatty_revoker,
    )

    entries = world.journal("sessions_revoked")
    assert len(entries) == 1
    detail = entries[0][4]
    assert detail["reason"] == "logged_out"
    # an allow-listed key survives, with its type and bound enforced
    assert detail["sessions"] == 3
    # and nothing else the provider chose is written at all
    assert "token" not in detail, "a bearer token was journaled"
    assert "body" not in detail, "an unbounded provider string was journaled"
    assert "client_id" not in detail, "a key outside the agreed vocabulary was journaled"
    assert "nested" not in detail, "a non-scalar was journaled"
    assert set(detail) <= {"reason", "status", "sessions", "issuer_host"}, detail


# ------------------------------------------------------------- the gate too


def test_a_deactivated_principal_is_blocked_at_the_gate(
    organisation, world, people, db, observing_revoker
):
    who = uuid.uuid4()
    world.membership(who)
    me = people.principal(who)

    before = block_state(db, me)
    assert before.blocked is False

    deactivate_member(
        db,
        people.principal(people.admin),
        organisation_id=organisation,
        target=who,
        revoker=observing_revoker,
    )

    after = block_state(db, me)
    assert after.blocked is True
    assert after.status is not None and after.status.value == "deactivated"
    assert after.deactivated_by == people.admin
    assert after.deactivated_at is not None


def test_a_principal_with_no_membership_row_is_not_blocked(world, people, db):
    """Absence is not a block, and it is not a grant either.

    A principal who never went through the invite lifecycle is not closed. The
    direction matters: the gate denies on a recorded state, and the only way to
    record one is a function the runtime role cannot bypass.
    """
    state = block_state(db, people.principal(people.stranger))
    assert state.blocked is False
    assert state.status is None
    assert state.deactivated_at is None


def test_the_membership_close_cannot_be_repeated(world, people, db, observing_revoker, run_sql):
    """A second deactivation is refused, and says nothing about the first.

    'No such active member' and 'you may not see it' are the same answer, so a
    caller cannot learn about another organisation's membership by asking twice.
    """
    org = world.organisation()
    who = uuid.uuid4()
    world.membership(who)
    rc, _ = run_sql("SELECT kb.claim_organisation_admin(%s)", (org,), principal=people.admin)
    assert rc == 0

    admin = people.principal(people.admin)
    deactivate_member(db, admin, organisation_id=org, target=who, revoker=observing_revoker)
    rc, out = run_sql(
        "SELECT kb.deactivate_membership(%s, %s, 'again')", (org, who), principal=people.admin
    )
    assert rc != 0, "a second deactivation must be refused"
    assert "no such active member" in out, out
    # the first deactivation is untouched by the failed second attempt
    assert world.membership_row(who)[0] == "deactivated"


def test_a_non_admin_cannot_block_anybody(world, people, db, run_sql):
    """The actor check happens before a row exists."""
    org = world.organisation()
    victim = uuid.uuid4()
    world.membership(victim)

    rc, out = run_sql(
        "SELECT kb.deactivate_membership(%s, %s, 'bye')", (org, victim), principal=people.stranger
    )
    assert rc != 0
    assert "only an organisation administrator" in out, out
    assert world.membership_row(victim)[0] == "active"


def test_anybody_may_block_themselves(world, people, db, run_sql):
    """Leaving is not an administrative act and must not require one."""
    org = world.organisation()
    who = uuid.uuid4()
    world.membership(who)

    rc, out = run_sql(
        "SELECT kb.deactivate_membership(%s, %s, 'leaving')", (org, who), principal=who
    )
    assert rc == 0, out
    assert world.membership_row(who)[0] == "deactivated"
    assert world.membership_row(who)[3] == "leaving"
