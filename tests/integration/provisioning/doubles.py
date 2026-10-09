"""Test doubles for the OpenViking boundary. **These are not OpenViking.**

Read this before using anything in this file.

OpenViking cannot run in the C15 environment: it is not installed, and it
cannot be installed, because its embedding provider is unavailable (no Ollama,
and a paid API is forbidden by the executor contract). So there is no honest
way to test the remote half of provisioning here, and there are two ways to
fake it. One of them is wrong:

* the wrong way is a double that behaves plausibly and a test suite that calls
  it "provisioning works" — that is how an unverified card becomes an accepted
  one without anybody deciding to lie;
* the right way is a double whose every call is recorded, which lets the
  *policy* be tested against a real database, plus an honest statement of what
  was not exercised.

So that is what this is. :class:`RecordingIndexAdmin` records the calls the
orchestration makes, can be told to fail at one specific step, and hands out
keys that are recognisable markers rather than plausible secrets — which makes
"this value reached the database" a thing a test can assert *did not happen*.

Every test that uses one of these says so, and the corresponding check is
recorded as ``not_run`` with its gate in ``docs/handoff/results/C15.json``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from kb.retrieval.provisioning import (
    AccountSpec,
    IssuedKey,
    ProvisioningError,
    RemoteAccount,
    SecretRef,
)

#: A marker that is obviously a marker. If this string ever turns up in a
#: database row, in a log line or in an exception message, the test that
#: follows has caught a leak.
KEY_MARKER = "KEY-MARKER-not-a-real-credential-0000000000"


class RecordingIndexAdmin:
    """A stand-in for the admin port that records instead of pretending.

    It is deliberately dumb: it stores what it was told, it can be made to
    fail at a named step, and it never invents behaviour a real server might
    have. In particular it has no notion of an OpenViking URI scheme, because
    this tree does not know one and inventing one would be fabrication.
    """

    def __init__(self, *, fail_at: str | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.keys_issued: list[str] = []
        self.fail_at = fail_at

    def _record(self, name: str, subject: str = "") -> None:
        self.calls.append((name, subject))
        if self.fail_at == name:
            raise ProvisioningError(
                f"injected failure at step {name!r} (test double, not a real service)"
            )

    def create_account(self, spec: AccountSpec) -> RemoteAccount:
        self._record("create_account", spec.account_ref)
        return RemoteAccount(account_ref=spec.account_ref, created=True)

    def ensure_service_identity(self, spec: AccountSpec, identity: str) -> None:
        self._record("ensure_service_identity", identity)

    def apply_acl(self, spec: AccountSpec) -> None:
        self._record("apply_acl", spec.account_ref)

    def issue_service_key(self, spec: AccountSpec, identity: str) -> IssuedKey:
        self._record("issue_service_key", identity)
        self.keys_issued.append(identity)
        return IssuedKey(identity=identity, secret=KEY_MARKER)

    def describe_account(self, spec: AccountSpec) -> dict[str, object]:
        self._record("describe_account", spec.account_ref)
        return {"acl_entries": len(spec.acl.entries)}

    def calls_named(self, name: str) -> list[str]:
        return [subject for called, subject in self.calls if called == name]


@dataclass
class RecordingSecretSink:
    """Records what it was asked to store, and keeps it in memory.

    The kept values exist so a test can assert that a key never reached the
    database, the journal or the return value. They live in the test process
    and nowhere else.
    """

    written: list[tuple[str, str]] = field(default_factory=list)

    def write(self, identity: str, secret: str) -> SecretRef:
        self.written.append((identity, secret))
        return SecretRef(path=f"kb-secrets/{identity}/key")

    def secrets(self) -> list[str]:
        return [secret for _identity, secret in self.written]


class RecordingIndexReader:
    """A stand-in for the read port that answers from a canned list.

    It records the exact scopes it was handed, so a test can assert that a
    search never asked about an account the caller was not entitled to — which
    is A02's "отказ до чужого index-запроса", and the half that does not need
    a live server to be meaningful.
    """

    def __init__(self, hits: list[Any] | None = None) -> None:
        self.hits = list(hits or [])
        self.asked_with: list[list[Any]] = []
        self.queries: list[Any] = []

    def find(self, scopes: list[Any], query: Any) -> list[Any]:
        self.asked_with.append(list(scopes))
        self.queries.append(query)
        return list(self.hits)

    def accounts_asked_for(self) -> set[str]:
        return {scope.account_ref for scopes in self.asked_with for scope in scopes}


class RefusingGenerativePort:
    """A model runner that must never be called.

    If the search path ever reaches the generative door, this raises. The test
    that uses it also asserts the tripwire counter stayed at zero, so the two
    mechanisms fail independently.
    """

    def run(self, request: Any) -> str:
        raise AssertionError(
            "a generative port was reached from a path that must not make "
            "generative calls; ordinary search is vectors-only"
        )
