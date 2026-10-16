"""Record an authorized issuance, separately from reading or explicit experience."""

from collections.abc import Sequence
from uuid import UUID

from kb.access.policy import AccessDenied


def record_retrieval(
    cur,
    principal,
    *,
    item_kind,
    item_id,
    version_no=None,
) -> UUID:
    if principal.generation_watermark < 1:
        raise AccessDenied("forbidden")

    params: tuple[object, ...]
    if item_kind == "source":
        if version_no is not None:
            raise ValueError("source_version_must_be_none")
        sql = """
            INSERT INTO kb.retrieval_event(
              principal_id,account_id,library_id,item_kind,item_id,version_no
            )
            SELECT %s,l.organisation_id,l.id,'source',s.id,NULL
            FROM kb.source AS s
            JOIN kb.library AS l ON l.id=s.library_id
            WHERE s.id=%s AND l.organisation_id=%s AND %s>=1
            RETURNING id
        """
        params = (
            principal.principal_id,
            item_id,
            principal.account_id,
            principal.generation_watermark,
        )
    elif item_kind == "knowledge":
        if version_no is not None:
            raise ValueError("knowledge_version_must_be_none")
        sql = """
            INSERT INTO kb.retrieval_event(
              principal_id,account_id,library_id,item_kind,item_id,version_no
            )
            SELECT %s,l.organisation_id,l.id,'knowledge',k.id,NULL
            FROM kb.knowledge AS k
            JOIN kb.library AS l ON l.id=k.library_id
            WHERE k.id=%s AND l.organisation_id=%s AND %s>=1
            RETURNING id
        """
        params = (
            principal.principal_id,
            item_id,
            principal.account_id,
            principal.generation_watermark,
        )
    elif item_kind == "experience":
        if version_no is not None:
            raise ValueError("experience_version_must_be_none")
        sql = """
            INSERT INTO kb.retrieval_event(
              principal_id,account_id,library_id,item_kind,item_id,version_no
            )
            SELECT %s,l.organisation_id,l.id,'experience',e.id,NULL
            FROM kb.shared_experience AS e
            JOIN kb.library AS l ON l.id=e.library_id
            WHERE e.id=%s AND l.organisation_id=%s AND %s>=1
            RETURNING id
        """
        params = (
            principal.principal_id,
            item_id,
            principal.account_id,
            principal.generation_watermark,
        )
    elif item_kind == "rule":
        if version_no is not None and (
            isinstance(version_no, bool) or not isinstance(version_no, int) or version_no <= 0
        ):
            raise ValueError("invalid_version_no")
        sql = """
            WITH canonical_rule AS (
              SELECT r.id,r.version_no,l.organisation_id,l.id AS library_id
              FROM kb.rule AS r
              JOIN kb.library AS l ON l.id=r.library_id
              WHERE r.id=%s
                AND l.organisation_id=%s
                AND %s>=1
                AND (%s::integer IS NULL OR r.version_no=%s)
            )
            INSERT INTO kb.retrieval_event(
              principal_id,account_id,library_id,item_kind,item_id,version_no
            )
            SELECT %s,organisation_id,library_id,
                   'rule',id,version_no
            FROM canonical_rule
            RETURNING id
        """
        params = (
            item_id,
            principal.account_id,
            principal.generation_watermark,
            version_no,
            version_no,
            principal.principal_id,
        )
    else:
        raise ValueError("invalid_item_kind")

    cur.execute(sql, params)
    row = cur.fetchone()
    if row is None:
        raise AccessDenied("forbidden")
    event_id = row[0] if isinstance(row, tuple) else row["id"]
    return event_id


def record_knowledge_retrievals(
    cur,
    principal,
    *,
    item_ids: Sequence[UUID],
) -> list[UUID]:
    """Record authorized knowledge issuances in caller-supplied order.

    The correlated event UUIDs are prepared before the one INSERT, so response
    mapping never depends on the order in which PostgreSQL emits RETURNING.
    Duplicate inputs remain distinct events. A local transaction is a
    savepoint when the caller already owns one.
    """
    if principal.generation_watermark < 1:
        raise AccessDenied("forbidden")

    try:
        ids = tuple(item_ids)
    except TypeError as exc:
        raise ValueError("item_ids must be a sequence of UUIDs") from exc
    if any(not isinstance(item_id, UUID) for item_id in ids):
        raise ValueError("item_ids must contain only UUIDs")
    if not ids:
        return []

    with cur.connection.transaction():
        cur.execute(
            """
            WITH requested AS MATERIALIZED (
              SELECT input.item_id, input.position_no
              FROM unnest(%s::uuid[]) WITH ORDINALITY
                   AS input(item_id, position_no)
            ),
            canonical AS MATERIALIZED (
              SELECT requested.position_no, requested.item_id,
                     k.library_id, l.organisation_id
              FROM requested
              JOIN kb.knowledge AS k ON k.id=requested.item_id
              JOIN kb.library AS l ON l.id=k.library_id
              WHERE l.organisation_id=%s
                AND %s>=1
                AND k.publication<>'withdrawn'
                AND NOT EXISTS (
                  SELECT 1
                  FROM kb.knowledge_provenance AS p
                  JOIN kb.source AS s ON s.id=p.source_id
                  WHERE p.knowledge_id=k.id
                    AND s.publication='withdrawn'
                )
            ),
            guard AS (
              SELECT count(*)=%s AS complete
              FROM canonical
            ),
            prepared AS MATERIALIZED (
              SELECT canonical.position_no, canonical.item_id,
                     gen_random_uuid() AS event_id,
                     %s::uuid AS principal_id,
                     canonical.organisation_id AS account_id,
                     canonical.library_id
              FROM canonical
              CROSS JOIN guard
              WHERE guard.complete
            ),
            inserted AS (
              INSERT INTO kb.retrieval_event(
                id,principal_id,account_id,library_id,item_kind,item_id,version_no
              )
              SELECT event_id,principal_id,account_id,library_id,
                     'knowledge',item_id,NULL
              FROM prepared
              RETURNING id
            )
            SELECT array_agg(
                     prepared.event_id ORDER BY prepared.position_no
                   ) AS event_ids
            FROM prepared
            JOIN inserted ON inserted.id=prepared.event_id
            """,
            (
                list(ids),
                principal.account_id,
                principal.generation_watermark,
                len(ids),
                principal.principal_id,
            ),
        )
        row = cur.fetchone()
        if row is None:
            raise AccessDenied("forbidden")
        event_ids = row[0] if isinstance(row, tuple) else row["event_ids"]
        if event_ids is None or len(event_ids) != len(ids):
            raise AccessDenied("forbidden")
        mapped = list(event_ids)

    return mapped
