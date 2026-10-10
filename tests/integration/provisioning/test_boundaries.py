"""C15 — the boundaries, on a real PostgreSQL 16.2 and against real files.

The card's four acceptance criteria are structural, and each one is checked
against the thing that would have to break for it to fail:

1. *The gateway cannot create an arbitrary account* — asserted against
   PostgreSQL's own privilege and policy refusals, for ``kb_app`` and for the
   one-shot role acting without a manager.
2. *No Podman/Docker socket in the application* — asserted by reading the
   Python package and the Quadlet unit. There is no container runtime here to
   inspect, so reading the artifact is the strongest available check, and the
   runtime check is recorded as ``not_run``.
3. *Partial provisioning resumes without leaking keys* —
   ``test_provisioning_resume.py``; the queue half is here.
4. *No wildcard manage over a published root* —
   ``test_account_registry.py``; the "who may write an ACL" half is here.

The CLI tests run the real program in a subprocess against the real server.
Its nonzero exits are the receipts: a provisioning run that did not happen has
not succeeded, and a program that says otherwise is worse than no program.
"""

from __future__ import annotations

import ast
import json
import pathlib
import subprocess
import sys
from uuid import uuid4

import psycopg
import pytest
from pydantic import ValidationError

from kb.retrieval.provisioning import (
    RequestProvisioning,
    UnconfiguredIndexAdmin,
    UnconfiguredSecretSink,
)

pytestmark = pytest.mark.integration

PACKAGE = pathlib.Path(__file__).resolve().parents[3] / "src" / "kb" / "retrieval"
DEPLOY = pathlib.Path(__file__).resolve().parents[3] / "deploy" / "maintenance"

_ACCOUNT_INSERT = (
    "INSERT INTO kb.index_account (library_id, account_ref, read_identity, "
    "index_identity, root_path, dimension, embedding_profile, acl) "
    "VALUES (%s, %s, %s, %s, '/index', 1024, 'p', %s::jsonb)"
)


def _bools(out: str) -> list[bool]:
    """Parse the booleans psycopg renders as True/False in a tab-separated row."""
    return [value == "True" for value in out.split()]


def _acl(library) -> str:
    return json.dumps(
        {
            "inherit_from_parent": False,
            "entries": [{"principal": f"kb-svc-read-{library.hex}", "rights": ["read"]}],
        }
    )


def _account_params(library):
    return (
        library,
        f"kb-lib-{library.hex}",
        f"kb-svc-read-{library.hex}",
        f"kb-svc-index-{library.hex}",
        _acl(library),
    )


# ------------------------------------------- 1. the gateway cannot provision


def test_the_gateway_has_no_privilege_on_the_account_table(run_sql, schema):
    """Privileges, checked in the catalog rather than inferred from a failure."""
    rc, out = run_sql(
        "SELECT has_table_privilege('kb_app','kb.index_account','INSERT'), "
        "has_table_privilege('kb_app','kb.index_account','UPDATE'), "
        "has_table_privilege('kb_app','kb.index_account','DELETE')"
    )
    assert rc == 0
    assert _bools(out) == [False, False, False], out


def test_the_gateway_cannot_create_an_account_even_as_a_manager(run_sql, world):
    """Acceptance criterion 1, as a refusal from the server itself.

    The principal is a manager of a library that really exists, so the only
    thing standing between this INSERT and a provisioned account is the
    boundary. There are two more: the provisioning policy admits only the
    one-shot role, and the one-shot role still needs a manager principal.
    """
    library, _people = world.library_with(("owner", "manager"))
    rc, out = run_sql(
        _ACCOUNT_INSERT, role="kb_app", principal=world.owner, params=_account_params(library)
    )
    assert rc != 0, "the gateway created an account"
    assert "permission denied for table index_account" in out, out


def test_the_gateway_cannot_point_a_library_at_an_account_it_chose(run_sql, world):
    """Account names are server-generated, so a chosen one is not storable.

    The CHECK is the second lock on the same door: even a write that somehow
    reached the table cannot name an account, because the name has to be
    ``kb-lib-<the library id>``.
    """
    library, _people = world.library_with(("owner", "manager"))
    params = list(_account_params(library))
    params[1] = "attacker-chosen-account"
    rc, out = run_sql(
        _ACCOUNT_INSERT, role="kb_provisioner", principal=world.owner, params=tuple(params)
    )
    assert rc != 0
    assert "account_ref_is_server_generated" in out, out


def test_the_one_shot_cannot_provision_for_a_library_it_does_not_manage(run_sql, world):
    """Holding the root key is not authority. The grant is.

    The one-shot role may write this table — it has to, that is the job — but
    the policy also requires the transport principal to manage the library. A
    stolen root key plus a one-shot connection is still bounded by whoever's
    identity the run carries.
    """
    library, _people = world.library_with(("owner", "manager"), ("colleague", "reader"))
    rc, out = run_sql(
        _ACCOUNT_INSERT,
        role="kb_provisioner",
        principal=world.colleague,
        params=_account_params(library),
    )
    assert rc != 0
    assert "row-level security" in out.lower(), out


def test_the_one_shot_with_a_manager_principal_may_provision(run_sql, world):
    """The control: the same statement, a manager, is accepted.

    Without this the negative above would pass for the wrong reason — a
    mis-written INSERT that fails for everybody proves nothing about the
    policy.
    """
    library, _people = world.library_with(("owner", "manager"))
    rc, out = run_sql(
        _ACCOUNT_INSERT,
        role="kb_provisioner",
        principal=world.owner,
        params=_account_params(library),
    )
    assert rc == 0, out


# ---------------------------------------- the worker does not consume it


@pytest.mark.parametrize(
    "table",
    [
        "kb.provisioning_request",
        "kb.provisioning_run",
        "kb.index_account",
        "kb.index_credential_ref",
    ],
)
@pytest.mark.parametrize("statement", ["SELECT 1 FROM {t} LIMIT 1", "DELETE FROM {t}"])
def test_the_worker_cannot_reach_the_provisioning_tables(run_sql, schema, table, statement):
    """ACCESS-MODEL §5: the ordinary worker does not consume this queue.

    A worker that could read the queue would eventually drain it, and a
    process that can write an index credential is a process that can rotate
    one. The privilege is revoked in the migration and asserted here.
    """
    rc, out = run_sql(statement.format(t=table), role="kb_worker")
    assert rc != 0
    assert "permission denied" in out, out


def test_public_holds_no_provisioning_function_either(run_sql, schema):
    """PostgreSQL grants EXECUTE on every new function to PUBLIC by default.

    Found by the test above: the migration said the worker was not granted
    these functions, and ``has_function_privilege`` said otherwise, because a
    grant that does not revoke the default is not a grant. The same hole would
    have been open to every other role in the cluster, including future ones
    nobody has created yet.
    """
    rc, out = run_sql(
        "SELECT has_function_privilege('public',"
        "'kb.admit_index_rebuild(uuid,text)','EXECUTE'), "
        "has_function_privilege('public',"
        "'kb.publish_index_generation(uuid,integer,text)','EXECUTE'), "
        "has_function_privilege('public',"
        "'kb.fail_index_generation(uuid,integer,text)','EXECUTE')"
    )
    assert rc == 0
    assert _bools(out) == [False, False, False], out


def test_the_worker_holds_no_provisioning_function(run_sql, schema):
    """No EXECUTE on the generation functions either.

    The worker embeds; it does not open a generation and it does not publish
    one. A worker that could publish a generation could publish one for a
    library nobody asked about.
    """
    rc, out = run_sql(
        "SELECT has_function_privilege('kb_worker',"
        "'kb.admit_index_rebuild(uuid,text)','EXECUTE'), "
        "has_function_privilege('kb_worker',"
        "'kb.publish_index_generation(uuid,integer,text)','EXECUTE'), "
        "has_function_privilege('kb_worker',"
        "'kb.fail_index_generation(uuid,integer,text)','EXECUTE')"
    )
    assert rc == 0
    assert _bools(out) == [False, False, False], out


def test_the_worker_may_still_read_the_index_status(run_sql, world, schema):
    """Narrower does not mean blind: the worker can see whether to work."""
    library, _people = world.library_with(("owner", "manager"))
    world.published_generation(library, 1)
    rc, out = run_sql(
        "SELECT index_ready FROM kb.index_status(%s)", role="kb_worker", params=(library,)
    )
    assert rc == 0, out
    assert _bools(out) == [True], out


# ------------------------------------------- 2. no container socket anywhere


def test_the_one_shot_package_cannot_reach_a_container_runtime():
    """No subprocess, no shell, no socket path, in any file of the package.

    The one-shot holds a root key. A process that can start containers is a
    process that can be turned into the host, so the absence is checked on the
    source rather than asserted in a comment — and it is checked for *every*
    file, because a helper module would be exactly where such a call would be
    added.
    """
    forbidden = (
        "subprocess",
        "podman",
        "docker.sock",
        "podman.sock",
        "DOCKER_HOST",
        "/run/podman",
        "/var/run/docker",
        "os.system",
        "pty.spawn",
    )
    offenders: list[str] = []
    for path in sorted(PACKAGE.glob("provisioning*.py")):
        for needle in forbidden:
            # The documentation says these words on purpose; the code must not
            # use them. Strip comments and docstrings before looking.
            code = _code_without_prose(path)
            if needle in code:
                offenders.append(f"{path.name}: {needle}")
    assert offenders == [], offenders


def _code_without_prose(path: pathlib.Path) -> str:
    """Module source with docstrings and comments removed.

    A grep over raw text would flag this very sentence, and a test that fails
    on its own explanation is a test people learn to disable. Parsing instead
    of grepping is the difference between "no subprocess call" and "the word
    subprocess appears in a comment".
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None and node.body:
                first = node.body[0]
                docstrings.add((first.lineno, first.end_lineno))
    kept = []
    for number, line in enumerate(source.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        if any(start <= number <= end for start, end in docstrings):
            continue
        kept.append(line.split("  #")[0] if "  #" in line else line)
    return "\n".join(kept)


def test_the_quadlet_unit_mounts_no_container_socket_and_publishes_nothing():
    """The unit file is an artifact, so the artifact is what gets checked.

    No Podman here means the unit cannot be started and its runtime behaviour
    is `not_run`; the properties below are readable from the file, so they are
    read rather than assumed.
    """
    text = (DEPLOY / "kb-provision-index.container").read_text(encoding="utf-8")
    # Strip the comment block that documents the absences, so this test
    # measures the unit's configuration rather than its prose.
    directives = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    body = "\n".join(directives)
    for needle in ("podman.sock", "docker.sock", "DOCKER_HOST", "PublishPort="):
        assert needle not in body, needle
    assert "NoNewPrivileges=yes" in body
    assert "ReadOnly=yes" in body
    assert "DropCapability=ALL" in body
    assert "Type=oneshot" in body
    # No [Install] WantedBy: provisioning is an explicit action, never a boot
    # step. The section exists here to say so in prose, with no keys in it.
    install = body.split("[Install]", 1)[1] if "[Install]" in body else ""
    assert "WantedBy" not in install


def test_the_deployment_kit_contains_no_secret():
    """Every credential in deploy/maintenance is a placeholder."""
    for path in sorted(DEPLOY.iterdir()):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if path.name.endswith(".env.example"):
            for line in text.splitlines():
                if line.startswith("#") or "=" not in line:
                    continue
                name, _, value = line.partition("=")
                # Paths and endpoints may have real values; credentials may
                # not. The distinction is by variable name, not by shape,
                # because a shape test would one day accept a real key.
                if any(marker in name for marker in ("KEY", "DSN", "URL", "SECRET_VALUE")):
                    assert "__PLACEHOLDER__" in value, f"{path.name}: {name} has a real value"
        assert "sk-" not in text.replace("sk- + ", ""), path.name


# ------------------------------- the request surface carries no authority


def test_the_request_model_carries_no_identity_and_no_account_name():
    """A request is a library id. That is the entire surface.

    ``extra="forbid"`` is the mechanism: a caller who sends an account, a URI,
    a dimension or a ``requested_by`` gets a validation error instead of a
    field that is quietly dropped.
    """
    library = uuid4()
    assert RequestProvisioning(library_id=library).library_id == library
    for payload in (
        {"library_id": library, "requested_by": str(uuid4())},
        {"library_id": library, "account_ref": "kb-lib-whatever"},
        {"library_id": library, "root_uri": "openviking://x/y"},
        {"library_id": library, "dimension": 768},
        {"library_id": library, "root_key": "sk-..."},
    ):
        with pytest.raises(ValidationError):
            RequestProvisioning.model_validate(payload)


def test_the_search_query_model_carries_no_account_and_no_generate_flag():
    """A02 at the model boundary: a caller cannot aim the index.

    The account is a server-side mapping. If a query could name one, the
    access question would move from PostgreSQL to a string, and A02's
    "отказ до чужого index-запроса" would be downgraded to "the index
    returned nothing".
    """
    from kb.retrieval.provisioning_search import SearchQuery

    assert SearchQuery(text="brand voice").limit == 10
    for payload in (
        {"text": "x", "account_ref": "kb-lib-" + "0" * 32},
        {"text": "x", "uri": "openviking://other/published"},
        {"text": "x", "generate": True},
        {"text": "x", "mode": "generative"},
        {"text": "x", "rerank": True},
    ):
        with pytest.raises(ValidationError):
            SearchQuery.model_validate(payload)


def test_the_search_path_cannot_import_the_generative_door():
    """The import graph, not a convention.

    A runtime test that says "the counter is zero" is worth very little if the
    search path could have called the door without moving the counter. So the
    structural claim is checked too: the search module has no import of the
    generative module, of the admin port, and no attribute bound to either.
    """
    source = (PACKAGE / "provisioning_search.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(f"{node.module or ''}.{alias.name}" for alias in node.names)
    forbidden = {
        "kb.retrieval.provisioning_generative",
        "kb.retrieval.provisioning_generative.generate",
    }
    assert not (imported & forbidden), imported & forbidden
    # And no way to reach the door through a function-local import either.
    assert "import_module" not in source and "__import__" not in source


def test_the_search_module_holds_no_admin_port():
    """The privileged boundary is not reachable from a read path."""
    from kb.retrieval import provisioning_search

    for name in dir(provisioning_search):
        assert "Admin" not in name, name
        assert "generate" not in name.lower() or name == "_log", name


# ------------------------------------------- the one-shot refuses to pretend


def test_the_unconfigured_admin_refuses_every_operation():
    """The absence of OpenViking is a runtime answer with a reason.

    If this class quietly returned something plausible, every test in the card
    would still be green and the card would be a lie.
    """
    from kb.retrieval.provisioning import AccountSpec, build_account_spec

    spec: AccountSpec = build_account_spec(uuid4())
    admin = UnconfiguredIndexAdmin()
    for call in (
        lambda: admin.create_account(spec),
        lambda: admin.ensure_service_identity(spec, spec.read_identity),
        lambda: admin.apply_acl(spec),
        lambda: admin.issue_service_key(spec, spec.read_identity),
        lambda: admin.describe_account(spec),
    ):
        with pytest.raises(Exception) as caught:
            call()
        assert "no OpenViking admin endpoint" in str(caught.value)
    with pytest.raises(Exception, match="no OpenViking admin endpoint"):
        UnconfiguredSecretSink().write("kb-svc-read-x", "secret")


# --------------------------------------------------------------- the CLI


def _run_cli(args: list[str], dsn: str, **env_extra: str) -> subprocess.CompletedProcess[str]:
    import os

    env = dict(os.environ)
    env["KB_DB_DSN"] = dsn
    env["PYTHONPATH"] = str(PACKAGE.parents[1])
    for key in ("KB_OPENVIKING_ADMIN_URL", "KB_OPENVIKING_ROOT_KEY", "KB_SECRET_STORE_DIR"):
        env.pop(key, None)
    env.update(env_extra)
    return subprocess.run(  # noqa: S603 - fixed argv, absolute interpreter
        [sys.executable, "-m", "kb.retrieval.provisioning_cli", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def test_the_cli_refuses_to_run_as_the_migration_owner(c15_dsn, schema):
    """The one-shot must not be the DDL owner.

    Connecting as the superuser is the easiest way to make provisioning tests
    pass, because a superuser bypasses RLS. The program refuses that outright
    rather than letting a convenient connection quietly widen the blast
    radius of a process that holds a root key.
    """
    result = _run_cli(["list-requests"], c15_dsn)
    assert result.returncode == 4, result.stderr
    assert "refusing to run the one-shot" in result.stderr, result.stderr


def test_the_cli_lists_open_requests_against_a_real_database(provisioner_dsn, world):
    """The part of the one-shot that genuinely runs here, and it exits 0."""
    library, _people = world.library_with(("owner", "manager"))
    from kb.retrieval.provisioning import request_provisioning

    with psycopg.connect(provisioner_dsn, autocommit=True) as conn:
        request = request_provisioning(conn, world.manager, library)
    result = _run_cli(["list-requests"], provisioner_dsn)
    assert result.returncode == 0, result.stderr
    assert str(request.id) in result.stdout
    assert str(library) in result.stdout


def test_the_cli_exits_nonzero_when_openviking_is_absent(provisioner_dsn, world):
    """The receipt for a provisioning run that did not happen.

    Exit 3, a message that names the missing dependency, and no account row
    afterwards. A green line here would be the exact failure this card is
    graded on.
    """
    library, _people = world.library_with(("owner", "manager"))
    from kb.retrieval.provisioning import request_provisioning

    with psycopg.connect(provisioner_dsn, autocommit=True) as conn:
        request = request_provisioning(conn, world.manager, library)
    result = _run_cli(["run", str(request.id)], provisioner_dsn)
    assert result.returncode == 3, (result.returncode, result.stdout, result.stderr)
    assert "CANNOT PROVISION" in result.stderr, result.stderr
    assert "success" not in result.stdout.lower()
    assert world.account_row(library) is None


def test_the_cli_exits_nonzero_even_with_the_endpoint_configured(provisioner_dsn, world):
    """A flag is not an implementation.

    Setting ``KB_OPENVIKING_ADMIN_URL`` must not turn a missing client into a
    green run. The tree ships the boundary, not a client, and says so.
    """
    library, _people = world.library_with(("owner", "manager"))
    from kb.retrieval.provisioning import request_provisioning

    with psycopg.connect(provisioner_dsn, autocommit=True) as conn:
        request = request_provisioning(conn, world.manager, library)
    result = _run_cli(
        ["run", str(request.id)],
        provisioner_dsn,
        KB_OPENVIKING_ADMIN_URL="http://127.0.0.1:19300",
        KB_SECRET_STORE_DIR="/run/kb-secrets",  # noqa: S106 - a path, not a credential
    )
    assert result.returncode == 3, (result.returncode, result.stdout, result.stderr)
    assert "ships no OpenViking admin client" in result.stderr, result.stderr
