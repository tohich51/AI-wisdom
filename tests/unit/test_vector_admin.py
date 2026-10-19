"""Regression for SDK filesystem conflict mapping and ACL fail-closed behavior."""

from contextlib import nullcontext
from uuid import uuid4

import pytest
from openviking_sdk.errors import AlreadyExistsError, ConflictError, PermissionDeniedError

from kb.retrieval.embedding_profile import SELECTED_PROFILE
from kb.retrieval.openviking_admin import OpenVikingAdmin
from kb.retrieval.provisioning import (
    ProvisioningRefused,
    ProvisioningUnavailable,
    build_account_spec,
)


class ExistingDirectory:
    def __init__(self, error, entries, *, broken=False):
        self.error, self.entries, self.broken = error, entries, broken
        self.repairs = []

    def mkdir(self, uri, **kwargs):
        raise self.error

    def acl_set(self, uri, entries, *, acl_mode):
        self.repairs.append((uri, entries, acl_mode))

    def wait_processed(self, *, timeout):
        return {}

    def acl_get(self, uri):
        return {"acl_mode": "restricted", "effective_entries": [] if self.broken else self.entries}


def _adapter(monkeypatch, error, *, broken=False):
    spec = build_account_spec(
        uuid4(), dimension=SELECTED_PROFILE.dimension, embedding_profile=SELECTED_PROFILE.name
    )
    adapter = object.__new__(OpenVikingAdmin)
    client = ExistingDirectory(error, adapter._entries(spec), broken=broken)
    monkeypatch.setattr(adapter, "_admin", lambda _spec: nullcontext(client))
    return adapter, client, spec


@pytest.mark.parametrize("error", [AlreadyExistsError("owned"), ConflictError("owned")])
def test_existing_directory_conflicts_reapply_and_verify_exact_acl(monkeypatch, error):
    adapter, client, spec = _adapter(monkeypatch, error)
    adapter.apply_acl(spec)
    assert client.repairs == [
        ("viking://resources" + spec.root_path, adapter._entries(spec), "restricted")
    ]


def test_conflict_never_accepts_a_mismatched_final_acl(monkeypatch):
    adapter, _client, spec = _adapter(monkeypatch, ConflictError("owned"), broken=True)
    with pytest.raises(ProvisioningRefused, match="openviking_acl_verification_failed"):
        adapter.apply_acl(spec)


def test_permission_denial_is_not_treated_as_an_existing_directory(monkeypatch):
    adapter, client, spec = _adapter(monkeypatch, PermissionDeniedError())
    with pytest.raises(ProvisioningUnavailable, match="openviking_admin_unavailable"):
        adapter.apply_acl(spec)
    assert client.repairs == []
