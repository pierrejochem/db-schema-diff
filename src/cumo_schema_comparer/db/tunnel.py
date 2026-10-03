"""An SSH tunnel to a database that is not directly routable.

CUMO's real databases sit behind bastion hosts. Rather than make everyone run ``ssh -L`` by hand and
then write a DSN pointing at whichever local port they happened to pick, a source can name a gateway
and this opens the forward for exactly as long as the connection it serves.

**The database is not named here.** It is already in the DSN, written as the *gateway* sees it,
so the same DSN is correct with or without a tunnel and no local port ever reaches a config file.

**Host keys: trust on first use.** An unknown gateway is pinned on first contact and written to the
host key store; a gateway whose key has *changed* is refused outright. Both halves come from
paramiko — ``load_host_keys`` makes the store writable, ``AutoAddPolicy`` pins the unknown, and
the changed-key check runs before the policy is consulted, so it cannot be policy'd away. The second
half is the one that matters: pinning without refusing a change would be theatre. Note that the
first connection to a new gateway is unauthenticated, so make it on a network you trust.

**Keys and passphrases.** The agent is tried first. A key file is used when the config names
one, and its passphrase comes from the environment or the keychain like every other secret here —
never from the YAML.
"""

from __future__ import annotations

import logging
import select
import socket
import socketserver
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..config.model import SshRef
from ..config.secrets import Secret
from ..errors import ConfigError, ConnectionFailed

log = logging.getLogger(__name__)

#: Where host keys live when the config does not say.
DEFAULT_KNOWN_HOSTS = "~/.ssh/known_hosts"

#: Bound separately from the database's own connect timeout: a gateway that is down should fail
#: here, quickly, rather than inside libpq against a local port that will never answer.
CONNECT_TIMEOUT_SECONDS = 15

#: Copy buffer. Catalog queries return small rows in bulk, so this is about throughput, not latency.
_CHUNK = 32 * 1024


def _paramiko() -> Any:
    """The paramiko module, or a config error naming the extra that provides it.

    An ``ImportError`` traceback would be a poor way to learn that a config needs an optional
    dependency, and this is the only place that needs it.
    """
    try:
        import paramiko
    except ImportError:
        raise ConfigError(
            "a source in this configuration is reached through an SSH tunnel, which needs the "
            "optional 'ssh' extra: pip install 'cumo-db-schema-comparer[ssh]'"
        ) from None
    return paramiko


def available() -> bool:
    """Whether the optional extra that provides tunnelling is installed."""
    try:
        import paramiko  # noqa: F401
    except ImportError:
        return False
    return True


def describe(ssh: SshRef) -> str:
    """The gateway, in a form safe to show anywhere.

    Deliberately not ``user@host``: the GUI redacts any token containing ``@`` or ``://`` on its way
    to a property, so the usual spelling would reach the status line as ``***`` and say nothing.
    """
    who = f" as {ssh.user}" if ssh.user else ""
    return f"gateway {ssh.host}:{ssh.port}{who}"


class _Forwarder(socketserver.ThreadingTCPServer):
    """A local listener whose every accepted socket becomes a channel through the gateway."""

    daemon_threads = True
    allow_reuse_address = True


def _pump(sock: socket.socket, channel: Any) -> None:
    """Copy bytes both ways until either end stops talking."""
    while True:
        readable, _, _ = select.select([sock, channel], [], [])
        if sock in readable:
            data = sock.recv(_CHUNK)
            if not data:
                return
            channel.sendall(data)
        if channel in readable:
            data = channel.recv(_CHUNK)
            if not data:
                return
            sock.sendall(data)


def _handler(transport: Any, to_host: str, to_port: int) -> type[socketserver.BaseRequestHandler]:
    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            try:
                channel = transport.open_channel(
                    "direct-tcpip", (to_host, to_port), self.request.getpeername()
                )
            except Exception as exc:
                # Type only: a gateway's refusal text is not under this tool's redaction.
                log.warning("tunnel: the gateway refused a channel (%s)", type(exc).__name__)
                return
            if channel is None:
                log.warning("tunnel: the gateway refused a channel")
                return
            try:
                _pump(self.request, channel)
            except OSError:
                # A connection dropped mid-copy is ordinary; the database layer will report it.
                pass
            finally:
                channel.close()

    return Handler


def probe(
    ssh: SshRef,
    *,
    to_host: str | None = None,
    to_port: int | None = None,
    passphrase: Secret | None = None,
    client_factory: Callable[[], Any] | None = None,
) -> str:
    """Try everything the tunnel needs, without touching the database. Returns what it proved.

    With a target, this asks the gateway to open a channel to it. That matters because opening a
    forward proves nothing on its own: the local listener accepts whatever connects to it, and the
    channel is only opened when something does. Asking directly exercises the whole chain — host
    key, authentication, whether forwarding is permitted, and whether the gateway reaches the
    database — and is the difference between "it let me in" and "this will work".

    Without one, the gateway is still worth testing on its own: somebody filling in these fields has
    usually not set the database credential yet, and needing it to check an ssh key would make the
    two checks one again.
    """
    paramiko = _paramiko()
    with _connected(ssh, passphrase, paramiko, client_factory) as client:
        if to_host is None or to_port is None:
            return f"{describe(ssh)}: reached and authenticated"
        transport = client.get_transport()
        if transport is None:  # pragma: no cover - paramiko sets this on a successful connect
            raise ConnectionFailed(f"{describe(ssh)}: connected but gave no transport")
        try:
            channel = transport.open_channel("direct-tcpip", (to_host, to_port), ("127.0.0.1", 0))
        except Exception as exc:
            raise ConnectionFailed(
                f"{describe(ssh)}: connected, but it would not open a channel to "
                f"{to_host}:{to_port} ({type(exc).__name__}). The gateway may forbid forwarding, "
                "or may not reach the database itself."
            ) from None
        channel.close()
    return f"{describe(ssh)}: reached {to_host}:{to_port}"


@contextmanager
def open_tunnel(
    ssh: SshRef,
    *,
    to_host: str,
    to_port: int,
    passphrase: Secret | None = None,
    client_factory: Callable[[], Any] | None = None,
) -> Iterator[tuple[str, int]]:
    """Forward a local port to ``to_host:to_port`` through ``ssh``, for the life of the block.

    Yields the local address to connect to. The port is chosen by the OS, so two captures running
    in parallel cannot collide on it.
    """
    paramiko = _paramiko()
    with _connected(ssh, passphrase, paramiko, client_factory) as client:
        transport = client.get_transport()
        if transport is None:  # pragma: no cover - paramiko sets this on a successful connect
            raise ConnectionFailed(f"{describe(ssh)}: connected but gave no transport")
        yield from _forwarding(transport, ssh, to_host, to_port)


@contextmanager
def _connected(
    ssh: SshRef,
    passphrase: Secret | None,
    paramiko: Any,
    client_factory: Callable[[], Any] | None = None,
) -> Iterator[Any]:
    """An authenticated client for ``ssh``, closed on the way out.

    Shared by the forward and by :func:`probe`, so that what the button tests is the same sequence
    the connection performs — a check that authenticates differently from the real thing is worse
    than no check.
    """
    client = (client_factory or paramiko.SSHClient)()

    known_hosts = Path(ssh.known_hosts or DEFAULT_KNOWN_HOSTS).expanduser()
    # load_host_keys raises on a missing file, and it is also what makes the store writable, so a
    # first-ever connection has somewhere to pin to.
    known_hosts.parent.mkdir(parents=True, exist_ok=True)
    known_hosts.touch(exist_ok=True)
    client.load_host_keys(str(known_hosts))
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        client.connect(
            hostname=ssh.host,
            port=ssh.port,
            username=ssh.user,
            key_filename=str(Path(ssh.private_key).expanduser()) if ssh.private_key else None,
            passphrase=passphrase.value if passphrase is not None else None,
            allow_agent=True,
            # Only fall back to ssh's default key names when the config named none.
            look_for_keys=ssh.private_key is None,
            timeout=CONNECT_TIMEOUT_SECONDS,
        )
    except paramiko.BadHostKeyException:
        client.close()
        raise ConnectionFailed(
            f"{describe(ssh)}: the host key has changed since it was first trusted. "
            f"If this was expected, remove the old key from {known_hosts}; "
            "if it was not, do not connect."
        ) from None
    except paramiko.AuthenticationException:
        client.close()
        raise ConnectionFailed(
            f"{describe(ssh)}: authentication was refused. "
            "Load the key into your ssh agent, or name it with 'private_key'."
        ) from None
    except Exception as exc:
        client.close()
        # Type only, for the same reason the keychain layer reports types: a gateway's own text is
        # not under this tool's redaction.
        raise ConnectionFailed(f"{describe(ssh)}: not reachable ({type(exc).__name__})") from None

    try:
        yield client
    finally:
        client.close()


def _forwarding(
    transport: Any, ssh: SshRef, to_host: str, to_port: int
) -> Iterator[tuple[str, int]]:
    """The local listener, for as long as the caller needs it."""
    server = _Forwarder(("127.0.0.1", 0), _handler(transport, to_host, to_port))
    thread = threading.Thread(target=server.serve_forever, name="ssh-tunnel", daemon=True)
    thread.start()
    local_host, local_port = server.server_address[0], server.server_address[1]
    log.debug("tunnel open on 127.0.0.1:%s via %s", local_port, ssh.host)
    try:
        yield str(local_host), int(local_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
