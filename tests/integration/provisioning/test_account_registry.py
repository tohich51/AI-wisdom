"""C15 — the account registry, on a real PostgreSQL 16.2.

Every test here is a negative against the database. The interesting property
of a provisioning card is what it *refuses*: a wildcard over a published root,
a key in the registry, a root that escapes its own account, an identity that
belongs to somebody else's account. A test that only proved the happy path
would prove that INSERT works, which PostgreSQL has always done.
"""

from __future__ import annotations

import json
import uuid

import psycopg
import pytest
from pydantic import ValidationError

from kb.retrieval.provisioning import (
    RestrictedAcl,
    build_account_spec,
    credential_refs,
    list_accounts,
    load_account,
    secret_ref_for,
)

pytestmark = pytest.mark.integration


# ------------------------------------------------------------------- naming


def test_the_names_are_derived_from_the_library_and_nothing_else(world):
    """Two calls, one library, byte-identical names.

    Determinism is what makes a resumed run able to continue: the second
    attempt has to arrive at the names the first one used, and it can only do
    that if the names are a function of the library id.
    """
    library, _people = world.library_with(("owner", "manager"))
    first = build_account_spec(library)
    second = build_account_spec(library)
    assert first == second
    other = build_account_spec(uuid.uuid4())
    assert other.account_ref != first.account_ref


def test_each_library_gets_its_own_account_and_identities(world):
    library, _people = world.library_with(("owner", "manager"))
    spec = build_account_spec(library)
    world.insert_account(library, state="ready")

    stored = world.account_row(library)
    assert stored is not None
    assert stored["account_ref"] == spec.account_ref
    # Two identities, and they are not the same string. "Explicit service
    # identities" means the reader cannot also write.
    assert stored["read_identity"] != stored["index_identity"]
    assert stored["read_identity"].startswith("kb-svc-read-")
    assert stored["index_identity"].startswith("kb-svc-index-")


def test_a_library_cannot_have_two_accounts(world):
    library, _people = world.library_with(("owner", "manager"))
    world.insert_account(library)
    with pytest.raises(psycopg.errors.UniqueViolation):
        world.insert_account(library, account_ref=f"kb-lib-{uuid.uuid4().hex}")


def test_the_dimension_is_recorded_rather_than_assumed(world):
    """A 768-dimension account is storable — and readable back as 768.

    The point is not that 1024 is right. It is that the number is a property of
    the account that somebody can check, instead of a constant the reader
    assumes. A row at the wrong dimension is then a visible fact instead of a
    silent corruption.
    """
    library, _people = world.library_with(("owner", "manager"))
    world.insert_account(library, dimension=768, embedding_profile="some-other-model")
    assert world.account_row(library)["dimension"] == 768
    with pytest.raises(psycopg.errors.CheckViolation, match="dimension_is_a_positive_integer"):
        world.insert_account(uuid.uuid4(), dimension=0, account_ref=f"kb-lib-{uuid.uuid4().hex}")


# ------------------------------------------------------------- restricted acl


def _acl_for(library, **overrides) -> dict[str, object]:
    hexlib = f"{library.hex}"
    document = {
        "inherit_from_parent": False,
        "entries": [
            {"principal": f"kb-svc-read-{hexlib}", "rights": ["read"]},
            {"principal": f"kb-svc-index-{hexlib}", "rights": ["index", "read"]},
        ],
    }
    document.update(overrides)
    return document


def _insert_with_acl(world, library, acl: dict[str, object]) -> None:
    world.insert_account(
        library,
        acl=acl,
        account_ref=f"kb-lib-{library.hex}",
        read_identity=f"kb-svc-read-{library.hex}",
        index_identity=f"kb-svc-index-{library.hex}",
    )


def test_the_happy_acl_is_explicit_and_limited_to_read_and_index(world):
    library, _people = world.library_with(("owner", "manager"))
    _insert_with_acl(world, library, _acl_for(library))
    entries = world.account_row(library)["acl"]["entries"]
    assert len(entries) == 2
    rights = {right for entry in entries for right in entry["rights"]}
    assert rights <= {"read", "index"}
    assert "manage" not in rights


@pytest.mark.parametrize(
    ("case", "acl", "expected"),
    [
        (
            "a wildcard principal",
            {"inherit_from_parent": False, "entries": [{"principal": "*", "rights": ["read"]}]},
            "not one of this account's own service identities",
        ),
        (
            "manage on the published root",
            None,  # filled in below
            "not grantable on a published root",
        ),
        (
            "inheritance from the parent namespace",
            None,
            "may not inherit ACLs",
        ),
        (
            "somebody else's service identity",
            None,
            "not one of this account's own service identities",
        ),
        (
            "an empty grant list",
            {"inherit_from_parent": False, "entries": []},
            "empty; a restricted root needs explicit grants",
        ),
        (
            "a right that is not in the vocabulary",
            None,
            "not grantable on a published root",
        ),
    ],
)
def test_a_published_root_refuses_a_loose_acl(world, case, acl, expected):
    """Acceptance criterion 4, as a database constraint rather than a promise.

    Each case is a document a *different* implementation might have written:
    a wildcard, a manage, an inherited grant, a third party's identity, an
    empty allow-list, an invented right. The trigger refuses all six.
    """
    library, _people = world.library_with(("owner", "manager"))
    hexlib = f"{library.hex}"
    if acl is None:
        if case == "manage on the published root":
            acl = {
                "inherit_from_parent": False,
                "entries": [{"principal": f"kb-svc-index-{hexlib}", "rights": ["manage", "read"]}],
            }
        elif case == "inheritance from the parent namespace":
            acl = {
                "inherit_from_parent": True,
                "entries": [{"principal": f"kb-svc-read-{hexlib}", "rights": ["read"]}],
            }
        elif case == "somebody else's service identity":
            other = uuid.uuid4().hex
            acl = {
                "inherit_from_parent": False,
                "entries": [
                    {"principal": f"kb-svc-read-{hexlib}", "rights": ["read"]},
                    {"principal": f"kb-svc-index-{other}", "rights": ["index"]},
                ],
            }
        else:  # a right that is not in the vocabulary
            acl = {
                "inherit_from_parent": False,
                "entries": [{"principal": f"kb-svc-read-{hexlib}", "rights": ["read", "admin"]}],
            }
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match=expected):
        _insert_with_acl(world, library, acl)


def test_the_acl_cannot_be_loosened_after_the_fact(world):
    """The trigger fires on UPDATE too, not only on INSERT.

    A check that only guards creation is a check that a second statement walks
    straight past.
    """
    library, _people = world.library_with(("owner", "manager"))
    _insert_with_acl(world, library, _acl_for(library))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        world.conn.execute(
            "UPDATE kb.index_account SET acl = %s::jsonb WHERE library_id = %s",
            (
                json.dumps(_acl_for(library, entries=[{"principal": "*", "rights": ["manage"]}])),
                library,
            ),
        )


# ----------------------------------------------------------------- root path


@pytest.mark.parametrize(
    ("root_path", "why"),
    [
        ("openviking://other-account/published", "an absolute vendor URI is a foreign namespace"),
        ("/../../etc", "traversal out of the account"),
        ("/*", "a wildcard root"),
        ("/index/../admin", "traversal that looks local"),
    ],
)
def test_the_root_path_cannot_escape_its_own_account(world, root_path, why):
    library, _people = world.library_with(("owner", "manager"))
    with pytest.raises(
        psycopg.errors.CheckViolation, match="root_path_is_relative_to_its_own_account"
    ):
        world.insert_account(library, root_path=root_path)


# ------------------------------------------------------------------- secrets


def test_the_registry_refuses_to_store_key_material(world):
    """A bare key is rejected by a CHECK, so a future bug fails loudly.

    The pattern is the control: the column holds a *reference* into a closed
    store, and a key does not look like a path. That is a database statement
    about secrets rather than a code review promise.
    """
    library, _people = world.library_with(("owner", "manager"))
    world.insert_account(library)
    with pytest.raises(
        psycopg.errors.CheckViolation, match="index_credential_ref_secret_ref_check"
    ):
        world.credential_ref(library, f"kb-svc-read-{library.hex}", "sk-" + "0" * 48)
    # The reference shape is accepted, and nothing more.
    world.credential_ref(
        library, f"kb-svc-read-{library.hex}", f"kb-secrets/kb-svc-read-{library.hex}/key"
    )
    row = world.conn.execute(
        "SELECT secret_ref FROM kb.index_credential_ref WHERE library_id = %s", (library,)
    ).fetchone()
    assert row[0].startswith("kb-secrets/")


def test_a_key_cannot_be_smuggled_in_as_an_identity_name(world):
    """Two constraints stand between a credential and the identity column.

    The ACL trigger fires first, because an identity that is not one of the
    account's own cannot appear in its ACL either; the column CHECK is the
    backstop that survives an ACL document written to match. Both have to
    refuse, and the test asserts the refusal without pinning which one spoke
    first — a test that pins the order breaks the moment somebody makes the
    rule stricter, and then it protects nothing.
    """
    library, _people = world.library_with(("owner", "manager"))
    with pytest.raises(psycopg.Error) as caught:
        world.insert_account(
            library,
            account_ref=f"kb-lib-{library.hex}",
            read_identity="sk-" + "a" * 48,
            index_identity=f"kb-svc-index-{library.hex}",
            acl={
                "inherit_from_parent": False,
                "entries": [{"principal": "sk-" + "a" * 48, "rights": ["read"]}],
            },
        )
    message = str(caught.value)
    # PostgreSQL checks the column constraint before it fires the row trigger
    # on this path, so the CHECK is what usually speaks. Both are refusals and
    # the test accepts either rather than pinning the order.
    assert (
        "identities_are_explicit_and_per_role" in message
        or ("read_identity_is_explicit_and_per_role" in message)
        or ("not one of this account's own service identities" in message)
    ), message


def test_a_reader_sees_the_mapping_and_never_a_secret_reference(gateway, acting, world):
    """The account row a reader may see has no secret-adjacent column in it.

    This is why ``kb.index_credential_ref`` is a separate table rather than two
    columns on the account: a column filter can be undone by the next
    ``SELECT *``, a table boundary cannot be undone by a forgotten WHERE.
    """
    library, _people = world.library_with(("owner", "manager"), ("colleague", "reader"))
    world.insert_account(library, state="ready")
    world.credential_ref(library, f"kb-svc-read-{library.hex}", f"kb-secrets/{library.hex}/read")

    account = load_account(gateway, world.reader, library)
    assert account is not None
    assert account.account_ref.startswith("kb-lib-")
    # kb_app holds no SELECT privilege on the credential table at all, so this
    # is an empty answer rather than an error, and not even a policy decision.
    assert credential_refs(gateway, world.reader, library) == []
    with acting(world, "colleague") as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT secret_ref FROM kb.index_credential_ref").fetchall()

    # The manager does not see it either. `kb_app` was never granted SELECT on
    # the credential table — not for readers, not for managers — because the
    # gateway has no business resolving a path into a sealed store, and a
    # policy that is sometimes right is a policy that will be wrong once. The
    # only reader is the one-shot; see
    # test_provisioning_resume.py, which reads the reference over the
    # kb_provisioner connection.
    assert credential_refs(gateway, world.manager, library) == []
    with acting(world, "owner") as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT secret_ref FROM kb.index_credential_ref").fetchall()


def test_a_stranger_sees_no_account_at_all(gateway, world):
    library, _people = world.library_with(("owner", "manager"))
    world.insert_account(library, state="ready")
    assert load_account(gateway, world.nobody, library) is None
    assert list_accounts(gateway, world.nobody) == []


def test_a_manager_lists_exactly_the_libraries_they_manage(gateway, world):
    mine, _p1 = world.library_with(("owner", "manager"))
    world.insert_account(mine, state="ready")
    theirs, _p2 = world.library_with(("colleague", "manager"))
    world.insert_account(theirs, state="ready")

    visible = [a.library_id for a in list_accounts(gateway, world.manager)]
    assert visible == [mine]


# -------------------------------------------------------------------- model


def test_the_acl_model_agrees_with_the_database():
    """The Python model refuses what the trigger refuses.

    Two implementations of one rule, in two languages, that have to be kept in
    step. The model is not the authority — the database is — but a caller
    should get a clear error before a round trip, and the two must not disagree
    about which documents are acceptable.
    """
    read = "kb-svc-read-" + "0" * 32
    index = "kb-svc-index-" + "0" * 32
    good = RestrictedAcl(entries=[{"principal": read, "rights": ["read"]}])
    assert good.inherit_from_parent is False

    # inheritance, wildcard, manage, empty allow-list: the four shapes the
    # trigger refuses, refused here before a round trip.
    for bad in (
        {"inherit_from_parent": True, "entries": [{"principal": read, "rights": ["read"]}]},
        {"inherit_from_parent": False, "entries": [{"principal": "*", "rights": ["read"]}]},
        {"inherit_from_parent": False, "entries": [{"principal": index, "rights": ["manage"]}]},
        {"inherit_from_parent": False, "entries": []},
        {
            "inherit_from_parent": False,
            "entries": [{"principal": read, "rights": ["read", "read"]}],
        },
    ):
        with pytest.raises(ValidationError):
            RestrictedAcl.model_validate(bad)


def test_secret_refs_are_a_pure_function_of_the_identity():
    spec = build_account_spec(uuid.uuid4())
    assert secret_ref_for(spec.read_identity).path == f"kb-secrets/{spec.read_identity}/key"
    assert secret_ref_for(spec.read_identity) == secret_ref_for(spec.read_identity)
