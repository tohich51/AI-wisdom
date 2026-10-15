"""The research product: what a research run decides, and how it is checked.

A run reads sources, drafts material, cites it and has the result validated
before anything is published. That reasoning lives here and nowhere else, so
the durable worker in :mod:`kb.jobs.research` stays a queue consumer and
the catalog stays a store.

Like :mod:`kb.jobs`, the package root exports nothing. These modules import the
catalog and the retrieval layer; a package root that pulled them in would make
the import graph of every consumer depend on all of it.
"""
