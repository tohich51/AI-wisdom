"""First-library enqueue and deduplication through the real async connector."""

from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from kb.jobs.indexer import build_queue
from kb.jobs.schema import install_queue

pytestmark = pytest.mark.integration


def test_first_pending_library_is_queued_once_with_real_connector(world, admin) -> None:
    world.account_id = world.organisation()
    admin.execute(
        "INSERT INTO kb.membership(organisation_id,principal_id,issuer,subject,status) "
        "VALUES(%s,%s,'https://synthetic.invalid',%s,'active')",
        (world.account_id, world.owner, str(world.owner)),
    )
    library, _ = world.library_with(("owner", "manager"))
    world.knowledge(library, title="A published statement awaiting automatic indexing")
    install_queue(admin.info.dsn)
    login = "queue_test_" + uuid4().hex[:12]
    admin.execute(
        sql.SQL(
            "CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS IN ROLE kb_app,kb_provisioner"
        ).format(sql.Identifier(login))
    )
    dsn = make_conninfo(admin.info.dsn, user=login)
    app, enqueue = build_queue(dsn, world.manager, {})
    with app.open():
        enqueue()
        enqueue()
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert conn.execute(
            "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=%s", (login,)
        ).fetchone() == (False, False)
    rows = admin.execute(
        "SELECT task_name,queue_name,status,args,lock,queueing_lock "
        "FROM procrastinate_jobs WHERE args->>'library_id'=%s",
        (str(library),),
    ).fetchall()
    assert len(rows) == 1
    task, queue, status, args, lock, queueing_lock = rows[0]
    assert (task, queue, status) == ("kb.reindex_library", "semantic", "todo")
    assert args == {"library_id": str(library)}
    assert lock == "semantic-installation"
    assert queueing_lock == "semantic:" + str(library)
