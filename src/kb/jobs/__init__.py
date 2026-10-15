"""The durable job layer: one queue, one writer, and the workers that drain it.

What lives here is the machinery that survives a restart. A submitted source, a
semantic projection, a research run and a model draft all end as a queued task
with identifiers in Procrastinate and content in PostgreSQL; these modules are
what claim those tasks and what installs the queue schema the first time.

The package has no re-exports on purpose. Importing ``kb.jobs.ingestion`` from
the package root would pull the catalog, the queue and the storage layer into
every consumer of the queue, and a worker that only needs the schema would pay
for the world to find out which module owns a name.
"""
