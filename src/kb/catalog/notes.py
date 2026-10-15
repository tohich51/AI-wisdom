import re
from hashlib import sha256
from html import escape

from psycopg.rows import dict_row

from kb.access.policy import AccessDenied, Principal, transaction_identity
from kb.catalog.policies import require_manifest_export
from kb.catalog.research import checked_run

_MARKDOWN = re.compile(r"([\\`*_\[\]()#+\-.!|>~=])")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_DISCLAIMER = "Черновик по выбранным фрагментам; требует проверки."
_CONTEXT_NOTICE = "Ограничение контекста: доступный контекст был ограничен; требуется проверка."


def _validate_note(title: str, comment: str) -> None:
    if not isinstance(title, str) or not title.strip() or len(title) > 500:
        raise ValueError("note_title_invalid")
    if not isinstance(comment, str) or len(comment) > 8000:
        raise ValueError("note_comment_too_long")


def _owner_ids(principal: Principal) -> tuple[object, object]:
    return principal.account_id, principal.principal_id


def _md(value: object) -> str:
    text = escape(str(value), quote=False)
    return _MARKDOWN.sub(lambda match: "\\" + match.group(0), text)


def _inline(value: object) -> str:
    return " ".join(_md(value).split())


def _identifier(value: object) -> str:
    text = str(value)
    if _IDENTIFIER.fullmatch(text):
        return text
    return _inline(text)


def _citation_label(value: object) -> str:
    return f"[citation:{_identifier(value)}]"


def save_note(conn, principal: Principal, run_id, title, comment: str = "") -> dict:
    _validate_note(title, comment)
    account_id, principal_id = _owner_ids(principal)
    with transaction_identity(conn, principal):
        with conn.cursor(row_factory=dict_row) as cur:
            run = checked_run(cur, principal, run_id)
            if run["status"] != "done":
                raise ValueError("research_run_not_done")
            if run["result"].get("no_evidence") is not False:
                raise ValueError("note_requires_evidence")
            cur.execute(
                "INSERT INTO kb.research_note "
                "(account_id, principal_id, research_run_id, title, comment) "
                "VALUES (%s, %s, %s, %s, %s) "
                "RETURNING *",
                (account_id, principal_id, run_id, title, comment),
            )
            row = cur.fetchone()
            return {"note": row, "result": run["result"]}


def update_note(conn, principal: Principal, note_id, title, comment, revision) -> dict:
    _validate_note(title, comment)
    if type(revision) is not int or revision < 1:
        raise ValueError("note_revision_invalid")
    account_id, principal_id = _owner_ids(principal)
    with transaction_identity(conn, principal):
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM kb.research_note "
                "WHERE id = %s AND account_id = %s AND principal_id = %s",
                (note_id, account_id, principal_id),
            )
            note = cur.fetchone()
            if note is None:
                raise AccessDenied("forbidden")
            run = checked_run(cur, principal, note["research_run_id"])
            if run["status"] != "done":
                raise ValueError("research_run_not_done")
            cur.execute(
                "UPDATE kb.research_note SET title = %s, comment = %s "
                "WHERE id = %s AND account_id = %s "
                "AND principal_id = %s AND revision = %s RETURNING *",
                (title, comment, note_id, account_id, principal_id, revision),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError("note_revision_conflict")
            return {"note": row, "result": run["result"]}


def get_note(conn, principal: Principal, note_id) -> dict:
    account_id, principal_id = _owner_ids(principal)
    with transaction_identity(conn, principal):
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM kb.research_note "
                "WHERE id = %s AND account_id = %s AND principal_id = %s",
                (note_id, account_id, principal_id),
            )
            note = cur.fetchone()
            if note is None:
                raise AccessDenied("forbidden")
            run = checked_run(cur, principal, note["research_run_id"])
            if run["status"] != "done":
                raise ValueError("research_run_not_done")
            return {"note": note, "result": run["result"]}


def list_notes(conn, principal: Principal, library_id) -> dict:
    account_id, principal_id = _owner_ids(principal)
    with transaction_identity(conn, principal):
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id FROM kb.library WHERE id = %s AND organisation_id = %s",
                (library_id, account_id),
            )
            if cur.fetchone() is None:
                raise AccessDenied("forbidden")
            cur.execute(
                "SELECT n.* FROM kb.research_note AS n "
                "JOIN kb.research_run AS r ON r.id = n.research_run_id "
                "WHERE n.account_id = %s AND n.principal_id = %s "
                "AND r.library_id = %s "
                "ORDER BY n.updated_at DESC, n.id LIMIT 101",
                (account_id, principal_id, library_id),
            )
            rows = cur.fetchall()
            truncated = len(rows) > 100
            return {"notes": rows[:100], "truncated": truncated}


def export_research(conn, principal: Principal, run_id) -> dict:
    with transaction_identity(conn, principal):
        with conn.cursor(row_factory=dict_row) as cur:
            run = checked_run(cur, principal, run_id)
            require_manifest_export(cur, run["library_id"], run["manifest"])
            if run["status"] != "done":
                raise ValueError("research_run_not_done")
            result = run["result"]
            if result.get("no_evidence") is not False:
                raise ValueError("note_requires_evidence")
            title = result["title"]
            lines = [f"# {_inline(title)}", "", f"> {_DISCLAIMER}"]
            if result.get("context_limited") is True:
                lines.extend(["", f"> {_CONTEXT_NOTICE}"])
            for item in result["items"]:
                heading = item["heading"] or "Пункт"
                lines.extend(
                    [
                        "",
                        f"## {_inline(heading)}",
                        "",
                        _md(item["text"]),
                        "",
                    ]
                )
                citation_ids = item.get("citation_ids") or ()
                if citation_ids:
                    labels = " ".join(_citation_label(cid) for cid in citation_ids)
                    lines.extend([f"Источники: {labels}", ""])
            citations = result["citations"]
            if citations:
                lines.extend(["", "## Citations", ""])
            for citation in citations:
                citation_id = citation["id"]
                quote_text = citation["quote"]
                citation_title = citation["title"]
                lines.extend(
                    [
                        f"### {_citation_label(citation_id)}",
                        "",
                        f"Цитата: {_md(quote_text)}",
                        "",
                        f"Название: {_inline(citation_title)}",
                    ]
                )
                for key, label in (("author", "Автор"), ("edition", "Издание")):
                    value = citation.get(key)
                    if value is not None and value != "":
                        lines.append(f"{label}: {_inline(value)}")
                locator = citation.get("locator") or {}
                for key in ("chapter", "file_page", "printed_label", "paragraph", "ordinal"):
                    value = locator.get(key)
                    if value is not None and value != "":
                        lines.append(f"{key}: {_inline(value)}")
                lines.extend(
                    [
                        f"fragment_id: {_identifier(citation['fragment_id'])}",
                        f"source_version_id: {_identifier(citation['source_version_id'])}",
                        "",
                    ]
                )
            markdown = "\n".join(lines) + "\n"
            filename = f"research-{run['id']}.md"
            return {
                "filename": filename,
                "markdown": markdown,
                "sha256": sha256(markdown.encode("utf-8")).hexdigest(),
            }
