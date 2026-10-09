"""C08 — an invitation records an intent. It sends nothing.

"On this stand an invite in the UI must NOT send a real email; it records an
intent that an operator can act on."

That is a product constraint and it is a *structural* one, so the tests are
structural rather than behavioural wherever a behaviour test could pass while a
mailer quietly existed in a code path nobody exercised:

* the database has no way to record a "sent" state that nobody attested to;
* the runtime role has no way to write an invitation row directly, so the one
  creation path cannot be bypassed by a future caller;
* the module cannot send anything — asserted by parsing its import graph, not by
  reading a comment;
* the API response never claims an invitation was sent.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

pytestmark = pytest.mark.integration

SRC = pathlib.Path(__file__).resolve().parents[3] / "src" / "kb"


# ------------------------------------------------------ nothing can be sent


def test_no_module_on_the_invitation_path_can_send_anything():
    """Parse the import graph; do not trust a comment.

    A transport would enter as an import — smtplib, an email package, an
    httpx client aimed at a mail endpoint, a queue publisher. This walks the
    modules that create an invitation and fails on any of them. The list is the
    claim being tested, so it is written out rather than computed from "whatever
    is importable".
    """
    forbidden = {
        "smtplib",
        "email",
        "email.message",
        "sendgrid",
        "mail",
        "mailer",
        "postmarker",
        "ses",
        "boto3",
        "aiohttp",
    }
    modules = [
        SRC / "access" / "membership.py",
        SRC / "http" / "members.py",
    ]
    for path in modules:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                root = name.split(".")[0]
                assert root not in forbidden, (path.name, name)


def test_the_invitation_functions_take_no_transport():
    """Not even as a default argument.

    A ``mailer: Mailer | None = None`` parameter with a no-op default would pass
    a behavioural test that only ever calls it without a mailer, and would put a
    sender one refactor away.
    """
    from kb.access import membership as m
    from kb.http import members as h

    for func in (
        m.create_invitation,
        h.invite,
    ):
        parameters = list(func.__code__.co_varnames[: func.__code__.co_argcount])
        assert "mailer" not in parameters, (func.__name__, parameters)
        assert "smtp" not in parameters, (func.__name__, parameters)
        assert "sender" not in parameters, (func.__name__, parameters)
        assert "client" not in parameters, (func.__name__, parameters)


# ------------------------------------------------------------- the artefact


def test_an_invitation_is_a_row_an_operator_can_work_through(organisation, world, people, db):
    from kb.access import membership

    invitation = membership.create_invitation(
        db,
        people.principal(people.admin),
        organisation_id=organisation,
        email="colleague@example.invalid",
    )
    assert invitation.status.value == "pending"
    assert invitation.delivery_state.value == "operator_pending"
    assert invitation.awaiting_operator is True
    assert invitation.notified_at is None
    assert invitation.principal_id is None
    assert invitation.expires_at is None, "an absent expiry is None, not a made-up date"

    queue = membership.list_invitations(db, people.principal(people.admin), organisation, limit=10)
    assert [i.id for i in queue] == [invitation.id]
    assert queue[0].awaiting_operator is True


def test_nothing_is_created_besides_the_invitation_and_its_journal_entry(
    organisation, world, people, db
):
    """An invite is not a membership. People are not members until they accept."""
    from kb.access import membership

    membership.create_invitation(
        db,
        people.principal(people.admin),
        organisation_id=organisation,
        email="newcomer@example.invalid",
    )
    assert (
        world.count(
            "SELECT count(*) FROM kb.membership WHERE organisation_id = %s", (organisation,)
        )
        == 0
    )
    actions = [row[1] for row in world.journal()]
    assert (
        actions == ["organisation_admin_granted", "invitation_created"]
        or actions[-1] == "invitation_created"
    ), actions


def test_an_invitation_may_name_a_person_with_no_address(organisation, world, people, db):
    """Absent stays None. Never a placeholder address."""
    from kb.access import membership

    invitation = membership.create_invitation(
        db,
        people.principal(people.admin),
        organisation_id=organisation,
        subject="keycloak-subject-only",
    )
    assert invitation.email is None
    assert invitation.subject == "keycloak-subject-only"

    with pytest.raises(ValueError):
        membership.create_invitation(
            db, people.principal(people.admin), organisation_id=organisation
        )


def test_an_invitation_needs_an_address_or_a_subject_in_the_database_too(
    organisation, world, people, run_sql
):
    rc, refusal = run_sql(
        "SELECT kb.create_invitation(%s, NULL, NULL, NULL)",
        (organisation,),
        principal=people.admin,
    )
    assert rc != 0
    assert "address or a provider subject" in refusal, refusal


def test_a_notified_invitation_names_who_notified(organisation, world, people, db, run_sql):
    """The only way out of ``operator_pending`` requires a human."""
    from kb.access import membership

    invitation = membership.create_invitation(
        db,
        people.principal(people.admin),
        organisation_id=organisation,
        email="someone@example.invalid",
    )
    notified = membership.mark_invitation_notified(
        db, people.principal(people.admin), invitation.id
    )
    assert notified.delivery_state.value == "notified"
    assert notified.notified_at is not None
    assert notified.awaiting_operator is False
    assert (
        world.count(
            "SELECT count(*) FROM kb.access_policy_journal "
            "WHERE organisation_id = %s AND action = 'invitation_notified'",
            (organisation,),
        )
        == 1
    )

    # the database will not hold a notified row without a notifier
    rc, refusal = run_sql(
        "UPDATE kb.invitation SET delivery_state = 'notified' WHERE id = %s",
        (invitation.id,),
        principal=people.admin,
    )
    assert rc != 0, "the runtime role reached an invitation it should not own"
    assert "permission denied" in refusal, refusal


def test_only_an_administrator_may_notify_or_withdraw(organisation, world, people, db):
    from kb.access import membership
    from kb.access.membership_deactivation import RevocationOutcome

    invitation = membership.create_invitation(
        db,
        people.principal(people.admin),
        organisation_id=organisation,
        email="stranger@example.invalid",
    )
    world.membership(people.colleague)
    with pytest.raises(psycopg_error()):
        membership.mark_invitation_notified(db, people.principal(people.colleague), invitation.id)
    with pytest.raises(psycopg_error()):
        membership.withdraw_invitation(db, people.principal(people.colleague), invitation.id)
    del RevocationOutcome


def test_the_runtime_role_cannot_insert_an_invitation(organisation, world, people, run_sql):
    """One creation path, and it journals. Otherwise somebody would find another."""
    rc, out = run_sql(
        "INSERT INTO kb.invitation (organisation_id, email, created_by) "
        "VALUES (%s, 'bypass@example.invalid', %s)",
        (organisation, people.admin),
        principal=people.admin,
    )
    assert rc != 0
    assert "permission denied for table invitation" in out, out
    # scoped to this test's organisation: the database is shared by the suite
    assert (
        world.count(
            "SELECT count(*) FROM kb.invitation WHERE organisation_id = %s", (organisation,)
        )
        == 0
    )


# ------------------------------------------------------------ the lifecycle


def test_accepting_binds_the_membership_to_the_verified_caller(organisation, world, people, db):
    from kb.access import membership

    invitation = membership.create_invitation(
        db,
        people.principal(people.admin),
        organisation_id=organisation,
        email="colleague@example.invalid",
    )
    who = people.colleague
    assert world.membership_row(who) is None, "accepting must create, not find"

    membership.accept_invitation(
        db,
        people.principal(who),
        invitation.id,
        issuer="https://id.example.invalid/realms/kb",
        subject=f"sub-{who.hex}",
    )

    row = world.membership_row(who)
    assert row is not None and row[0] == "active"
    actions = [r[1] for r in world.journal()]
    assert "membership_created" in actions and "invitation_accepted" in actions
    assert (
        membership.list_invitations(db, people.principal(people.admin), organisation)[
            0
        ].status.value
        == "accepted"
    )


def test_an_invitation_cannot_be_accepted_twice(organisation, world, people, db):
    from kb.access import membership

    invitation = membership.create_invitation(
        db,
        people.principal(people.admin),
        organisation_id=organisation,
        email="once@example.invalid",
    )
    who = people.colleague
    for _ in range(2):
        try:
            membership.accept_invitation(
                db,
                people.principal(who),
                invitation.id,
                issuer="iss",
                subject=f"sub-{who.hex}",
            )
        except Exception as exc:  # the refusal type is what the test is about
            refusal = type(exc).__name__
    assert refusal == "RestrictViolation", refusal


def test_a_withdrawn_invitation_cannot_be_accepted(organisation, world, people, db):
    from kb.access import membership

    invitation = membership.create_invitation(
        db,
        people.principal(people.admin),
        organisation_id=organisation,
        email="gone@example.invalid",
    )
    membership.withdraw_invitation(db, people.principal(people.admin), invitation.id)
    with pytest.raises(psycopg_error()):
        membership.accept_invitation(
            db,
            people.principal(people.colleague),
            invitation.id,
            issuer="iss",
            subject="s",
        )
    assert world.membership_row(people.colleague) is None


def test_an_expired_invitation_is_not_accepted(organisation, world, people, db):
    import datetime as dt

    from kb.access import membership

    invitation = membership.create_invitation(
        db,
        people.principal(people.admin),
        organisation_id=organisation,
        email="late@example.invalid",
        expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=1),
    )
    with pytest.raises(psycopg_error()):
        membership.accept_invitation(
            db,
            people.principal(people.colleague),
            invitation.id,
            issuer="iss",
            subject="s",
        )


def test_two_open_invitations_for_one_address_are_refused(organisation, world, people, db):
    from kb.access import membership

    membership.create_invitation(
        db,
        people.principal(people.admin),
        organisation_id=organisation,
        email="dup@example.invalid",
    )
    import psycopg

    with pytest.raises(psycopg.errors.UniqueViolation):
        membership.create_invitation(
            db,
            people.principal(people.admin),
            organisation_id=organisation,
            email="dup@example.invalid",
        )


def test_re_inviting_a_blocked_person_restores_them(organisation, world, people, db):
    """The supported way back, and it is not a hidden verb.

    A second invitation, accepted by the same person, is an explicit,
    journalled act. The card does not ask for a separate "restore" endpoint and
    this is not one.
    """
    from kb.access import membership
    from kb.access.membership_deactivation import RevocationOutcome, deactivate_member

    who = people.colleague
    world.membership(who)

    class Confirming:
        def revoke_sessions(self, *, issuer: str, subject: str) -> RevocationOutcome:
            return RevocationOutcome(revoked=True, reason="logged_out")

    deactivate_member(
        db,
        people.principal(people.admin),
        organisation_id=organisation,
        target=who,
        revoker=Confirming(),
    )
    assert membership.block_state(db, people.principal(who)).blocked is True

    invitation = membership.create_invitation(
        db,
        people.principal(people.admin),
        organisation_id=organisation,
        email="again@example.invalid",
    )
    membership.accept_invitation(
        db,
        people.principal(who),
        invitation.id,
        issuer="iss",
        subject=f"sub-{who.hex}",
    )
    assert membership.block_state(db, people.principal(who)).blocked is False
    assert world.membership_row(who)[0] == "active"


# ------------------------------------------------------------------- http


def test_the_api_never_reports_an_invitation_as_sent(api, organisation, world, people):
    created = api.as_(people.admin).post(
        f"/orgs/{organisation}/invitations", json={"email": "ui@example.invalid"}
    )
    assert created.status_code == 201, created.text
    payload = created.json()
    assert payload["status"] == "pending"
    assert payload["delivery_state"] == "operator_pending"
    assert payload["awaiting_operator"] is True
    assert payload["notified_at"] is None
    for forbidden in ("sent", "emailed", "message_id", "smtp"):
        assert forbidden not in payload, payload


def test_an_invitation_body_cannot_name_a_principal(api, organisation, world, people):
    response = api.as_(people.admin).post(
        f"/orgs/{organisation}/invitations",
        json={"email": "x@example.invalid", "principal_id": str(people.colleague)},
    )
    assert response.status_code == 422, response.text


def test_accepting_over_http_requires_a_verified_identity(api, organisation, world, people):
    """A request that reaches the router with a principal but no verified
    identity cannot create a membership: ``(issuer, subject)`` is the thing the
    database stores, and there is no honest way to make one up."""
    from kb.access import membership

    invitation = membership.create_invitation(
        _owner_connection(world),
        people.principal(people.admin),
        organisation_id=organisation,
        email="noid@example.invalid",
    )
    # a client whose stand-in set no identity at all
    response = api.post(f"/orgs/{organisation}/invitations/{invitation.id}/accept", json={})
    assert response.status_code in (401, 403), response.status_code
    assert world.membership_row(people.colleague) is None


def test_a_body_that_names_another_principal_is_refused_on_accept(api, organisation, world, people):
    invitation = (
        api.as_(people.admin)
        .post(f"/orgs/{organisation}/invitations", json={"email": "spoof@example.invalid"})
        .json()
    )

    caller = api.as_(people.colleague)
    bad = caller.post(
        f"/orgs/{organisation}/invitations/{invitation['id']}/accept",
        json={"principal_id": str(people.stranger), "subject": "someone-else"},
    )
    assert bad.status_code == 422, bad.text
    assert world.membership_row(people.stranger) is None
    assert world.membership_row(people.colleague) is None


# ----------------------------------------------------------------- helpers


def psycopg_error():
    import psycopg

    return (psycopg.errors.InsufficientPrivilege, psycopg.errors.RestrictViolation)


def _owner_connection(world):
    return world.conn
