"""Install the library-owned queue schema once; no runtime role owns it."""

import hashlib

import procrastinate
import psycopg
from psycopg import sql


def install_queue(dsn):
    app = procrastinate.App(connector=procrastinate.PsycopgConnector(conninfo=dsn))
    expected = hashlib.sha256(app.schema_manager.get_schema().encode()).hexdigest()
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(647232)")
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS public.pkb_queue_schema "
                "(version text PRIMARY KEY,checksum text NOT NULL)"
            )
            current = conn.execute(
                "SELECT version,checksum FROM public.pkb_queue_schema"
            ).fetchone()
            if current and current != ("3.10.0", expected):
                raise RuntimeError("queue_schema_drift")
            if current is None:
                with conn.transaction():
                    conn.execute(app.schema_manager.get_schema())
                    conn.execute(
                        "INSERT INTO public.pkb_queue_schema VALUES(%s,%s)", ("3.10.0", expected)
                    )
            for relation, kind in [("pg_tables", "TABLE"), ("pg_sequences", "SEQUENCE")]:
                column = "tablename" if kind == "TABLE" else "sequencename"
                query = sql.SQL(
                    "SELECT {} FROM {} WHERE schemaname='public' AND {} LIKE 'procrastinate\\_%'"
                ).format(sql.Identifier(column), sql.Identifier(relation), sql.Identifier(column))
                for (name,) in conn.execute(query).fetchall():
                    conn.execute(
                        sql.SQL("REVOKE ALL ON {} FROM PUBLIC").format(sql.Identifier(name))
                    )
                    conn.execute(
                        sql.SQL("GRANT ALL ON {} {} TO kb_app,kb_worker").format(
                            sql.SQL(kind), sql.Identifier(name)
                        )
                    )
            conn.execute("REVOKE ALL ON public.pkb_queue_schema FROM PUBLIC")
        finally:
            conn.execute("SELECT pg_advisory_unlock(647232)")
