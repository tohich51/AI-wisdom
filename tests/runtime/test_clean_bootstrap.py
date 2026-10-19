"""S04 bootstrap regression: actual empty counts, complete schema and retained data.

Real PostgreSQL and product upload/parse/provenance; no browser or native OIDC
claim. The separate transient rendered acceptance supplies the UI leg.
"""

import importlib.util
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pgserver
import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from test_runtime import runtime_db as runtime_db

from kb.access.policy import Principal
from kb.adapters.originals import LocalBlobStore
from kb.catalog import content, libraries, upload, writes
from kb.jobs.ingestion import run_job, submit_processing

pytestmark = [pytest.mark.integration, pytest.mark.acceptance]


def test_bootstrap_reports_actual_counts_and_preserves_uploaded_identity_and_rights(
    runtime_db, tmp_path, monkeypatch, capsys
):
    path = Path(__file__).resolve().parents[2] / "scripts/bootstrap.py"
    spec = importlib.util.spec_from_file_location("clean_bootstrap", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    server = SimpleNamespace(get_uri=lambda: runtime_db)
    assert module.apply_schema(server) == []
    owner = uuid4()
    initial = module.bootstrap(
        server, organisation="Synthetic clean installation", owner_principal=str(owner)
    )
    assert module.content_counts(server) == dict.fromkeys(
        ["libraries", "sources", "knowledge", "rules", "fragments"], 0
    )
    assert all(initial[k] == 0 for k in ["libraries", "sources", "knowledge", "rules", "fragments"])
    account = UUID(initial["organisation_id"])
    with psycopg.connect(runtime_db) as conn:
        conn.execute(
            "INSERT INTO kb.membership(organisation_id,principal_id,issuer,subject,status)"
            " VALUES(%s,%s,'https://identity.test',%s,'active')",
            (account, owner, str(owner)),
        )
        conn.execute(
            "INSERT INTO kb.organisation_admin(organisation_id,principal_id) VALUES(%s,%s)",
            (account, owner),
        )
    principal = Principal(owner, account)
    dsn = make_conninfo(runtime_db, user="kb_app")
    worker = make_conninfo(runtime_db, user="kb_worker")
    store = LocalBlobStore(tmp_path / "originals")
    original = (
        b"Synthetic original. State one measurable objective.\n"
        b"Cite the source before applying guidance."
    )
    with psycopg.connect(dsn) as conn:
        library = libraries.create_library(
            conn,
            principal,
            libraries.CreateLibrary(
                organisation_id=account, name="First owner library", kind="reference"
            ),
        )
        source = upload.submit_file(
            conn,
            principal,
            store,
            upload.SubmitFile(
                library_id=library.id, title="First original", idempotency_key=str(uuid4())
            ),
            io.BytesIO(original),
            declared_media_type="text/plain",
        ).source
        job = submit_processing(conn, principal, source.id, str(uuid4()), extract=False)
    run_job(worker, job["job_id"], principal, store.root)
    with psycopg.connect(dsn) as conn:
        read = content.read_source(conn, principal, source.id)
        fragment = read["fragments"][0]["id"]
        rule = writes.create_rule(
            conn,
            principal,
            library.id,
            writes.RuleInput(
                title="First guidance",
                when_to_apply="Before work",
                expected_effect="Traceable work",
                actions=["Cite the source"],
                fragment_ids=[fragment],
            ),
        )
        writes.publish(conn, principal, rule["id"])

    def census():
        with psycopg.connect(runtime_db) as conn:
            return {
                table: conn.execute(
                    sql.SQL("SELECT * FROM kb.{} ORDER BY 1").format(sql.Identifier(table))
                ).fetchall()
                for table in [
                    "organisation",
                    "membership",
                    "organisation_admin",
                    "library",
                    "library_grant",
                    "source",
                    "fragment",
                    "rule",
                    "rule_provenance",
                    "object_manifest",
                ]
            }

    before = census()
    assert module.apply_schema(server) == []
    repeated = module.bootstrap(
        server, organisation="Synthetic clean installation", owner_principal=str(owner)
    )
    assert repeated["organisation_id"] == initial["organisation_id"]
    assert repeated["libraries"] == repeated["sources"] == repeated["rules"] == 1
    assert repeated["knowledge"] == 0 and repeated["fragments"] > 0
    monkeypatch.setattr(pgserver, "get_server", lambda **kwargs: server)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bootstrap.py",
            "--organisation",
            "Synthetic clean installation",
            "--owner-principal",
            str(owner),
            "--json",
        ],
    )
    capsys.readouterr()
    assert module.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["empty_after_bootstrap"] is False
    assert report["content_counts"] == module.content_counts(server)
    assert report["migrations_applied"] == []
    assert census() == before
    assert (store.root / source.object_key).read_bytes() == original
    with psycopg.connect(dsn) as conn:
        assert (
            content.get_rule(conn, principal, rule["id"])["provenance"][0]["fragment_id"]
            == fragment
        )
