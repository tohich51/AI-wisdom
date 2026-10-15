"""Outbound adapters: code that calls a process or a service we do not own.

The model runner belongs here rather than in :mod:`kb.jobs` because the part
that is a job — claim, authorise, persist — belongs to the job layer, and the
part that is not ours — spawn the cloud coding agent, parse its stdout, refuse
a malformed answer — is an adapter around someone else's contract.

Keeping the boundary explicit matters for the review that has to happen before
any change here: an adapter is the only place allowed to build an argv, and it
must never be handed a value that came from a request body.
"""
