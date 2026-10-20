"""
A10 — withdrawing the source closes the source, rule and export links; the
revoked grant
closes what is left.

One scenario, one harness. ``other`` is an ordinary member of the organisation
holding the
*reader* role and no admin role. The owner derives one published
rule in the original private
reference library (``world["lib"]``) from a
fragment of a source parsed into that same
library, and grants the reader on
this library plus one unrelated private library that stays
readable throughout.

**Same library, deliberately.** Rule provenance is library-local: the 0006
``WITH CHECK``
requires ``source.library_id = rule.library_id``, so a rule may
cite only fragments of sources
in its own library. An earlier draft of this
file put the derived rule in a second library and
the *database* rejected the
fixture, which was a wrong scenario, not a product failure.
Nothing here asks
for, or claims, cross-library provenance, and no DDL is changed to make it
possible. The unrelated library is a **positive control** that stays 200 all the
way through,
and that is what isolates "the dependency is gone" from "the
library permission is gone".

Phase 1 — the source, the rule and the exact export body all resolve through
every supported
prefix, and the manifest carries the real source id and SKILL.md carries the
real cited
fragment id.
Phase 2 — the owner withdraws the source (``publication='withdrawn'``) with no
permission change whatsoever: the rule/export requests, replayed twice per prefix, are
refused. The directly granted original remains readable; the *same* library still reads 200
and its rule listing simply
omits the rule whose provenance is no longer reachable.
Phase 3 —
the publication label captured from the row before the withdrawal is
restored exactly
(whatever enum label the parsed fixture actually carries), and
the same links open again.
Phase 4 — only the reader's grant on the origin library is revoked. The same
paths and the
same export body stay refused, the unrelated library still reads
200, and the owner still
reads the source and the rule through the services, so
the refusals above were access control
and never missing data.

**Honest limit.** ``POST /libraries/{id}/export`` returns the artifact *in the
HTTP response
body*: there is no persisted server-side copy, no stored download
URL and no expiring link for
a revocation to invalidate. What is proven here is
exactly what a grant or publication change
can control — that every *new* read
of the rule and export is refused after withdrawal, and
the original also refuses after its library grant is revoked. Artifact bytes the
reader
already received are the reader's own copy, off the server; no access
control, here or
anywhere, can recall them.

No product code, no DDL, no live OIDC: the middleware below is a synthetic
stand-in for C07's
Keycloak middleware that sets
``request.state.principal`` to ``world["other"]`` and nothing
else, and
``db_connection`` hands out a real ``kb_app`` connection, so RLS decides every
answer.
"""

import base64
import hashlib
import io
import json
import zipfile
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from test_research_grounding import parsed
from test_runtime import runtime_db as runtime_db
from test_runtime import world as world

from kb.access.policy import transaction_identity
from kb.catalog import content, libraries, policies, writes
from kb.contracts.operations import API_PREFIXES
from kb.http.router import mount_api

pytestmark = [pytest.mark.integration, pytest.mark.acceptance]

# The A10 packet line, used verbatim as the parsed-source marker and as the
# canary that must never appear in a refused response body.
A10 = "Закрытый источник участвует в производном правиле; права на источник сняты"
RULE_TITLE = "A10 derived rule: provenance of the withdrawn source"
CONTROL_PATH = "/libraries/{}/rules"


@pytest.fixture
def reader(world):
    """A real in-process app whose only identity is the fixture reader.

    ``mount_api`` serves the real content and library routers under every
    supported prefix, and ``db_connection`` hands out a real ``kb_app``
    connection, so RLS decides every answer. The middleware is the whole of the
    authentication story here: a synthetic stand-in for C07's Keycloak
    middleware, not a token check, and it can only ever say ``other``.
    """
    app = FastAPI()

    @app.middleware("http")
    async def synthetic_identity(request: Request, call_next):
        request.state.principal = world["other"]
        return await call_next(request)

    app.state.db_connection = lambda: psycopg.connect(world["dsn"])
    mount_api(app)
    with TestClient(app) as client:
        yield client


def test_withdrawn_source_then_revoked_grant_close_every_link(world, reader):
    with psycopg.connect(world["dsn"]) as conn:
        source_id = parsed(world, A10)
        with transaction_identity(conn, world["principal"]):
            fragment_id, publication = conn.execute(
                "SELECT f.id, s.publication FROM kb.source s"
                " JOIN kb.fragment f ON f.source_id = s.id WHERE s.id = %s",
                (source_id,),
            ).fetchone()

        # An unrelated private library. It holds nothing and is read by nobody
        # else; it is the positive control that must survive every step below.
        control = libraries.create_library(
            conn,
            world["principal"],
            libraries.CreateLibrary(
                organisation_id=world["principal"].account_id,
                name="Unrelated control " + str(uuid4()),
                kind="reference",
            ),
        ).id
        for library in (world["lib"], control):
            libraries.set_grant(
                conn,
                world["principal"],
                library,
                libraries.SetGrant(principal_id=world["other"].principal_id, role="reader"),
            )
            policies.put_policy(
                conn,
                world["principal"],
                library,
                policies.LibraryPolicyInput(
                    external_processing_allowed=False,
                    cloud_mcp_allowed=False,
                    export_allowed=True,
                    generation_allowed=False,
                ),
            )

        # The derived rule is created in the source's own library, because 0006
        # requires source.library_id = rule.library_id for every citation.
        derived = writes.create_rule(
            conn,
            world["principal"],
            world["lib"],
            writes.RuleInput(
                title=RULE_TITLE,
                when_to_apply="When the provenance of the derived guidance is checked",
                expected_effect="The originating source is still traceable",
                actions=["Trace the cited fragment"],
                fragment_ids=[fragment_id],
            ),
        )
        writes.publish(conn, world["principal"], derived["id"])

    # Service dicts carry UUIDs, not strings: normalise once, here.
    source_id, fragment_id, rule_id = str(source_id), str(fragment_id), str(derived["id"])
    source_path = f"/sources/{source_id}"
    rule_path = f"/rules/{rule_id}"
    origin_path = f"/libraries/{world['lib']}"
    listing_path = f"/libraries/{world['lib']}/rules"
    export_path = f"/libraries/{world['lib']}/export"
    control_path = CONTROL_PATH.format(control)
    export_body = {"rule_ids": [rule_id]}

    def links_open():
        """Source, rule and the exact export body, through every real prefix."""
        for prefix in API_PREFIXES:
            opened = reader.get(prefix + source_path)
            assert opened.status_code == 200, opened.text
            assert A10 in opened.text

            opened = reader.get(prefix + rule_path)
            assert opened.status_code == 200, opened.text
            rule_body = opened.json()
            assert rule_body["title"] == RULE_TITLE

            artifact = reader.post(prefix + export_path, json=export_body)
            assert artifact.status_code == 200, artifact.text
            body = artifact.json()
            manifest = body["manifest"]
            assert manifest["content_sha256"] == body["sha256"]
            serialised = json.dumps(manifest, default=str)
            assert source_id in serialised, (
                "the manifest must carry the real source, not a stand-in"
            )
            assert fragment_id in body["content"], (
                "the SKILL provenance must carry the real cited fragment"
            )

            archive = body["archive"]
            blob = base64.b64decode(archive["content_base64"])
            assert archive["bytes"] == len(blob)
            assert archive["sha256"] == hashlib.sha256(blob).hexdigest()
            assert archive["filename"] == "ai-wisdom-" + body["manifest_sha256"][:16] + ".zip"
            with zipfile.ZipFile(io.BytesIO(blob)) as portable:
                assert portable.namelist() == ["SKILL.md", "manifest.json"]
                assert all(
                    info.date_time == (1980, 1, 1, 0, 0, 0) for info in portable.infolist()
                ), "the archive must carry a pinned timestamp for reproducible hashing"
                assert portable.read("SKILL.md").decode("utf-8") == body["content"]
                packed_manifest = portable.read("manifest.json").decode("utf-8")
            assert packed_manifest == json.dumps(
                manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            packed = json.loads(packed_manifest)
            assert packed == manifest
            assert (
                hashlib.sha256(packed_manifest.encode("utf-8")).hexdigest()
                == body["manifest_sha256"]
            )
            packed_rules = {entry["id"]: entry for entry in packed["rules"]}
            assert packed_rules[str(rule_body["id"])]["version_no"] == rule_body["version_no"], (
                "the archived manifest must carry the exact rule version"
            )
            assert {source["id"] for source in packed["sources"]} >= {source_id}, (
                "the archived manifest must carry the real source id"
            )

    def links_closed(*, source_access_revoked):
        """The very same requests, through every prefix, twice each."""
        for _attempt in range(2):
            for prefix in API_PREFIXES:
                for path in (source_path, rule_path) if source_access_revoked else (rule_path,):
                    closed = reader.get(prefix + path)
                    assert closed.status_code in (403, 404), closed.text
                    assert A10 not in closed.text and RULE_TITLE not in closed.text

                exported = reader.post(prefix + export_path, json=export_body)
                assert exported.status_code in (403, 404), exported.text
                # No partial export: neither the artifact nor its manifest leaks.
                assert A10 not in exported.text and RULE_TITLE not in exported.text
                assert source_id not in exported.text and fragment_id not in exported.text
                assert "manifest" not in exported.text and "content_sha256" not in exported.text

    def control_readable():
        for prefix in API_PREFIXES:
            assert reader.get(prefix + control_path).status_code == 200

    # ---- phase 1: the whole chain is reachable, through both actual prefixes.
    links_open()
    control_readable()
    for prefix in API_PREFIXES:
        assert reader.get(prefix + origin_path).status_code == 200
        assert RULE_TITLE in reader.get(prefix + listing_path).text

    # ---- phase 2: the owner withdraws the source and commits. No permission is
    # touched, so the refusals below can only be about the lost dependency.
    with psycopg.connect(world["dsn"]) as conn:
        with transaction_identity(conn, world["principal"]):
            conn.execute("UPDATE kb.source SET publication='withdrawn' WHERE id=%s", (source_id,))

    links_closed(source_access_revoked=False)
    control_readable()
    for prefix in API_PREFIXES:
        assert reader.get(prefix + source_path).status_code == 200
        assert reader.get(prefix + origin_path).status_code == 200, (
            "the origin library itself must still be readable: this phase removes"
            " the dependency, not the grant"
        )
        listing = reader.get(prefix + listing_path)
        assert listing.status_code == 200
        assert rule_id not in {str(row["id"]) for row in listing.json()}
        assert RULE_TITLE not in listing.text

    # ---- phase 3: the publication label read from the row before the
    # withdrawal, restored exactly — no assumption about the parsed fixture's
    # own enum value.
    with psycopg.connect(world["dsn"]) as conn:
        with transaction_identity(conn, world["principal"]):
            conn.execute(
                "UPDATE kb.source SET publication=%s WHERE id=%s", (publication, source_id)
            )

    links_open()
    for prefix in API_PREFIXES:
        assert RULE_TITLE in reader.get(prefix + listing_path).text

    # ---- phase 4: only the reader's grant on the origin library is revoked.
    with psycopg.connect(world["dsn"]) as conn:
        libraries.revoke_grant(conn, world["principal"], world["lib"], world["other"].principal_id)

    links_closed(source_access_revoked=True)
    control_readable()
    for prefix in API_PREFIXES:
        assert reader.get(prefix + origin_path).status_code in (403, 404)

    # The data is untouched and the owner still reads it through the services:
    # the reader's refusals were access control, never absent rows.
    with psycopg.connect(world["dsn"]) as conn:
        kept_source = content.read_source(conn, world["principal"], UUID(source_id))
        assert any(
            row["id"] == UUID(fragment_id) and row["text"] == A10
            for row in kept_source["fragments"]
        )
        assert content.get_rule(conn, world["principal"], UUID(rule_id))["title"] == RULE_TITLE
