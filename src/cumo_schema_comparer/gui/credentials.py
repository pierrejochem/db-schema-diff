"""Credential resolution for the application.

Two rules govern everything here.

**Nothing raw crosses into the UI.** ``resolve`` returns the library's ``Dsn``, which redacts itself
in every string context, and ``describe`` returns only a host/port/database/user summary. No
connection string reaches a Slint property, a log line or a rendered string.

**The keychain wins over the environment** — inside this application only. The CLI keeps reading the
environment and nothing else, so a credential stored on one machine can never silently override what
a pipeline exported and make it compare the wrong database.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..config.secrets import Dsn, format_missing
from ..errors import MissingCredentialsError

#: Keychain service name. Entries are (KEYCHAIN_SERVICE, <dsn_env name>).
KEYCHAIN_SERVICE = "cumo-schema-diff"


class CredentialSource(StrEnum):
    """Where a credential's value came from."""

    KEYCHAIN = "keychain"
    ENVIRONMENT = "environment"
    UNSET = "unset"


@dataclass(frozen=True, slots=True)
class CredentialStatus:
    """What the UI may show about a credential. Safe to render whole."""

    env_name: str
    source: CredentialSource
    summary: str | None
    """Host, port, database and user. Never a password."""
    storage_available: bool


class CredentialStore:
    """Resolves credentials, keychain first, and stores them there on request."""

    def __init__(
        self,
        backend: Any | None = None,
        environ: Mapping[str, str] | None = None,
        *,
        _import_keyring: bool = True,
    ) -> None:
        self._environ = dict(os.environ if environ is None else environ)
        self._problem: str | None = None
        self._probed = False
        self._backend = backend if backend is not None else self._load_backend(_import_keyring)

    def _load_backend(self, allowed: bool) -> Any | None:
        if not allowed:
            self._problem = "keyring is not installed"
            return None
        try:
            import keyring

            return keyring.get_keyring()
        except Exception as exc:  # pragma: no cover - platform dependent
            self._problem = f"no keychain available: {exc}"
            return None

    @property
    def storage_available(self) -> bool:
        """Whether credentials can be stored. False on a machine with no usable keychain."""
        self._probe()
        return self._backend is not None and self._problem is None

    @property
    def storage_problem(self) -> str | None:
        """Why storage is unavailable, for the UI to explain rather than fail silently."""
        self._probe()
        return self._problem

    def _probe(self) -> None:
        """Touch the backend once so a refusal is known before anything is read or stored.

        Without this, ``storage_available`` would report True until the first lookup happened to
        fail. Reading an absent entry does not prompt on macOS.
        """
        if self._probed or self._backend is None:
            return
        self._probed = True
        self._from_keychain("__availability_probe__")

    def describe(self, env_name: str) -> CredentialStatus:
        """Where this credential would come from, and a summary safe to display."""
        value, source = self._lookup(env_name)
        summary = None
        if value is not None:
            parts = Dsn(value, env_name=env_name).safe_summary()
            summary = ", ".join(f"{k}={v}" for k, v in sorted(parts.items()) if k != "source")
        return CredentialStatus(
            env_name=env_name,
            source=source,
            summary=summary,
            storage_available=self.storage_available,
        )

    def resolve(self, env_name: str) -> Dsn:
        """The credential to connect with, keychain first."""
        value, source = self._lookup(env_name)
        if value is None or source is CredentialSource.UNSET:
            raise MissingCredentialsError(format_missing([(env_name, "this source")]))
        return Dsn(value, env_name=env_name)

    def store(self, env_name: str, dsn: str) -> None:
        """Put a credential in the keychain. Never writes to a config file."""
        if not dsn.strip():
            raise ValueError("refusing to store an empty credential")
        backend = self._require_storage()
        try:
            backend.set_password(KEYCHAIN_SERVICE, env_name, dsn.strip())
        except Exception as exc:
            self._problem = f"keychain unavailable: {exc}"
            raise RuntimeError(self._problem) from exc

    def forget(self, env_name: str) -> None:
        """Remove a credential from the keychain, leaving the environment untouched."""
        backend = self._require_storage()
        try:
            backend.delete_password(KEYCHAIN_SERVICE, env_name)
        except Exception as exc:
            self._problem = f"keychain unavailable: {exc}"
            raise RuntimeError(self._problem) from exc

    def _require_storage(self) -> Any:
        if not self.storage_available or self._backend is None:
            raise RuntimeError(self._problem or "no keychain available")
        return self._backend

    def _lookup(self, env_name: str) -> tuple[str | None, CredentialSource]:
        stored = self._from_keychain(env_name)
        if stored:
            return stored, CredentialSource.KEYCHAIN
        from_env = self._environ.get(env_name)
        if from_env and from_env.strip():
            return from_env.strip(), CredentialSource.ENVIRONMENT
        return None, CredentialSource.UNSET

    def _from_keychain(self, env_name: str) -> str | None:
        """Read from the keychain, tolerating a backend that refuses.

        macOS prompts for permission and the prompt can be denied; the application must keep working
        from the environment rather than crash, and must say that storage is unavailable.
        """
        if self._backend is None:
            return None
        try:
            value = self._backend.get_password(KEYCHAIN_SERVICE, env_name)
        except Exception as exc:
            self._problem = f"keychain unavailable: {exc}"
            return None
        if value is None:
            return None
        if not isinstance(value, str):
            self._problem = f"keychain returned an unexpected type ({type(value).__name__})"
            return None
        return value.strip() or None
