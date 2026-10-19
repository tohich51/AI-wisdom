"""Explicit live gate; omitted from default collection, never silently skipped.

Run this exact file on the prepared xorek host. PostgreSQL fixtures are disposable;
the OpenViking endpoint is the real service and uses only two synthetic accounts.
"""

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
from openviking_sdk import SyncHTTPClient
from psycopg.conninfo import make_conninfo
from test_runtime import runtime_db as runtime_db
from test_runtime import world as world

from kb.access.policy import Principal
from kb.catalog import libraries
from kb.retrieval import provisioning as p
from kb.retrieval import provisioning_cli as cli
from kb.retrieval.embedding_profile import SELECTED_PROFILE
from kb.retrieval.openviking_admin import OpenVikingAdmin

pytestmark = pytest.mark.integration


def test_live_isolated_accounts_partial_resume_and_permissions(world, tmp_path, monkeypatch):
    url = os.environ.get("KB_C15_LIVE_URL")
    root_file = os.environ.get("KB_C15_LIVE_ROOT_KEY_FILE")
    destination = Path(os.environ["KB_C15_ARTIFACT_DIR"])
    if not url or not root_file:
        pytest.fail("required_live_openviking_configuration_missing", pytrace=False)
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    account_b, person_b = uuid4(), uuid4()
    with psycopg.connect(world["admin"]) as admin:
        admin.execute(
            "INSERT INTO kb.organisation(id,name) VALUES(%s,'C15 live synthetic')", (account_b,)
        )
        admin.execute(
            "INSERT INTO kb.membership(organisation_id,principal_id,issuer,subject,status) "
            "VALUES(%s,%s,'https://synthetic.invalid',%s,'active')",
            (account_b, person_b, str(person_b)),
        )
        admin.execute(
            "INSERT INTO kb.organisation_admin(organisation_id,principal_id) VALUES(%s,%s)",
            (account_b, person_b),
        )
    second = Principal(person_b, account_b)
    with psycopg.connect(world["dsn"]) as conn:
        lib_b = libraries.create_library(
            conn,
            second,
            libraries.CreateLibrary(organisation_id=account_b, name="C15 live B", kind="reference"),
        ).id
        request_a = p.request_provisioning(conn, world["principal"], world["lib"])
        request_b = p.request_provisioning(conn, second, lib_b)
    keys, bootstrap = tmp_path / "keys", tmp_path / "bootstrap"
    keys.mkdir(mode=0o700)
    bootstrap.mkdir(mode=0o700)
    specs = [
        p.build_account_spec(
            i, dimension=SELECTED_PROFILE.dimension, embedding_profile=SELECTED_PROFILE.name
        )
        for i in [world["lib"], lib_b]
    ]
    intent = {
        "source_sha": os.environ["KB_C15_EXPECTED_SHA"],
        "account_refs": [s.account_ref for s in specs],
        "denied_account_ref": "kb-c15-denied-" + uuid4().hex,
        "keys_root": str(keys),
        "bootstrap_root": str(bootstrap),
        "scope": "only_new_synthetic_accounts_no_existing_account_or_book_mutation",
    }
    (destination / "intent.json").write_text(json.dumps(intent, indent=2) + "\n")
    for name, value in {
        "KB_DB_DSN": make_conninfo(world["admin"], user="kb_provisioner"),
        "KB_OPENVIKING_URL": url,
        "KB_OPENVIKING_ROOT_KEY_FILE": root_file,
        "KB_INDEX_KEYS_ROOT": str(keys),
        "KB_BOOTSTRAP_KEYS_ROOT": str(bootstrap),
    }.items():
        monkeypatch.setenv(name, value)
    created, issued = [], []

    class CountingAdmin(OpenVikingAdmin):
        def create_account(self, spec):
            result = super().create_account(spec)
            created.append(spec.account_ref)
            return result

        def issue_service_key(self, spec, identity):
            result = super().issue_service_key(spec, identity)
            issued.append(identity)
            return result

    monkeypatch.setattr(cli, "OpenVikingAdmin", CountingAdmin)
    store = p._store_credential_ref
    interrupted = [False]

    def fail_after_first_file(conn, principal, library_id, identity, ref):
        if library_id == world["lib"] and not interrupted[0]:
            interrupted[0] = True
            raise RuntimeError("controlled_db_ref_interruption")
        return store(conn, principal, library_id, identity, ref)

    monkeypatch.setattr(p, "_store_credential_ref", fail_after_first_file)
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        first_status = cli.main(["run"])
        assert first_status != 0
        first_path = keys / "read" / specs[0].read_identity / "key"
        first_hash = hashlib.sha256(first_path.read_bytes()).hexdigest()
        assert cli.main(["run"]) == 0
        assert cli.main(["run", str(request_a.id)]) == 0
        assert cli.main(["run", str(request_b.id)]) == 0
    assert len(created) == 2 and len(issued) == 4
    assert hashlib.sha256(first_path.read_bytes()).hexdigest() == first_hash
    with psycopg.connect(world["admin"]) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM kb.provisioning_request "
                "WHERE id=ANY(%s) AND state='succeeded'",
                ([request_a.id, request_b.id],),
            ).fetchone()[0]
            == 2
        )
    checks = []
    for position, spec in enumerate(specs):
        index_path = keys / "index" / spec.index_identity / "key"
        read_path = keys / "read" / spec.read_identity / "key"
        index_key, read_key = index_path.read_text().strip(), read_path.read_text().strip()
        if index_key in captured.getvalue() or read_key in captured.getvalue():
            pytest.fail("credential_leak_detected", pytrace=False)
        marker = "c15-scope-" + str(position)
        writer = SyncHTTPClient(url=url, api_key=index_key, timeout=30)
        reader = SyncHTTPClient(url=url, api_key=read_key, timeout=30)
        try:
            writer.initialize()
            reader.initialize()
            writer.mkdir("viking://resources/index/" + marker)
            writer.wait_processed(timeout=60)
            assert marker in json.dumps(reader.ls("viking://resources/index"))
            denied = False
            try:
                reader.mkdir("viking://resources/index/c15-denied-write")
            except Exception as exc:
                denied = type(exc).__name__ == "PermissionDeniedError"
            assert denied
            response = httpx.post(
                url + "/api/v1/admin/accounts",
                headers={"X-API-Key": read_key},
                json={"account_id": intent["denied_account_ref"], "admin_user_id": "denied"},
                timeout=30,
            )
            assert response.status_code == 403
            checks.append(
                {
                    "account_ref": spec.account_ref,
                    "read_own": True,
                    "write_with_index": True,
                    "write_with_read_denied": True,
                    "admin_create_with_read_denied": True,
                }
            )
        finally:
            writer.close()
            reader.close()
        admin_client = SyncHTTPClient(
            url=url,
            api_key=(bootstrap / (spec.account_ref + ".key")).read_text().strip(),
            timeout=30,
        )
        try:
            admin_client.initialize()
            acl = admin_client.acl_get("viking://resources/index")
            assert acl["acl_mode"] == "restricted"
            assert {(x["principal"], x["level"]) for x in acl["effective_entries"]} == {
                ("user:" + spec.read_identity, "read"),
                ("user:" + spec.index_identity, "write"),
            }
        finally:
            admin_client.close()
    # An ordinary user key must not select another account through trusted-mode headers.
    read_key = (keys / "read" / specs[0].read_identity / "key").read_text().strip()
    response = httpx.get(
        url + "/api/v1/fs/ls",
        params={"uri": "viking://resources/index"},
        headers={
            "X-API-Key": read_key,
            "X-OpenViking-Account": specs[1].account_ref,
            "X-OpenViking-User": specs[1].read_identity,
        },
        timeout=30,
    )
    if response.status_code == 200:
        assert "c15-scope-0" in response.text and "c15-scope-1" not in response.text
    else:
        assert response.status_code in (400, 401, 403)
    report = {
        "source_sha": intent["source_sha"],
        "live_openviking": True,
        "real_postgresql": True,
        "synthetic_accounts": 2,
        "actual_admin_adapter": True,
        "account_creations": len(created),
        "service_key_issuances": len(issued),
        "partial_resume_key_preserved": True,
        "completed_replay_no_rotation": True,
        "different_manager_accounts": True,
        "header_account_escape_denied": True,
        "restricted_acl_exact": True,
        "checks": checks,
        "secrets_printed": False,
        "real_books_used": False,
        "cleanup": "retain_exact_intent_and_keys_until_both_reviews_consume_evidence",
    }
    (destination / "result.json").write_text(json.dumps(report, indent=2) + "\n")
