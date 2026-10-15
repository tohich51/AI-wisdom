"""Outbound adapters: the data and services this application does not own.

What is here is the blob store the uploaded originals live in, the OpenViking
vector reader and writer, and the secret store the read keys come from. Each one
is an outbound edge: the code, the protocol and the failure modes all belong to
something outside this process.

The model runner is *not* here. It used to be, and this docstring was the
argument for it — that spawning somebody else's CLI is an adapter. The frozen
contract's layer rule put it the other way: `kb.jobs` is what survives a
restart, and the model runner is a durable queue service behind the
`model-runner` CLI name. It was moved to :mod:`kb.jobs` and this paragraph was
left behind arguing against the move, which made two documents in one repository
disagree about the same module. ``kb.cli`` resolves the service name to
``kb.jobs.model_runner``; this is the record of why.

Keeping the boundary explicit still matters for the review that has to happen
before any change here: an adapter is the only place allowed to speak another
system's protocol, and it must never be handed a value that came from a request
body without the boundary that data is allowed to cross.
"""
