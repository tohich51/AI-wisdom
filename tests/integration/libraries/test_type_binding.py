import uuid
from uuid import UUID

import pytest

from kb.access.policy import Principal, transaction_identity
from kb.catalog.idempotency import LIBRARY_CREATE, find_create_operation, hash_create_request
from kb.catalog.libraries import CreateLibrary

pytestmark = pytest.mark.integration


def _register(api, principal, key, base_kind):
    response = api.as_(principal).post(
        "/library-types", json={"key": key, "base_kind": base_kind, "title": key}
    )
    assert response.status_code == 201, response.text
    assert response.json()["base_kind"] == base_kind


def _create_library(api, principal, organisation, name, type_key):
    payload = {
        "organisation_id": str(organisation),
        "name": name,
        "kind": "brand",
        "audience_scope": "private",
    }
    if type_key is not None:
        payload["type_key"] = type_key
    response = api.as_(principal).post("/libraries", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _receipt_hash(pool, principal, key):
    with pool.connection() as connection:
        with transaction_identity(connection, principal), connection.cursor() as cursor:
            receipt = find_create_operation(cursor, principal, LIBRARY_CREATE, key)
            assert receipt is not None
            return receipt.request_hash


def _digest(spec):
    return hash_create_request(
        LIBRARY_CREATE, spec.model_dump(mode="json", exclude={"idempotency_key"}, exclude_none=True)
    )


def test_custom_binding_project_and_filters(api, world, people):
    owner = api.as_(people.owner)
    stranger = api.as_(people.stranger)
    organisation = world.organisation()
    suffix = uuid.uuid4().hex[:8]
    brand_type = f"brand_{suffix}"
    project_type = f"project_{suffix}"
    _register(api, people.owner, brand_type, "brand")
    _register(api, people.owner, project_type, "project")
    one = _create_library(api, people.owner, organisation, f"brand-one-{suffix}", brand_type)
    two = _create_library(api, people.owner, organisation, f"brand-two-{suffix}", brand_type)
    legacy = _create_library(api, people.owner, organisation, f"brand-legacy-{suffix}", None)
    read = owner.get(f"/libraries/{one['id']}")
    assert read.status_code == 200 and read.json()["type_key"] == brand_type
    project_response = owner.post(
        "/projects",
        json={
            "organisation_id": str(organisation),
            "name": f"typed-project-{suffix}",
            "type_key": project_type,
        },
    )
    assert project_response.status_code == 201, project_response.text
    project_id = project_response.json()["project"]["id"]
    project_read = owner.get(f"/projects/{project_id}").json()
    assert project_read["project"]["type_key"] == project_type
    world.link(UUID(project_id), UUID(one["id"]))
    context = owner.get(f"/projects/{project_id}/context")
    assert context.status_code == 200, context.text
    assert context.json()["links"][0]["library"]["type_key"] == brand_type

    page = owner.get("/libraries", params={"type_key": brand_type, "limit": 1}).json()
    assert page["total"] == 2 and {item["id"] for item in page["libraries"]} == {one["id"]}
    page = owner.get("/libraries", params={"type_key": brand_type, "limit": 1, "offset": 1}).json()
    assert page["total"] == 2 and {item["id"] for item in page["libraries"]} == {two["id"]}
    hidden = stranger.get("/libraries", params={"type_key": brand_type}).json()
    assert hidden["total"] == 0 and hidden["libraries"] == []
    core = owner.get("/libraries", params={"type_key": "brand"}).json()
    assert core["total"] == 1 and [item["id"] for item in core["libraries"]] == [legacy["id"]]


def test_invalid_bindings_are_atomic_and_retired_binding_survives(
    api, world, people, admin, run_sql, db_pool
):
    owner = api.as_(people.owner)
    suffix = uuid.uuid4().hex[:8]
    retired_type = f"retired_{suffix}"
    mismatch_type = f"reference_{suffix}"
    _register(api, people.owner, retired_type, "brand")
    _register(api, people.owner, mismatch_type, "reference")
    existing = _create_library(
        api, people.owner, world.organisation(), f"retired-ok-{suffix}", retired_type
    )
    rc, message = run_sql(
        "UPDATE kb.library_type SET retired_at=now() WHERE key=%s", params=(retired_type,)
    )
    assert rc == 0, message
    cases = (
        ("unknown", f"missing_{suffix}"),
        ("retired", retired_type),
        ("mismatch", mismatch_type),
    )
    for label, type_key in cases:
        name = f"reject-{label}-{suffix}"
        response = owner.post(
            "/libraries",
            json={
                "organisation_id": str(world.organisation()),
                "name": name,
                "kind": "brand",
                "type_key": type_key,
            },
        )
        assert response.status_code == 400, response.text
        count = admin.execute(
            "SELECT (SELECT count(*) FROM kb.library WHERE name=%s) + "
            "(SELECT count(*) FROM kb.library_grant g JOIN kb.library l "
            "ON l.id=g.library_id WHERE l.name=%s)",
            (name, name),
        ).fetchone()
        assert count == (0,), (label, name)
    assert owner.get(f"/libraries/{existing['id']}").json()["type_key"] == retired_type
    with db_pool.connection() as conn:
        with transaction_identity(conn, Principal(people.owner, world.organisation())):
            renamed = conn.execute(
                "UPDATE kb.library SET name=%s,kind=kind,type_key=type_key "
                "WHERE id=%s RETURNING id",
                (f"retired-renamed-{suffix}", existing["id"]),
            ).fetchone()
    # Existing runtime RLS has no library UPDATE policy; this feature adds none.
    assert renamed is None
    # The existing privileged configuration path may rename a retired binding.
    renamed = admin.execute(
        "UPDATE kb.library SET name=%s,kind=kind,type_key=type_key WHERE id=%s RETURNING id",
        (f"retired-renamed-{suffix}", existing["id"]),
    ).fetchone()
    assert renamed == (UUID(existing["id"]),), renamed
    read = owner.get(f"/libraries/{existing['id']}")
    assert read.status_code == 200 and read.json()["name"] == f"retired-renamed-{suffix}"


def test_type_key_changes_the_existing_idempotency_hash(api, world, people, admin, db_pool):
    owner = api.as_(people.owner)
    suffix = uuid.uuid4().hex[:8]
    type_key = f"brand_{suffix}"
    _register(api, people.owner, type_key, "brand")
    organisation = world.organisation()
    legacy_spec = CreateLibrary(
        organisation_id=organisation, name=f"idem-old-{suffix}", kind="brand"
    )
    legacy_fields = legacy_spec.model_dump(
        mode="json", exclude={"idempotency_key"}, exclude_none=True
    )
    assert set(legacy_fields) == {"organisation_id", "name", "kind", "audience_scope"}
    legacy_key = f"legacy-{suffix}"
    first = owner.post("/libraries", json={**legacy_fields, "idempotency_key": legacy_key})
    second = owner.post(
        "/libraries", json={**legacy_fields, "type_key": None, "idempotency_key": legacy_key}
    )
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert _receipt_hash(
        db_pool, Principal(people.owner, world.organisation()), legacy_key
    ) == _digest(legacy_spec)
    custom_spec = CreateLibrary(
        organisation_id=organisation, name=f"idem-new-{suffix}", kind="brand", type_key=type_key
    )
    custom_fields = custom_spec.model_dump(
        mode="json", exclude={"idempotency_key"}, exclude_none=True
    )
    assert set(custom_fields) == {"organisation_id", "name", "kind", "type_key", "audience_scope"}
    custom_key = f"custom-{suffix}"
    custom = owner.post("/libraries", json={**custom_fields, "idempotency_key": custom_key})
    replay = owner.post("/libraries", json={**custom_fields, "idempotency_key": custom_key})
    assert custom.status_code == replay.status_code == 201
    assert custom.json()["id"] == replay.json()["id"]
    custom_hash = _receipt_hash(db_pool, Principal(people.owner, world.organisation()), custom_key)
    without_type = dict(custom_fields)
    without_type.pop("type_key")
    assert custom_hash == _digest(custom_spec)
    assert custom_hash != hash_create_request(LIBRARY_CREATE, without_type)
    conflict = owner.post(
        "/libraries", json={**custom_fields, "type_key": None, "idempotency_key": custom_key}
    )
    assert conflict.status_code == 400, conflict.text
    assert (
        _receipt_hash(db_pool, Principal(people.owner, world.organisation()), custom_key)
        == custom_hash
    )
    assert admin.execute(
        "SELECT count(*) FROM kb.library WHERE name=%s", (custom_spec.name,)
    ).fetchone() == (1,)
