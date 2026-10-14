"""An explicit rule draft from a checked research result; never automatic publication."""

from uuid import UUID

from psycopg.rows import dict_row

from kb.access.policy import transaction_identity
from kb.catalog.research import checked_run
from kb.catalog.writes import RuleInput, create_rule


def draft_rule_from_research(conn, principal, run_id, title, when_to_apply, expected_effect):
    with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cur:
        row = checked_run(cur, principal, run_id)
        if row["status"] != "done" or row["result"]["no_evidence"]:
            raise ValueError("research_result_not_ready")
        result = row["result"]
        fragment_ids = list(dict.fromkeys(UUID(c["fragment_id"]) for c in result["citations"]))
        if len(fragment_ids) > 20:
            raise ValueError("select_a_smaller_research_scope")
        cur.execute(
            """SELECT DISTINCT s.library_id FROM kb.fragment f JOIN kb.source s ON s.id=f.source_id
                     WHERE f.id=ANY(%s::uuid[])""",
            (fragment_ids,),
        )
        if {x["library_id"] for x in cur.fetchall()} != {row["library_id"]}:
            raise ValueError("draft_rule_requires_sources_in_target_library")
        body = RuleInput(
            title=title,
            when_to_apply=when_to_apply,
            expected_effect=expected_effect,
            actions=[item["text"] for item in result["items"]][:20],
            fragment_ids=fragment_ids,
            verification="Проверьте применимость и ожидаемый результат перед публикацией.",
        )
        return create_rule(conn, principal, row["library_id"], body)
