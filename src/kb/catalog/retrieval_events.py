"""Record an authorized issuance, separately from reading or explicit experience."""

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
