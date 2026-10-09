"""C15 — invariants that need no database, so they run inside ``just check``.

Everything here is a statement about *shape*: what a model will accept, what
the package is allowed to reference, what the migration says about privileges.
None of it needs PostgreSQL, which is the point — a contract regression should
be caught by the fast gate, not only by the slow one.

The database-enforced half of the same rules lives in
``test_account_registry.py``, ``test_boundaries.py`` and
``test_generation_policy.py``. These are deliberately the checks that cannot
drift away from the code by being reimplemented.
"""

from __future__ import annotations

import ast
import pathlib
import re
from uuid import uuid4

import pytest
from pydantic import ValidationError

from kb.retrieval.provisioning import (
    DEFAULT_ROOT_PATH,
    IssuedKey,
    RequestProvisioning,
    RestrictedAcl,
    UnconfiguredIndexAdmin,
    UnconfiguredSecretSink,
    build_account_spec,
    secret_ref_for,
)
from kb.retrieval.provisioning_generations import (
    GLOBAL_CONCURRENCY,
    GenerationPermit,
    IndexStatus,
    SetGenerationPolicy,
    mint_permit,
)
from kb.retrieval.provisioning_search import READ_MODE, SearchQuery

REPO = pathlib.Path(__file__).resolve().parents[3]
PACKAGE = REPO / "src" / "kb" / "retrieval"
MIGRATION = REPO / "migrations" / "0004_provisioning.sql"

#: A stand-in for a key that is obviously not one. S105/S106 fire on anything
#: that looks like a credential, which is the right instinct and the wrong
#: tool for a test that needs a distinctive string to search for.
FAKE_KEY = "SUPER-SECRET-VALUE"


# ------------------------------------------------------------------- naming


def test_every_name_is_derived_from_the_library_id():
    """One library, one account, one read identity, one index identity.

    Deterministic, because a resumed run has to arrive at the same names the
    interrupted one used. And derived, because a name that is not a function
    of the id is a name somebody chose.
    """
    library = uuid4()
    spec = build_account_spec(library)
    assert spec.account_ref == f"kb-lib-{library.hex}"
    assert spec.read_identity == f"kb-svc-read-{library.hex}"
    assert spec.index_identity == f"kb-svc-index-{library.hex}"
    assert spec.read_identity != spec.index_identity
    assert spec.root_path == DEFAULT_ROOT_PATH
    assert spec == build_account_spec(library)
    assert spec != build_account_spec(uuid4())


def test_the_generated_acl_grants_read_and_index_and_nothing_else():
    spec = build_account_spec(uuid4())
    assert spec.acl.inherit_from_parent is False
    assert spec.acl.principals() == {spec.read_identity, spec.index_identity}
    rights = {right for entry in spec.acl.entries for right in entry.rights}
    assert rights == {"read", "index"}
    assert "manage" not in rights


def test_secret_refs_are_a_path_under_the_closed_store():
    spec = build_account_spec(uuid4())
    ref = secret_ref_for(spec.index_identity)
    assert ref.path.startswith("kb-secrets/")
    assert spec.index_identity in ref.path


# -------------------------------------------------------------------- models


@pytest.mark.parametrize(
    "document",
    [
        {
            "inherit_from_parent": True,
            "entries": [{"principal": "kb-svc-read-" + "0" * 32, "rights": ["read"]}],
        },
        {"inherit_from_parent": False, "entries": [{"principal": "*", "rights": ["read"]}]},
        {
            "inherit_from_parent": False,
            "entries": [{"principal": "kb-svc-read-" + "0" * 32, "rights": ["manage"]}],
        },
        {
            "inherit_from_parent": False,
            "entries": [{"principal": "kb-svc-read-" + "0" * 32, "rights": []}],
        },
        {"inherit_from_parent": False, "entries": []},
        {
            "inherit_from_parent": False,
            "entries": [{"principal": "kb-svc-read-" + "0" * 32, "rights": ["read", "read"]}],
        },
    ],
)
def test_the_acl_model_refuses_a_loose_document(document):
    """The Python half of a rule the database also enforces.

    The model is not the authority. It is here so a caller gets a clear error
    before a round trip — and it must not accept a document the trigger would
    refuse, or the two would disagree in the one place a user sees.
    """
    with pytest.raises(ValidationError):
        RestrictedAcl.model_validate(document)


@pytest.mark.parametrize(
    "field",
    ["requested_by", "account_ref", "root_uri", "uri", "dimension", "root_key", "state"],
)
def test_the_request_model_has_exactly_one_field(field):
    """A provisioning request is a library id and nothing else.

    `extra="forbid"` is the mechanism, so a caller who sends a root key, an
    account name or somebody else's identity gets a validation error rather
    than a field that is quietly dropped on the floor.
    """
    payload = {"library_id": str(uuid4()), field: "anything"}
    with pytest.raises(ValidationError):
        RequestProvisioning.model_validate(payload)
    assert set(RequestProvisioning.model_fields) == {"library_id"}


@pytest.mark.parametrize("field", ["account_ref", "uri", "generate", "mode", "rerank", "user_id"])
def test_the_search_query_model_has_no_authority_fields(field):
    """A02 at the boundary: a caller cannot aim the index or claim an identity."""
    with pytest.raises(ValidationError):
        SearchQuery.model_validate({"text": "x", field: "anything"})
    assert set(SearchQuery.model_fields) == {"text", "library_ids", "limit"}


def test_search_is_vectors_only_and_the_mode_is_not_a_parameter():
    """The read profile is a property of the index, not something a caller picks."""
    assert READ_MODE == "vectors_only"
    with pytest.raises(ValidationError):
        SearchQuery.model_validate({"text": "x", "mode": "generative"})


def test_concurrency_above_one_is_refused_at_the_model():
    """A column that says 3 while the installation allows 1 is a lie."""
    assert GLOBAL_CONCURRENCY == 1
    with pytest.raises(ValidationError, match="concurrency is fixed at 1"):
        SetGenerationPolicy(generation_allowed=True, max_concurrency=2)
    assert SetGenerationPolicy(generation_allowed=True).max_concurrency == 1


def test_index_status_explains_itself_in_a_closed_vocabulary():
    """The reason a library was skipped is one of three known strings."""
    behind = IndexStatus(
        library_id=uuid4(), library_generation=3, current_generation=1, index_ready=False
    )
    assert behind.skip_reason() == "index_behind"
    rebuilding = IndexStatus(
        library_id=uuid4(),
        library_generation=1,
        current_generation=1,
        building_generation=2,
        index_ready=False,
    )
    assert rebuilding.skip_reason() == "rebuild_in_progress"
    ready = IndexStatus(
        library_id=uuid4(), library_generation=1, current_generation=1, index_ready=True
    )
    assert ready.skip_reason() is None


# --------------------------------------------------------------------- keys


def test_a_key_never_appears_in_a_repr():
    """Printing a key-bearing object must not print the key.

    A dataclass field with ``repr=False`` does this; the test is what stops
    somebody from "simplifying" it back into a plain field, which is the kind
    of change that never shows up in review and always shows up in a log.
    """
    key = IssuedKey(identity="kb-svc-read-" + "0" * 32, secret=FAKE_KEY)
    assert FAKE_KEY not in repr(key)
    assert FAKE_KEY not in str(key)
    assert "<withheld>" in repr(key)
    assert key.secret == FAKE_KEY


def test_a_permit_names_a_library_a_generation_and_a_content_hash():
    """Authority is an object, not a boolean somebody can assert."""
    permit = mint_permit(uuid4(), 3, "a" * 64)
    assert isinstance(permit, GenerationPermit)
    assert permit.generation == 3
    with pytest.raises(ValidationError):
        GenerationPermit(library_id=uuid4(), generation=0, content_hash="a" * 64)
    with pytest.raises(ValidationError):
        GenerationPermit(library_id=uuid4(), generation=1, content_hash="not-a-hash")


# --------------------------------------------------------------- the boundary


def test_the_unconfigured_admin_refuses_and_names_the_dependency():
    """The absence of OpenViking is a runtime answer, not a stub that works."""
    spec = build_account_spec(uuid4())
    admin = UnconfiguredIndexAdmin()
    with pytest.raises(Exception, match="no OpenViking admin endpoint"):
        admin.create_account(spec)
    with pytest.raises(Exception, match="no OpenViking admin endpoint"):
        admin.issue_service_key(spec, spec.read_identity)
    with pytest.raises(Exception, match="no OpenViking admin endpoint"):
        UnconfiguredSecretSink().write(spec.read_identity, "not-a-real-key")


def test_no_module_of_the_one_shot_can_reach_a_container_runtime():
    """No subprocess, no shell, no socket path — in any file of the package.

    Read from the AST with docstrings and comments stripped, so the check
    measures code rather than the sentences that describe the rule. A grep
    would flag this very file.
    """
    offenders: list[str] = []
    forbidden = (
        "subprocess",
        "podman",
        "docker.sock",
        "podman.sock",
        "DOCKER_HOST",
        "/run/podman",
        "/var/run/docker",
        "os.system",
    )
    for path in sorted(PACKAGE.glob("provisioning*.py")):
        code = _code_only(path)
        for needle in forbidden:
            if needle in code:
                offenders.append(f"{path.name}: {needle}")
    assert offenders == [], offenders


def _code_only(path: pathlib.Path) -> str:
    """Module source with docstrings and comments removed."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    spans: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and ast.get_docstring(node, clean=False) is not None
            and node.body
        ):
            first = node.body[0]
            spans.append((first.lineno, first.end_lineno or first.lineno))
    kept = []
    for number, line in enumerate(source.splitlines(), start=1):
        if any(start <= number <= end for start, end in spans):
            continue
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        kept.append(line.split("  #")[0] if "  #" in line else line)
    return "\n".join(kept)


def test_the_generative_door_is_the_only_name_bound_to_a_model_runner():
    """Exactly one module may hold a generative port.

    Not "no module may" — the processing pipeline needs one eventually. The
    claim is that there is one door, and that the search module is not it.
    """
    holders = []
    for path in sorted(PACKAGE.glob("provisioning*.py")):
        if "provisioning_generative" in path.name:
            continue
        code = _code_only(path)
        if "GenerativePort" in code or "generate(" in code:
            holders.append(path.name)
    assert holders == [], holders


# -------------------------------------------------------------- the migration


def test_the_migration_states_its_role_restrictions():
    """Read the DDL, so a later edit that widens a role fails here.

    The provisioning role is created NOSUPERUSER NOBYPASSRLS, the worker is
    revoked from the provisioning tables, and nothing is granted to PUBLIC.
    Those three are the difference between a narrow one-shot and a wide one.
    """
    text = MIGRATION.read_text(encoding="utf-8")
    assert "CREATE ROLE kb_provisioner LOGIN NOSUPERUSER NOBYPASSRLS" in text
    assert re.search(r"REVOKE ALL ON [^;]*kb_worker", text, re.DOTALL)
    grants_to_public = re.findall(r"GRANT[^;]*\bTO\s+PUBLIC", text, re.DOTALL)
    assert grants_to_public == [], grants_to_public


def test_the_migration_grants_the_worker_no_provisioning_function():
    text = MIGRATION.read_text(encoding="utf-8")
    for function in ("admit_index_rebuild", "publish_index_generation", "fail_index_generation"):
        for match in re.finditer(rf"GRANT EXECUTE ON FUNCTION[^;]*\b{function}[^;]*;", text):
            assert "kb_worker" not in match.group(0), match.group(0)


def test_the_migration_widens_no_existing_table_grant_to_the_gateway():
    """``kb_app`` gets SELECT on the account and nothing more.

    A blanket ``GRANT ... ON ALL TABLES`` in this file would silently hand the
    gateway INSERT on the account table, undoing the card's first acceptance
    criterion in a line nobody reads.
    """
    text = MIGRATION.read_text(encoding="utf-8")
    for match in re.finditer(r"GRANT[^;]*kb_app[^;]*;", text, re.DOTALL):
        statement = match.group(0)
        if "index_account" in statement:
            assert "INSERT" not in statement and "UPDATE" not in statement, statement
