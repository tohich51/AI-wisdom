"""Explicit processing/export policy, changed only by a library manager."""

from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, StrictBool

from kb.access.policy import AccessDenied, transaction_identity


class LibraryPolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    external_processing_allowed: StrictBool
    cloud_mcp_allowed: StrictBool
    export_allowed: StrictBool
    generation_allowed: StrictBool | None = None


def get_policy(conn, principal, library_id):
    with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT l.id AS library_id, "
            "COALESCE(p.external_processing_allowed,true) AS external_processing_allowed, "
            "COALESCE(p.cloud_mcp_allowed,false) AS cloud_mcp_allowed, "
            "COALESCE(p.export_allowed,true) AS export_allowed, "
            "COALESCE(g.generation_allowed,false) AS generation_allowed, "
            "kb.effective_role(kb.current_principal(),l.id) AS effective_role "
            "FROM kb.library l LEFT JOIN kb.library_policy p ON p.library_id=l.id "
            "LEFT JOIN kb.generation_policy g ON g.library_id=l.id "
            "WHERE l.id=%s",
            (library_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise AccessDenied("forbidden")
        return dict(row)


def put_policy(conn, principal, library_id, policy: LibraryPolicyInput):
    with transaction_identity(conn, principal):
        row = conn.execute(
            "SELECT id FROM kb.library WHERE id=%s AND "
            "kb.role_rank(kb.effective_role(kb.current_principal(),id))>=40",
            (library_id,),
        ).fetchone()
        if row is None:
            raise AccessDenied("forbidden")
        conn.execute(
            "INSERT INTO kb.library_policy(library_id,external_processing_allowed,"
            "cloud_mcp_allowed,export_allowed) VALUES(%s,%s,%s,%s) "
            "ON CONFLICT(library_id) DO UPDATE SET "
            "external_processing_allowed=EXCLUDED.external_processing_allowed,"
            "cloud_mcp_allowed=EXCLUDED.cloud_mcp_allowed,export_allowed=EXCLUDED.export_allowed",
            (
                library_id,
                policy.external_processing_allowed,
                policy.cloud_mcp_allowed,
                policy.export_allowed,
            ),
        )
        if policy.generation_allowed is not None:
            conn.execute(
                "INSERT INTO kb.generation_policy(library_id,generation_allowed,"
                "max_concurrency,requires_owner_start) VALUES(%s,%s,1,true) "
                "ON CONFLICT(library_id) DO UPDATE SET "
                "generation_allowed=EXCLUDED.generation_allowed",
                (library_id, policy.generation_allowed),
            )
        return get_policy(conn, principal, library_id)


def require_export(cur, library_ids):
    # Call only within transaction_identity and after resolving the actual
    # object/manifest, never from a caller-supplied list of dependencies.
    for library_id in set(library_ids):
        cur.execute(
            "SELECT 1 FROM kb.library WHERE id=%s AND kb.library_policy_allows(id,'export')",
            (library_id,),
        )
        if cur.fetchone() is None:
            raise AccessDenied("forbidden")


def require_manifest_export(cur, library_id, manifest):
    source_ids = [item["source_id"] for item in manifest["sources"]]
    cur.execute("SELECT library_id FROM kb.source WHERE id=ANY(%s::uuid[])", (source_ids,))
    rows = cur.fetchall()
    if len(rows) != len(set(source_ids)):
        raise AccessDenied("forbidden")
    require_export(cur, [library_id, *[row["library_id"] for row in rows]])
