import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from test_runtime import runtime_db as runtime_db
from test_runtime import world as world

from kb.adapters.secrets import FileSecretSink
from kb.jobs import indexer
from kb.retrieval import provisioning as p
from kb.retrieval import provisioning_cli as cli
from kb.retrieval.embedding_profile import SELECTED_PROFILE

pytestmark = pytest.mark.integration


def operator(world):
    return make_conninfo(world["admin"], user="kb_provisioner")


def request(world):
    with psycopg.connect(world["dsn"]) as conn:
        return p.request_provisioning(conn, world["principal"], world["lib"])


class AdminDouble:
    # Only the PostgreSQL/file tests use this double; it proves no live OV effect.
    def __init__(self):
        self.issued = []
        self.specs = []
        self.verifications = 0
        self.closed = False

    def create_account(self, spec):
        self.specs.append(spec)
        return p.RemoteAccount(spec.account_ref, True)

    def ensure_service_identity(self, spec, identity):
        pass

    def apply_acl(self, spec):
        pass

    def issue_service_key(self, spec, identity):
        self.issued.append(identity)
        return p.IssuedKey(identity, "SYNTHETIC-C15-SECRET-" + str(len(self.issued)))

    def describe_account(self, spec):
        self.verifications += 1
        return {"acl_entries": 2}

    def close(self):
        self.closed = True


def test_context_is_exact_and_active_membership_is_rechecked(world):
    submitted = request(world)
    with psycopg.connect(operator(world), autocommit=True) as conn:
        row = conn.execute("SELECT * FROM kb.provisioning_context(%s)", (submitted.id,)).fetchone()
        assert row == (
            submitted.id,
            world["lib"],
            world["principal"].principal_id,
            world["principal"].account_id,
        )
        assert cli._principal_for(conn, submitted.id) == world["principal"]
        with psycopg.connect(world["admin"]) as admin:
            admin.execute(
                "DELETE FROM kb.membership WHERE organisation_id=%s AND principal_id=%s",
                (world["principal"].account_id, world["principal"].principal_id),
            )
        assert (
            conn.execute("SELECT * FROM kb.provisioning_context(%s)", (submitted.id,)).fetchall()
            == []
        )
        with pytest.raises(p.ProvisioningRefused):
            cli._principal_for(conn, submitted.id)


@pytest.mark.parametrize("role", ["kb_app", "kb_worker"])
def test_unprivileged_roles_cannot_obtain_operator_context(world, role, monkeypatch):
    dsn = make_conninfo(world["admin"], user=role)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with psycopg.connect(dsn) as conn:
            conn.execute("SELECT * FROM kb.provisioning_context()")
    monkeypatch.setenv("KB_DB_DSN", dsn)
    assert cli.main(["list-requests"]) == cli.EXIT_REFUSED


def test_file_key_recovery_preserves_keys_and_repairs_completed_missing_file(
    world, tmp_path, monkeypatch, capsys
):
    submitted = request(world)
    keys = tmp_path / "keys"
    keys.mkdir(mode=0o700)
    sink, admin = FileSecretSink(keys), AdminDouble()
    original_store = p._store_credential_ref
    fail_once = [True]

    def interrupted_store(*args):
        if fail_once.pop() if fail_once else False:
            raise RuntimeError("synthetic_ref_failure")
        return original_store(*args)

    monkeypatch.setattr(p, "_store_credential_ref", interrupted_store)
    spec = p.build_account_spec(
        world["lib"],
        dimension=SELECTED_PROFILE.dimension,
        embedding_profile=SELECTED_PROFILE.name,
    )
    with psycopg.connect(operator(world), autocommit=True) as conn:

        def run():
            return p.run_provisioning(
                conn,
                world["principal"],
                submitted.id,
                admin,
                sink,
                dimension=SELECTED_PROFILE.dimension,
                embedding_profile=SELECTED_PROFILE.name,
            )

        with pytest.raises(p.ProvisioningError):
            run()
        assert admin.issued == [spec.read_identity]
        read_file = keys / "read" / spec.read_identity / "key"
        read_before = read_file.read_bytes()
        result = run()
        assert result.resumed
        assert admin.issued == [spec.read_identity, spec.index_identity]
        assert read_file.read_bytes() == read_before
        assert len(p.credential_refs(conn, world["principal"], world["lib"])) == 2
        assert run().steps == []
        assert len(admin.issued) == 2
        index_file = keys / "index" / spec.index_identity / "key"
        index_file.unlink()
        run()
        assert admin.issued == [spec.read_identity, spec.index_identity, spec.index_identity]
        assert read_file.read_bytes() == read_before
        with psycopg.connect(world["admin"]) as raw:
            assert raw.execute(
                "SELECT state FROM kb.provisioning_request WHERE id=%s", (submitted.id,)
            ).fetchone() == ("succeeded",)
        index_file.chmod(0o644)
        with pytest.raises(p.ProvisioningRefused):
            run()
        assert len(admin.issued) == 3
    captured = capsys.readouterr()
    assert "SYNTHETIC-C15-SECRET-" not in captured.out + captured.err


def test_no_id_cli_provisions_selected_profile_and_show_uses_identity(
    world, tmp_path, monkeypatch, capsys
):
    request(world)
    keys, bootstrap = tmp_path / "keys", tmp_path / "bootstrap"
    keys.mkdir(mode=0o700)
    bootstrap.mkdir(mode=0o700)
    root_key = tmp_path / "root.key"
    root_key.write_text("SYNTHETIC-ROOT")
    root_key.chmod(0o600)
    for name, value in {
        "KB_DB_DSN": operator(world),
        "KB_OPENVIKING_URL": "http://synthetic.invalid",
        "KB_OPENVIKING_ROOT_KEY_FILE": str(root_key),
        "KB_INDEX_KEYS_ROOT": str(keys),
        "KB_BOOTSTRAP_KEYS_ROOT": str(bootstrap),
    }.items():
        monkeypatch.setenv(name, value)
    admin = AdminDouble()
    monkeypatch.setattr(cli, "OpenVikingAdmin", lambda *_args: admin)
    assert cli.main(["run"]) == 0
    assert admin.closed
    assert len(admin.issued) == 2
    assert len(admin.specs) == 1
    assert admin.specs[0].dimension == SELECTED_PROFILE.dimension
    assert admin.specs[0].embedding_profile == SELECTED_PROFILE.name
    assert cli.main(["show", str(world["lib"])]) == 0
    output = capsys.readouterr().out
    assert "kb-lib-" + world["lib"].hex in output
    assert "SYNTHETIC" not in output


def test_indexer_only_requests_missing_account_without_openviking_access(
    world, tmp_path, monkeypatch
):
    def forbidden_writer(*_args):
        raise AssertionError("No OpenViking call before provisioning")

    monkeypatch.setattr(indexer, "OpenVikingWriter", forbidden_writer)
    for _ in range(2):
        result = indexer.reindex(
            operator(world),
            world["principal"],
            world["lib"],
            url="http://synthetic.invalid",
            keys_root=tmp_path,
        )
        assert result["state"] == "awaiting_provisioning"
    with psycopg.connect(world["admin"]) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM kb.provisioning_request WHERE library_id=%s",
                (world["lib"],),
            ).fetchone()[0]
            == 1
        )
