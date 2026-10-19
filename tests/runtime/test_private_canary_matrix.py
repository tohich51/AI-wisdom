"""A01 - private library isolation between two non-admin principals.

Alice and Bob each own one private library carrying unique canaries; search,
the source card, metadata, library counts and the export artifact must never
disclose the other principal's canary.  The test drives the real kb_app
services against the shared PostgreSQL instance, so SQL row-level security is
the component under test; it does not exercise a rendered UI nor a live OIDC
multi-user login.
"""

import json
import uuid

import psycopg
import pytest
from test_final_project_manifest import _published
from test_runtime import runtime_db as runtime_db
from test_runtime import world as world

from kb.access.policy import AccessDenied, Principal, transaction_identity
from kb.catalog import content, libraries, policies, writes

pytestmark = [pytest.mark.integration, pytest.mark.acceptance]

QUERY_WORD = "privacyneedle"
SECRET_FIELDS = (
    "lib",
    "library_name",
    "source_id",
    "fragment_id",
    "author",
    "rule_id",
    "knowledge_id",
    "title",
)


def _canary(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _secrets(data):
    return [str(data[field]) for field in SECRET_FIELDS]


def _bob(world):
    """Bob: a fresh non-admin principal with an active membership only."""
    template = world["other"]
    actor = Principal(uuid.uuid4(), template.account_id)
    with psycopg.connect(world["admin"], autocommit=True) as admin:
        admin.execute(
            "INSERT INTO kb.membership (organisation_id, principal_id, issuer,"
            " subject, status) VALUES (%s, %s, 'https://identity.test', %s, 'active')",
            (actor.account_id, actor.principal_id, actor.principal_id),
        )
        for principal_id in (template.principal_id, actor.principal_id):
            assert (
                admin.execute(
                    "SELECT count(*) FROM kb.organisation_admin WHERE principal_id = %s",
                    (principal_id,),
                ).fetchone()[0]
                == 0
            )
    return actor


def _publish_knowledge(conn, actor, library_id, statement, source_id, fragment_id):
    with transaction_identity(conn, actor):
        knowledge_id = conn.execute(
            "INSERT INTO kb.knowledge (library_id, statement, kind)"
            " VALUES (%s, %s, 'assertion') RETURNING id",
            (library_id, statement),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO kb.knowledge_provenance (knowledge_id, source_id, fragment_id)"
            " VALUES (%s, %s, %s)",
            (knowledge_id, source_id, fragment_id),
        )
    writes.publish(conn, actor, knowledge_id, knowledge=True)
    return knowledge_id


def _seed(world, actor, label):
    title = "{}-{}".format(_canary(label + "-rule"), QUERY_WORD)
    author = _canary(label + "-author")
    with psycopg.connect(world["dsn"]) as conn:
        library_name = _canary(label + "-library")
        library_id = libraries.create_library(
            conn,
            actor,
            libraries.CreateLibrary(
                organisation_id=actor.account_id,
                name=library_name,
                kind="reference",
                audience_scope="private",
            ),
        ).id
        rule, (source_id, fragment_id, content_hash) = _published(
            {**world, "principal": actor, "lib": library_id}, library_id, title
        )
        writes.update_metadata(
            conn, actor, source_id, {"author": author, "edition": "Synthetic edition"}
        )
        knowledge_id = _publish_knowledge(
            conn,
            actor,
            library_id,
            f"{title}. State the audience before drafting.",
            source_id,
            fragment_id,
        )
        policies.put_policy(
            conn,
            actor,
            library_id,
            policies.LibraryPolicyInput(
                external_processing_allowed=False,
                cloud_mcp_allowed=False,
                export_allowed=True,
                generation_allowed=False,
            ),
        )
    return {
        "actor": actor,
        "lib": library_id,
        "library_name": library_name,
        "title": title,
        "author": author,
        "source_id": source_id,
        "fragment_id": fragment_id,
        "content_hash": content_hash,
        "rule_id": rule["id"],
        "knowledge_id": knowledge_id,
    }


@pytest.fixture
def canary_world(world, runtime_db):
    alice = world["other"]
    return {
        "alice": _seed(world, alice, "alice"),
        "bob": _seed(world, _bob(world), "bob"),
    }


def _run_matrix(world, canary_world, me, them):
    own = canary_world[me]
    foreign = canary_world[them]
    actor = own["actor"]
    secrets = _secrets(foreign)

    with psycopg.connect(world["dsn"]) as conn:
        page = libraries.list_libraries(conn, actor, limit=200)
        assert [lib.id for lib in page.libraries] == [own["lib"]]
        assert page.total == 1
        past_end = libraries.list_libraries(conn, actor, limit=1, offset=1)
        assert list(past_end.libraries) == [] and past_end.total == 1
        assert libraries.get_library(conn, actor, foreign["lib"]) is None

        sources = content.list_sources(conn, actor, own["lib"])
        assert [s for s in sources if str(s.get("id")) == str(own["source_id"])]
        assert content.list_sources(conn, actor, foreign["lib"]) == []

        card = content.read_source(conn, actor, own["source_id"])
        assert card["source"]["title"] == "Fixture"
        assert card["source"]["author"] == own["author"]
        card_blob = json.dumps(card, default=str)
        assert own["title"] in card_blob and str(own["fragment_id"]) in card_blob
        for secret in secrets:
            assert secret not in card_blob
        with pytest.raises(AccessDenied) as denied:
            content.read_source(conn, actor, foreign["source_id"])
        assert "forbidden" in str(denied.value)

        hits = json.dumps(content.search_knowledge(conn, actor, QUERY_WORD), default=str)
        assert own["title"] in hits and str(own["knowledge_id"]) in hits
        for secret in secrets:
            assert secret not in hits

        rows = writes.list_knowledge(conn, actor, own["lib"], limit=500, offset=0)
        assert [str(row.get("id")) for row in rows] == [str(own["knowledge_id"])]
        assert writes.list_knowledge(conn, actor, foreign["lib"], limit=500, offset=0) == []

        rule = content.get_rule(conn, actor, own["rule_id"])
        assert rule["title"] == own["title"]
        with pytest.raises(AccessDenied) as denied:
            content.get_rule(conn, actor, foreign["rule_id"])
        assert "forbidden" in str(denied.value)

        artifact = content.export_skill(conn, actor, own["lib"], [own["rule_id"]])
        assert artifact["filename"] and artifact["sha256"] and artifact["manifest"]
        export_blob = json.dumps(artifact, default=str)
        assert own["title"] in export_blob and str(own["source_id"]) in export_blob
        for secret in secrets:
            assert secret not in export_blob

        for library_id, rule_ids in (
            (foreign["lib"], [foreign["rule_id"]]),
            (own["lib"], [foreign["rule_id"]]),
            (own["lib"], [own["rule_id"], foreign["rule_id"]]),
        ):
            with pytest.raises(AccessDenied) as denied:
                content.export_skill(conn, actor, library_id, rule_ids)
            assert "forbidden" in str(denied.value)
            assert own["title"] not in str(denied.value)


@pytest.mark.parametrize("me,them", [("alice", "bob"), ("bob", "alice")])
def test_private_canary_isolation(world, canary_world, me, them):
    for _ in range(2):
        _run_matrix(world, canary_world, me, them)
