"""The SSH tunnel: how it is configured, and how the connection is pointed through it.

Nothing here opens a socket. What these tests defend is the handful of choices that are wrong in a
way no error message would reveal — a connection that silently stops verifying TLS, a host key
policy that trusts a key that changed, a missing optional dependency reported as an ImportError.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cumo_schema_comparer.config.model import SshRef
from cumo_schema_comparer.config.secrets import Dsn, Secret
from cumo_schema_comparer.db import tunnel
from cumo_schema_comparer.db.connect import ConnectionOptions, _build_conninfo, _tunnel_target
from cumo_schema_comparer.errors import ConfigError, ConnectionFailed

paramiko = pytest.importorskip("paramiko", reason="the ssh extra is not installed")

GATEWAY = SshRef(host="bastion.internal", port=2222, user="deploy")


class FakeClient:
    """Records what the tunnel asked of paramiko, and fails on demand."""

    def __init__(self, fail: BaseException | None = None) -> None:
        self.fail = fail
        self.loaded: str | None = None
        self.policy: Any = None
        self.connected: dict[str, Any] | None = None
        self.closed = False

    def load_host_keys(self, filename: str) -> None:
        self.loaded = filename

    def set_missing_host_key_policy(self, policy: Any) -> None:
        self.policy = policy

    def connect(self, **kwargs: Any) -> None:
        self.connected = kwargs
        if self.fail is not None:
            raise self.fail

    def get_transport(self) -> Any:  # pragma: no cover - only reached on the success path
        return None

    def close(self) -> None:
        self.closed = True


def run(ssh: SshRef, client: FakeClient, **kwargs: Any):
    with tunnel.open_tunnel(
        ssh, to_host="db.internal", to_port=5432, client_factory=lambda: client, **kwargs
    ):
        pass  # pragma: no cover - the fakes here never reach the body


class TestHostKeys:
    """Trust on first use is only worth anything if a *changed* key is refused."""

    def test_the_store_is_loaded_writable_so_a_new_host_can_be_pinned(self, tmp_path):
        client = FakeClient(fail=paramiko.AuthenticationException())
        store = tmp_path / "known_hosts"
        with pytest.raises(ConnectionFailed):
            run(SshRef(host="b", known_hosts=str(store)), client)
        # load_host_keys, not load_system_host_keys: only the former makes AutoAddPolicy persist
        # what it accepts, and a pin that is not written down is not a pin.
        assert client.loaded == str(store)
        assert isinstance(client.policy, paramiko.AutoAddPolicy)

    def test_a_missing_store_is_created_rather_than_failing_the_connection(self, tmp_path):
        store = tmp_path / "nested" / "known_hosts"
        with pytest.raises(ConnectionFailed):
            run(SshRef(host="b", known_hosts=str(store)), FakeClient(paramiko.SSHException()))
        assert store.is_file(), "paramiko raises on a missing host key file"

    def test_a_changed_host_key_is_refused_and_says_what_to_do(self, tmp_path):
        store = tmp_path / "known_hosts"
        failure = paramiko.BadHostKeyException("bastion.internal", object(), object())
        with pytest.raises(ConnectionFailed) as raised:
            run(SshRef(host="bastion.internal", known_hosts=str(store)), FakeClient(failure))

        message = str(raised.value)
        assert "has changed" in message
        assert str(store) in message
        assert "do not connect" in message

    def test_the_default_store_is_the_users_own(self):
        assert tunnel.DEFAULT_KNOWN_HOSTS == "~/.ssh/known_hosts"


class TestAuthentication:
    def test_the_agent_is_always_allowed(self, tmp_path):
        client = FakeClient(paramiko.AuthenticationException())
        with pytest.raises(ConnectionFailed):
            run(SshRef(host="b", known_hosts=str(tmp_path / "kh")), client)
        assert client.connected["allow_agent"] is True

    def test_a_named_key_stops_the_search_for_default_key_names(self, tmp_path):
        client = FakeClient(paramiko.AuthenticationException())
        ssh = SshRef(host="b", private_key="~/.ssh/id_ed25519", known_hosts=str(tmp_path / "kh"))
        with pytest.raises(ConnectionFailed):
            run(ssh, client)
        assert client.connected["key_filename"] == str(Path("~/.ssh/id_ed25519").expanduser())
        assert client.connected["look_for_keys"] is False

    def test_with_no_named_key_ssh_may_try_its_usual_names(self, tmp_path):
        client = FakeClient(paramiko.AuthenticationException())
        with pytest.raises(ConnectionFailed):
            run(SshRef(host="b", known_hosts=str(tmp_path / "kh")), client)
        assert client.connected["key_filename"] is None
        assert client.connected["look_for_keys"] is True

    def test_the_passphrase_is_handed_over_only_as_its_value(self, tmp_path):
        client = FakeClient(paramiko.AuthenticationException())
        with pytest.raises(ConnectionFailed):
            run(
                SshRef(host="b", known_hosts=str(tmp_path / "kh")),
                client,
                passphrase=Secret("hunter2", env_name="QA_SSH"),
            )
        assert client.connected["passphrase"] == "hunter2"

    def test_a_refusal_says_what_to_try_and_carries_no_key_material(self, tmp_path):
        client = FakeClient(paramiko.AuthenticationException("no acceptable key"))
        with pytest.raises(ConnectionFailed) as raised:
            run(
                SshRef(host="b", known_hosts=str(tmp_path / "kh")),
                client,
                passphrase=Secret("hunter2", env_name="QA_SSH"),
            )
        message = str(raised.value)
        assert "hunter2" not in message
        assert "agent" in message

    def test_an_unreachable_gateway_reports_the_type_and_not_its_words(self, tmp_path):
        """A gateway's own text is not under this tool's redaction, so it is never repeated."""
        client = FakeClient(OSError("connect to 10.0.0.1 failed: secret-looking detail"))
        with pytest.raises(ConnectionFailed) as raised:
            run(SshRef(host="b", known_hosts=str(tmp_path / "kh")), client)
        message = str(raised.value)
        assert "OSError" in message
        assert "secret-looking detail" not in message

    def test_every_failure_closes_the_client(self, tmp_path):
        client = FakeClient(paramiko.SSHException())
        with pytest.raises(ConnectionFailed):
            run(SshRef(host="b", known_hosts=str(tmp_path / "kh")), client)
        assert client.closed is True


class TestDescription:
    def test_the_gateway_is_named_without_an_at_sign(self):
        """The GUI redacts any token containing `@` or `://` on its way to a property.

        Spelled `deploy@bastion.internal`, the gateway would reach the status line as `***` and
        tell nobody anything.
        """
        described = tunnel.describe(GATEWAY)
        assert "@" not in described
        assert "bastion.internal" in described and "2222" in described and "deploy" in described

    def test_a_gateway_with_no_user_says_nothing_about_one(self):
        assert tunnel.describe(SshRef(host="b")) == "gateway b:22"


class TestMissingExtra:
    def test_a_config_that_needs_the_extra_says_so(self, monkeypatch):
        """An ImportError traceback is a poor way to learn that an extra is needed."""
        import builtins

        real = builtins.__import__

        def refuse(name, *args, **kwargs):
            if name == "paramiko":
                raise ImportError("no module named paramiko")
            return real(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", refuse)
        with pytest.raises(ConfigError) as raised:
            tunnel._paramiko()
        assert "ssh" in str(raised.value)
        assert "pip install" in str(raised.value)


class TestPointingTheConnectionThroughIt:
    """The half of this that would fail silently rather than loudly."""

    def dsn(self, value: str) -> Dsn:
        return Dsn(value, env_name="QA_DSN")

    def test_the_tunnel_goes_to_the_database_the_dsn_names(self):
        target = self.dsn("postgresql://u:p@db-prod.internal:6432/inv")
        assert _tunnel_target(target) == ("db-prod.internal", 6432)

    def test_a_dsn_without_a_port_means_the_postgres_default(self):
        assert _tunnel_target(self.dsn("postgresql://u@db-prod.internal/inv")) == (
            "db-prod.internal",
            5432,
        )

    @pytest.mark.parametrize(
        "value",
        ["postgresql://u@/inv", "postgresql://u@%2Fvar%2Frun%2Fpostgresql/inv"],
    )
    def test_a_socket_dsn_cannot_be_tunnelled_and_says_why(self, value):
        with pytest.raises(ConfigError) as raised:
            _tunnel_target(self.dsn(value))
        assert "TCP host" in str(raised.value)

    def test_the_dsn_is_not_quoted_back_when_it_cannot_be_tunnelled(self):
        with pytest.raises(ConfigError) as raised:
            _tunnel_target(self.dsn("postgresql://u:hunter2@/inv"))
        assert "hunter2" not in str(raised.value)

    def test_tls_verification_still_checks_the_real_hostname(self):
        """The subtle one, and the reason this is `hostaddr` rather than `host`.

        libpq connects to `hostaddr` but verifies the certificate against `host`. Rewriting `host`
        to 127.0.0.1 — the obvious implementation — turns `sslmode=verify-full` into a connection
        no certificate can satisfy, and the natural "fix" for that is to weaken sslmode.
        """
        built = _build_conninfo(
            self.dsn("postgresql://u:p@db-prod.internal:5432/inv?sslmode=verify-full"),
            ConnectionOptions(),
            "1.0",
            ("127.0.0.1", 54321),
        )
        assert "host=db-prod.internal" in built
        assert "hostaddr=127.0.0.1" in built
        assert "port=54321" in built
        assert "sslmode=verify-full" in built

    def test_without_a_tunnel_nothing_is_rewritten(self):
        built = _build_conninfo(
            self.dsn("postgresql://u:p@db-prod.internal:5432/inv"), ConnectionOptions(), "1.0"
        )
        assert "hostaddr" not in built
        assert "port=5432" in built
