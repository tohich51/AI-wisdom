"""Privileged OpenViking adapter, exclusively for the separate indexer."""

import os
import tempfile
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

import httpx
from openviking_sdk import SyncHTTPClient
from openviking_sdk.errors import AlreadyExistsError

from kb.adapters.secrets import _reject_symlinks
from kb.retrieval.embedding_profile import SELECTED_PROFILE
from kb.retrieval.provisioning import (
    IssuedKey,
    ProvisioningRefused,
    ProvisioningUnavailable,
    RemoteAccount,
    build_account_spec,
)


def safe_operation(method):
    @wraps(method)
    def run(self, spec, *args):
        if spec != build_account_spec(
            spec.library_id,
            dimension=SELECTED_PROFILE.dimension,
            embedding_profile=SELECTED_PROFILE.name,
        ):
            raise ProvisioningRefused("projection_profile_mismatch")
        try:
            return method(self, spec, *args)
        except ProvisioningRefused:
            raise
        except Exception:
            raise ProvisioningUnavailable("openviking_admin_unavailable") from None

    return run


class OpenVikingAdmin:
    def __init__(self, url: str, root_key_file: Path, bootstrap_dir: Path):
        self.url, self.bootstrap_dir = url, bootstrap_dir
        _reject_symlinks(root_key_file)
        _reject_symlinks(bootstrap_dir)
        bootstrap_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(bootstrap_dir, 0o700)
        key = root_key_file.read_text().strip()
        self.root = SyncHTTPClient(url=url, api_key=key, timeout=300)
        self.root.initialize()
        self.http = httpx.Client(base_url=url, headers={"X-API-Key": key}, timeout=300)

    def close(self):
        self.http.close()
        self.root.close()

    def _path(self, spec):
        path = self.bootstrap_dir / (spec.account_ref + ".key")
        _reject_symlinks(path)
        return path

    @safe_operation
    def create_account(self, spec):
        bootstrap = "kb-bootstrap-" + spec.library_id.hex
        response = self.http.post(
            "/api/v1/admin/accounts",
            json={
                "account_id": spec.account_ref,
                "admin_user_id": bootstrap,
                "settings": {"acl": {"enabled": True}},
            },
        )
        payload = response.json()
        created = response.status_code == 200
        path = self._path(spec)
        if created:
            key = payload["result"]["user_key"]
        elif payload.get("error", {}).get("code") == "ALREADY_EXISTS":
            key = (
                None
                if path.is_file()
                else self.root.admin_regenerate_key(spec.account_ref, bootstrap)["user_key"]
            )
        else:
            raise ProvisioningUnavailable("openviking_account_unavailable")
        if key:
            descriptor, name = tempfile.mkstemp(dir=self.bootstrap_dir)
            try:
                with os.fdopen(descriptor, "w") as stream:
                    stream.write(key)
                _reject_symlinks(path)
                os.replace(name, path)
            finally:
                Path(name).unlink(missing_ok=True)
        return RemoteAccount(spec.account_ref, created)

    @safe_operation
    def ensure_service_identity(self, spec, identity):
        if identity not in (spec.read_identity, spec.index_identity):
            raise ProvisioningRefused("invalid_service_identity")
        try:
            self.root.admin_register_user(spec.account_ref, identity, role="user")
        except AlreadyExistsError:
            pass

    @safe_operation
    def issue_service_key(self, spec, identity):
        if identity not in (spec.read_identity, spec.index_identity):
            raise ProvisioningRefused("invalid_service_identity")
        return IssuedKey(
            identity, self.root.admin_regenerate_key(spec.account_ref, identity)["user_key"]
        )

    @contextmanager
    def _admin(self, spec):
        path = self._path(spec)
        if path.stat().st_mode & 0o077:
            raise ProvisioningRefused("private_key_permissions")
        client = SyncHTTPClient(url=self.url, api_key=path.read_text().strip(), timeout=300)
        try:
            client.initialize()
            yield client
        finally:
            client.close()

    @staticmethod
    def _entries(spec):
        return [
            {"principal": "user:" + spec.read_identity, "level": "read"},
            {"principal": "user:" + spec.index_identity, "level": "write"},
        ]

    def _verify(self, spec, client):
        acl = client.acl_get("viking://resources" + spec.root_path)
        actual = {(e["principal"], e["level"]) for e in acl["effective_entries"]}
        expected = {(e["principal"], e["level"]) for e in self._entries(spec)}
        if acl.get("acl_mode") != "restricted" or actual != expected:
            raise ProvisioningRefused("openviking_acl_verification_failed")

    @safe_operation
    def apply_acl(self, spec):
        uri = "viking://resources" + spec.root_path
        with self._admin(spec) as client:
            try:
                client.mkdir(
                    uri,
                    description="Knowledge library boundary",
                    acl={"acl_mode": "restricted", "entries": self._entries(spec)},
                )
            except AlreadyExistsError:
                client.acl_set(uri, self._entries(spec), acl_mode="restricted")
            client.wait_processed(timeout=300)
            self._verify(spec, client)

    @safe_operation
    def describe_account(self, spec):
        settings = self.root.admin_get_account_settings(spec.account_ref)
        if settings.get("settings", {}).get("acl", {}).get("enabled") is not True:
            raise ProvisioningRefused("openviking_account_acl_disabled")
        with self._admin(spec) as client:
            self._verify(spec, client)
        return {"acl_entries": 2}
