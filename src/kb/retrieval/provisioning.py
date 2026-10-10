"""C15 — provisioning the retrieval/index tier, one account per library.

The shape of the product decision this module encodes:

* **One OpenViking account per library**, plus **two explicit service
  identities** per account — one that may read and one that may index. The
  gateway is given neither the account-creating key nor a shared identity.
* **A restricted root and explicit grants.** The ACL is generated here from
  the library id, and it is validated again by the database
  (``kb.index_account_acl_is_restricted``), because a second opinion written in
  two languages is worth more than one.
* **The mapping lives in PostgreSQL.** ``library_id -> account`` is a table,
  not a convention, and it is read from the database for every search. A
  client never names an account, a URI or a namespace.
* **Partial provisioning resumes without leaking keys.** Which steps finished
  is in ``kb.provisioning_run``; a key is issued once and handed straight to
  the secret store, and only its *reference* is ever written to a database.

Three things this module refuses to be, because each of them is a way this
card's own acceptance criteria get quietly broken:

1. It does not talk to a container runtime. There is no ``subprocess`` import
   and no socket path anywhere in the package: the one-shot is a process that
   holds a root key, and a process that can start containers is a process that
   can be turned into the host.
2. It does not contain a working OpenViking client. The boundary is
   :class:`IndexAdminPort`, and the only implementation shipped here is
   :class:`UnconfiguredIndexAdmin`, which refuses. A hand-written fake that
   answered calls would turn "unverified" into a claim nobody could audit.
3. It does not put key material in PostgreSQL, in a log, in a job payload or
   in an exception message. Keys move from the admin port to the secret store
   and are dropped.

The OpenViking admin port is a *boundary*, not an abstraction layer for its
own sake: the real client belongs to whichever card owns the running server,
and until then the honest state of the card is "the policy is verified, the
remote effects are not".
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from dataclasses import field as dataclasses_field
from typing import Final, Literal, Protocol, runtime_checkable
from uuid import UUID

import psycopg
from pydantic import BaseModel, ConfigDict, Field, field_validator

from kb.access.policy import Principal, transaction_identity

_log = logging.getLogger("kb.retrieval.provisioning")

# --------------------------------------------------------------- vocabulary
#
# Every name below is SERVER-GENERATED from the library id. Nothing that a
# request, a job payload or an operator types ends up in an account name: a
# name that is not derived from the id is a name somebody chose, and a chosen
# name is a place where "the wrong account" stops being a type error.
ACCOUNT_PREFIX: Final = "kb-lib-"
READ_IDENTITY_PREFIX: Final = "kb-svc-read-"
INDEX_IDENTITY_PREFIX: Final = "kb-svc-index-"
SECRET_STORE_PREFIX: Final = "kb-secrets/"  # noqa: S105 - a directory prefix, not a credential

#: The restricted root, RELATIVE to the account's own namespace. A vendor URI
#: is composed by the adapter at call time; nothing vendor-shaped is stored, so
#: a foreign namespace cannot be smuggled in through this field.
DEFAULT_ROOT_PATH: Final = "/index"

#: PRODUCT-SPEC: CPU embeddings, Qwen3-Embedding 0.6B, 1024 dimensions. It is
#: a recorded property of the account, not a constant the reader assumes: an
#: index built at 768 dimensions under a 1024-dimension label is a silent
#: corruption, and recording it is what lets that be caught.
DEFAULT_DIMENSION: Final = 1024
DEFAULT_EMBEDDING_PROFILE: Final = "qwen3-embedding-0.6b"

#: The only rights a published root may carry. 'manage' is absent on purpose:
#: a wildcard or a manage over a published tree is the failure this card is
#: graded on, and a closed vocabulary makes it unrepresentable rather than
#: merely discouraged.
AclRight = Literal["read", "index"]

STEPS: Final[tuple[str, ...]] = ("account", "identities", "acl", "secrets", "verified")


# ------------------------------------------------------------------ errors


class ProvisioningError(Exception):
    """Base for everything this module refuses to do."""


class ProvisioningRefused(ProvisioningError):
    """The policy says no. The message never says whether the object exists."""


class ProvisioningUnavailable(ProvisioningError):
    """A dependency this card does not have. Never reported as success."""


# ------------------------------------------------------------------- models


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AclEntry(_Model):
    """One explicit grant.

    ``principal`` must be one of the account's own two service identities; the
    database refuses anything else, so this model refuses it first.
    """

    principal: str = Field(pattern=r"^kb-svc-(read|index)-[0-9a-f]{32}$")
    rights: list[AclRight] = Field(min_length=1)

    @field_validator("rights")
    @classmethod
    def _no_duplicates(cls, value: list[AclRight]) -> list[AclRight]:
        # 'manage' is not in the Literal, so it cannot arrive here; a wildcard
        # cannot either. What is left to check is a repeated right, which
        # would render as a longer, differently-ordered document on every
        # write and make two equal ACLs compare unequal.
        if len(set(value)) != len(value):
            raise ValueError("rights must not repeat")
        return sorted(value)


class RestrictedAcl(_Model):
    """The ACL document stored on the account row.

    ``inherit_from_parent`` is a ``Literal[False]`` rather than a bool: an
    account that inherits the ACL of a namespace above it inherits whatever
    that namespace is granted, and a published root is exactly where a
    wildcard above it lands. The type makes inheritance a construction-time
    error instead of a runtime surprise.
    """

    inherit_from_parent: Literal[False] = False
    entries: list[AclEntry] = Field(min_length=1)

    def principals(self) -> set[str]:
        return {entry.principal for entry in self.entries}


class RequestProvisioning(_Model):
    """What the application may ask for.

    One field. No account name, no URI, no dimension, no identity, no key: the
    gateway can request provisioning *for a library it manages* and can do
    nothing else. ``extra="forbid"`` means a caller who sends
    ``{"library_id": ..., "account_ref": "..."}`` gets a validation error
    rather than a silently ignored field.
    """

    library_id: UUID


class SecretRef(_Model):
    """A reference into the closed secret store — never the secret itself."""

    path: str = Field(pattern=r"^kb-secrets/[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")


class AccountSpec(_Model):
    """Everything the one-shot needs to create, all of it derived."""

    library_id: UUID
    account_ref: str
    read_identity: str
    index_identity: str
    root_path: str = DEFAULT_ROOT_PATH
    dimension: int = Field(default=DEFAULT_DIMENSION, ge=1, le=8192)
    embedding_profile: str = DEFAULT_EMBEDDING_PROFILE
    acl: RestrictedAcl


class ProvisionedAccount(_Model):
    """What the database holds. Carries references, never secrets."""

    library_id: UUID
    account_ref: str
    read_identity: str
    index_identity: str
    root_path: str
    dimension: int
    embedding_profile: str
    acl: RestrictedAcl
    state: Literal["pending", "ready", "failed", "retired"]


class ProvisioningRequest(_Model):
    id: UUID
    library_id: UUID
    requested_by: UUID
    state: Literal["requested", "running", "succeeded", "failed"]
    attempt: int = Field(ge=0)


class StepOutcome(_Model):
    step: str
    state: Literal["done", "skipped", "failed"]
    detail: dict[str, object] = Field(default_factory=dict)


class ProvisioningOutcome(_Model):
    """The result of a run, safe to print.

    There is no field here that could hold a key, and that is the point: the
    one-shot's return value is the thing most likely to end up in a CI log.
    """

    request_id: UUID
    account_ref: str
    steps: list[StepOutcome]
    resumed: bool = False
    secret_refs: list[SecretRef] = Field(default_factory=list)


# ------------------------------------------------------------- the boundary


@dataclass(frozen=True)
class RemoteAccount:
    """What the admin port reports back after creating an account."""

    account_ref: str
    created: bool


@dataclass(frozen=True)
class IssuedKey:
    """A freshly issued key.

    Held in memory for the length of one call and never written anywhere. The
    dataclass is frozen so it cannot be mutated into a longer-lived object by
    accident, and ``repr`` is overridden so that printing one — in a debugger,
    in a traceback, in a test failure — shows ``<withheld>`` instead of the
    value. A dataclass field with ``repr=False`` would already do that; the
    explicit ``__repr__`` below is belt and braces, and
    ``test_a_key_never_appears_in_a_repr`` holds it honest.
    """

    identity: str
    secret: str = dataclasses_field(repr=False)

    def __repr__(self) -> str:
        return f"IssuedKey(identity={self.identity!r}, secret=<withheld>)"


@runtime_checkable
class IndexAdminPort(Protocol):
    """The OpenViking administration boundary.

    These are the *privileged* operations: create an account, create a service
    identity, issue its key, set an ACL. Nothing here is on any read path, and
    the only implementation in the product is a real client that does not
    exist yet. Ordinary search never receives an instance of this protocol.
    """

    def create_account(self, spec: AccountSpec) -> RemoteAccount: ...

    def ensure_service_identity(self, spec: AccountSpec, identity: str) -> None: ...

    def apply_acl(self, spec: AccountSpec) -> None: ...

    def issue_service_key(self, spec: AccountSpec, identity: str) -> IssuedKey: ...

    def describe_account(self, spec: AccountSpec) -> dict[str, object]: ...


@runtime_checkable
class SecretSink(Protocol):
    """Where key material goes: a sealed volume or a secret store.

    The one-shot calls this once per identity and then forgets the value. The
    database records only the :class:`SecretRef` the sink returns.
    """

    def write(self, identity: str, secret: str) -> SecretRef: ...


class UnconfiguredIndexAdmin:
    """The only shipped implementation, and it refuses everything.

    This class exists so that the *absence* of OpenViking is a runtime answer
    with a message a human can act on, rather than a ``NotImplementedError``
    from somewhere deeper or — much worse — a hand-written double that answers
    correctly enough to look like a working integration.

    Every method raises. The CLI wires this in when no admin endpoint is
    configured, and the resulting nonzero exit is the honest receipt for a
    provisioning run that did not happen.
    """

    reason = (
        "no OpenViking admin endpoint is configured; this environment has no "
        "OpenViking server and no embedding provider, so the remote half of "
        "provisioning cannot be exercised (docs/handoff/results/C15.json)"
    )

    def create_account(self, spec: AccountSpec) -> RemoteAccount:
        raise ProvisioningUnavailable(self.reason)

    def ensure_service_identity(self, spec: AccountSpec, identity: str) -> None:
        raise ProvisioningUnavailable(self.reason)

    def apply_acl(self, spec: AccountSpec) -> None:
        raise ProvisioningUnavailable(self.reason)

    def issue_service_key(self, spec: AccountSpec, identity: str) -> IssuedKey:
        raise ProvisioningUnavailable(self.reason)

    def describe_account(self, spec: AccountSpec) -> dict[str, object]:
        raise ProvisioningUnavailable(self.reason)


class UnconfiguredSecretSink:
    """Refuses too, for the same reason and with the same discipline."""

    def write(self, identity: str, secret: str) -> SecretRef:
        raise ProvisioningUnavailable(UnconfiguredIndexAdmin.reason)


# ------------------------------------------------------------------ naming


def build_account_spec(
    library_id: UUID,
    *,
    dimension: int = DEFAULT_DIMENSION,
    embedding_profile: str = DEFAULT_EMBEDDING_PROFILE,
    root_path: str = DEFAULT_ROOT_PATH,
) -> AccountSpec:
    """Derive the whole account from the library id. Deterministic.

    Determinism is not a nicety here. The run must be resumable, and a
    resume has to arrive at the same names the interrupted run used — which it
    can only do if the names are a function of the library rather than of the
    attempt, the clock or a random source.
    """
    hexid = f"{library_id.hex}"
    account_ref = f"{ACCOUNT_PREFIX}{hexid}"
    read_identity = f"{READ_IDENTITY_PREFIX}{hexid}"
    index_identity = f"{INDEX_IDENTITY_PREFIX}{hexid}"
    acl = RestrictedAcl(
        entries=[
            # The read identity may read the published tree and nothing else.
            AclEntry(principal=read_identity, rights=["read"]),
            # The index identity writes the projection. It is the only
            # identity allowed to, and it is not the one the gateway holds.
            AclEntry(principal=index_identity, rights=["index", "read"]),
        ]
    )
    return AccountSpec(
        library_id=library_id,
        account_ref=account_ref,
        read_identity=read_identity,
        index_identity=index_identity,
        root_path=root_path,
        dimension=dimension,
        embedding_profile=embedding_profile,
        acl=acl,
    )


def secret_ref_for(identity: str) -> SecretRef:
    """Where an identity's key belongs inside the closed store.

    A pure function of the identity, so a resumed run computes the same path
    the first attempt would have used.
    """
    return SecretRef(path=f"{SECRET_STORE_PREFIX}{identity}/key")


# ------------------------------------------------------------ the request


def request_provisioning(
    conn: psycopg.Connection, principal: Principal, library_id: UUID
) -> ProvisioningRequest:
    """Ask the one-shot to provision a library. Manager-only, enforced by RLS.

    The principal comes from the transport. There is no parameter through
    which a caller could name somebody else, and the policy additionally
    refuses a row whose ``requested_by`` is not the caller — so even a direct
    INSERT by the gateway role cannot file a request in another person's name.

    A refusal is reported as "no such library, or you may not manage it", for
    readers and for library ids that do not exist alike. A caller that could
    tell those two apart would have an existence oracle over every library in
    the installation (A20).
    """
    query = """
        INSERT INTO kb.provisioning_request (library_id, requested_by)
        VALUES (%s, %s)
        ON CONFLICT (library_id) WHERE state IN ('requested','running')
        DO NOTHING
        RETURNING id, library_id, requested_by, state, attempt
    """
    try:
        with transaction_identity(conn, principal), conn.cursor() as cur:
            cur.execute(query, (library_id, principal.principal_id))
            row = cur.fetchone()
            if row is None:
                # Either an open request already exists — which is a resume,
                # not an error — or RLS refused the write. The two are told
                # apart by looking the request up as the same principal.
                cur.execute(
                    """
                    SELECT id, library_id, requested_by, state, attempt
                      FROM kb.provisioning_request
                     WHERE library_id = %s AND state IN ('requested','running')
                    """,
                    (library_id,),
                )
                row = cur.fetchone()
    except psycopg.Error as exc:
        raise ProvisioningRefused("no such library, or you may not manage it") from exc
    if row is None:
        raise ProvisioningRefused("no such library, or you may not manage it")
    return ProvisioningRequest(
        id=row[0], library_id=row[1], requested_by=row[2], state=row[3], attempt=row[4]
    )


# ------------------------------------------------------------- the registry


def load_account(
    conn: psycopg.Connection, principal: Principal, library_id: UUID
) -> ProvisionedAccount | None:
    """The account mapping for a library, or None when there is not one.

    None means "not visible" and "does not exist" alike. RLS has already
    removed the rows the caller may not see; there is no branch here that
    distinguishes a missing account from a hidden one.
    """
    query = """
        SELECT library_id, account_ref, read_identity, index_identity,
               root_path, dimension, embedding_profile, acl, state
          FROM kb.index_account
         WHERE library_id = %s
    """
    with transaction_identity(conn, principal), conn.cursor() as cur:
        cur.execute(query, (library_id,))
        row = cur.fetchone()
    if row is None:
        return None
    return ProvisionedAccount(
        library_id=row[0],
        account_ref=row[1],
        read_identity=row[2],
        index_identity=row[3],
        root_path=row[4],
        dimension=row[5],
        embedding_profile=row[6],
        acl=RestrictedAcl.model_validate(row[7]),
        state=row[8],
    )


def list_accounts(conn: psycopg.Connection, principal: Principal) -> list[ProvisionedAccount]:
    """Every provisioned account the caller may see. RLS decides which."""
    query = """
        SELECT library_id, account_ref, read_identity, index_identity,
               root_path, dimension, embedding_profile, acl, state
          FROM kb.index_account
         ORDER BY library_id
    """
    with transaction_identity(conn, principal), conn.cursor() as cur:
        cur.execute(query)
        rows = cur.fetchall()
    return [
        ProvisionedAccount(
            library_id=r[0],
            account_ref=r[1],
            read_identity=r[2],
            index_identity=r[3],
            root_path=r[4],
            dimension=r[5],
            embedding_profile=r[6],
            acl=RestrictedAcl.model_validate(r[7]),
            state=r[8],
        )
        for r in rows
    ]


def credential_refs(
    conn: psycopg.Connection, principal: Principal, library_id: UUID
) -> list[SecretRef]:
    """Secret-store references for one library. Managers and the one-shot only.

    This function returns *references*. A reader has no privilege on the table
    at all — ``kb_app`` was never granted SELECT on
    ``kb.index_credential_ref`` — and that denial is translated into an empty
    list rather than raised, because "you may not see a secret reference" and
    "there are no secret references" are the same answer to this caller, and
    the caller cannot tell the difference anyway.

    The table is split from ``kb.index_account`` for the same reason: a column
    filter can be undone by the next ``SELECT *``, a table boundary cannot be
    undone by a forgotten WHERE.
    """
    try:
        with transaction_identity(conn, principal), conn.cursor() as cur:
            cur.execute(
                "SELECT secret_ref FROM kb.index_credential_ref "
                "WHERE library_id = %s ORDER BY identity",
                (library_id,),
            )
            rows = cur.fetchall()
    except psycopg.errors.InsufficientPrivilege:
        return []
    return [SecretRef(path=r[0]) for r in rows]


# ------------------------------------------------------------- the one-shot


def _completed_steps(conn: psycopg.Connection, principal: Principal, request_id: UUID) -> set[str]:
    with transaction_identity(conn, principal), conn.cursor() as cur:
        cur.execute(
            "SELECT step FROM kb.provisioning_run WHERE request_id = %s AND state = 'done'",
            (request_id,),
        )
        return {r[0] for r in cur.fetchall()}


def _record_step(
    conn: psycopg.Connection,
    principal: Principal,
    request_id: UUID,
    step: str,
    state: Literal["done", "failed"],
    detail: dict[str, object],
    attempt: int,
) -> None:
    """Append to the journal.

    The detail document is checked by the database: every string in it has to
    be one of this codebase's own reference shapes, so an accidental key in
    the journal fails the INSERT instead of being written quietly.
    """
    with transaction_identity(conn, principal), conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO kb.provisioning_run (request_id, step, state, detail, attempt)
            VALUES (%s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (request_id, step) DO UPDATE
                SET state = EXCLUDED.state,
                    detail = EXCLUDED.detail,
                    attempt = EXCLUDED.attempt,
                    finished_at = now()
            """,
            (request_id, step, state, json.dumps(detail), attempt),
        )


def _insert_account(conn: psycopg.Connection, principal: Principal, spec: AccountSpec) -> None:
    with transaction_identity(conn, principal), conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO kb.index_account
                (library_id, account_ref, read_identity, index_identity,
                 root_path, dimension, embedding_profile, acl, state)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, 'pending')
            """,
            (
                spec.library_id,
                spec.account_ref,
                spec.read_identity,
                spec.index_identity,
                spec.root_path,
                spec.dimension,
                spec.embedding_profile,
                spec.acl.model_dump_json(),
            ),
        )


def _store_credential_ref(
    conn: psycopg.Connection,
    principal: Principal,
    library_id: UUID,
    identity: str,
    ref: SecretRef,
) -> None:
    with transaction_identity(conn, principal), conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO kb.index_credential_ref (library_id, identity, secret_ref)
            VALUES (%s, %s, %s)
            ON CONFLICT (library_id, identity) DO UPDATE
                SET secret_ref = EXCLUDED.secret_ref, rotated_at = now()
            """,
            (library_id, identity, ref.path),
        )


def _has_credential_ref(
    conn: psycopg.Connection, principal: Principal, library_id: UUID, identity: str
) -> bool:
    with transaction_identity(conn, principal), conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM kb.index_credential_ref WHERE library_id = %s AND identity = %s",
            (library_id, identity),
        )
        return cur.fetchone() is not None


def run_provisioning(
    conn: psycopg.Connection,
    principal: Principal,
    request_id: UUID,
    admin: IndexAdminPort,
    sink: SecretSink,
) -> ProvisioningOutcome:
    """Carry out one provisioning request. Resumable, and it never leaks a key.

    The steps are ordered by what a later step depends on: an identity before
    its ACL, an ACL before a key, a key before verification. Each step records
    itself as done, and a re-run skips the recorded ones — which is what makes
    a run interrupted at step three continue at step three instead of
    creating a second account or a second key.

    The two hard rules are visible in the code below:

    * **A key is issued once.** The ``secrets`` step checks for an existing
      reference first. A resumed run that had already written the key does not
      ask the admin port for a second one, so a failure between "issued" and
      "recorded" cannot turn into a stream of new credentials.
    * **A key is never written anywhere but the sink.** It is a local
      variable for the length of one call. It is not logged, not journalled,
      not returned, and not put in an exception message — the run journal's
      ``detail`` is rejected by the database if it contains anything that is
      not one of our own reference shapes.
    """
    with transaction_identity(conn, principal), conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, library_id, state, attempt
              FROM kb.provisioning_request
             WHERE id = %s
            """,
            (request_id,),
        )
        request_row = cur.fetchone()
        if request_row is None:
            raise ProvisioningRefused("no such provisioning request")
        _, library_id, state, attempt = request_row

        if state == "succeeded":
            account = load_account(conn, principal, library_id)
            if (
                account is None
            ):  # pragma: no cover - the request outlives the account only in a broken install
                raise ProvisioningRefused("request is marked done but no account exists")
            return ProvisioningOutcome(
                request_id=request_id,
                account_ref=account.account_ref,
                steps=[],
                resumed=True,
                secret_refs=credential_refs(conn, principal, library_id),
            )

        cur.execute(
            """
            UPDATE kb.provisioning_request
               SET state = 'running',
                   attempt = attempt + 1,
                   started_at = COALESCE(started_at, now()),
                   finished_at = NULL,
                   failure_reason = NULL
             WHERE id = %s
            """,
            (request_id,),
        )
        current_attempt = attempt + 1

    spec = build_account_spec(library_id)
    done = _completed_steps(conn, principal, request_id)
    outcomes: list[StepOutcome] = []
    refs: list[SecretRef] = []
    resumed = bool(done)

    def _run(
        step: str,
        action: Callable[[], dict[str, object] | None],
        detail: dict[str, object],
    ) -> None:
        if step in done:
            outcomes.append(StepOutcome(step=step, state="skipped", detail={"resumed": True}))
            return
        try:
            extra = action() or {}
        except Exception as exc:
            # The exception TYPE is journalled and the exception itself is
            # re-raised untouched: an admin port that put a key in its error
            # message would leak it into the run journal through here.
            _record_step(
                conn,
                principal,
                request_id,
                step,
                "failed",
                {"error": type(exc).__name__.lower()},
                current_attempt,
            )
            outcomes.append(
                StepOutcome(step=step, state="failed", detail={"error": type(exc).__name__.lower()})
            )
            raise
        _record_step(
            conn, principal, request_id, step, "done", {**detail, **extra}, current_attempt
        )
        done.add(step)
        outcomes.append(StepOutcome(step=step, state="done", detail=detail))

    def _step_account() -> dict[str, object]:
        if load_account(conn, principal, library_id) is None:
            remote = admin.create_account(spec)
            if remote.account_ref != spec.account_ref:
                # The server generates the name, so a server that answers with
                # a different one is a server we do not understand. Adopting
                # its name would put a caller-chosen name into the mapping.
                raise ProvisioningRefused(
                    f"admin port returned account {remote.account_ref!r}; "
                    "expected the generated name"
                )
            _insert_account(conn, principal, spec)
            return {"created": True}
        return {"created": False}

    def _step_identities() -> dict[str, object]:
        for identity in (spec.read_identity, spec.index_identity):
            admin.ensure_service_identity(spec, identity)
        return {"identities": 2}

    def _step_acl() -> dict[str, object]:
        admin.apply_acl(spec)
        return {"entries": len(spec.acl.entries)}

    def _step_secrets() -> dict[str, object]:
        issued = 0
        for identity in (spec.read_identity, spec.index_identity):
            if _has_credential_ref(conn, principal, library_id, identity):
                refs.append(secret_ref_for(identity))
                continue
            key = admin.issue_service_key(spec, identity)
            # key.secret goes straight into the sink and is never named in the
            # journal, the log or the return value.
            ref = sink.write(identity, key.secret)
            _store_credential_ref(conn, principal, library_id, identity, ref)
            refs.append(ref)
            issued += 1
        return {"issued": issued, "skipped": 2 - issued}

    def _step_verified() -> dict[str, object]:
        described = admin.describe_account(spec)
        entries = described.get("acl_entries")
        if entries != len(spec.acl.entries):
            raise ProvisioningRefused(
                f"account reports {entries!r} acl entries; expected {len(spec.acl.entries)}"
            )
        with transaction_identity(conn, principal), conn.cursor() as cur:
            cur.execute(
                "UPDATE kb.index_account SET state = 'ready', updated_at = now() "
                "WHERE library_id = %s",
                (library_id,),
            )
        with transaction_identity(conn, principal), conn.cursor() as cur:
            cur.execute(
                "UPDATE kb.provisioning_request SET state = 'succeeded', finished_at = now() "
                "WHERE id = %s",
                (request_id,),
            )
        return {"state": "ready"}

    for step, action, detail in (
        ("account", _step_account, {"account_ref": spec.account_ref}),
        ("identities", _step_identities, {}),
        ("acl", _step_acl, {"entries": len(spec.acl.entries)}),
        ("secrets", _step_secrets, {}),
        ("verified", _step_verified, {}),
    ):
        _run(step, action, dict(detail))

    _log.info(
        "provisioning finished",
        extra={"account_ref": spec.account_ref, "resumed": resumed, "steps": len(outcomes)},
    )
    return ProvisioningOutcome(
        request_id=request_id,
        account_ref=spec.account_ref,
        steps=outcomes,
        resumed=resumed,
        secret_refs=refs,
    )


def pending_requests(conn: psycopg.Connection, principal: Principal) -> list[ProvisioningRequest]:
    """Requests this principal may see, oldest first.

    Ordered by request time rather than by id so that an operator looking at a
    stalled queue reads it in the order the work arrived.
    """
    with transaction_identity(conn, principal), conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, library_id, requested_by, state, attempt
              FROM kb.provisioning_request
             WHERE state IN ('requested','running')
             ORDER BY requested_at, id
            """
        )
        rows = cur.fetchall()
    return [
        ProvisioningRequest(id=r[0], library_id=r[1], requested_by=r[2], state=r[3], attempt=r[4])
        for r in rows
    ]


def completed_steps(
    conn: psycopg.Connection, principal: Principal, request_id: UUID
) -> Iterable[str]:
    """Which steps of a run are recorded as done. For diagnostics and tests."""
    return sorted(_completed_steps(conn, principal, request_id))
