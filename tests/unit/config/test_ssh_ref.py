"""The ssh block on a source, and the passphrase that goes with it.

The rule this file exists to defend: a config file is committed, so it may name a key *path* and a
variable *name*, and never a key or a passphrase.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from db_schema_diff.config.model import ComparerConfig, SourceRef, SshRef
from db_schema_diff.config.secrets import Secret
from db_schema_diff.errors import MissingCredentialsError


class TestSshRef:
    def test_a_gateway_needs_only_a_host(self):
        ref = SshRef(host="bastion.internal")
        assert (ref.port, ref.user, ref.private_key, ref.known_hosts) == (22, None, None, None)

    def test_the_host_is_required(self):
        # An ssh block with no host is a typo, not a request for a default.
        with pytest.raises(ValidationError):
            SshRef()

    @pytest.mark.parametrize("host", ["", "   ", "two words"])
    def test_a_host_that_is_not_a_host_is_refused(self, host):
        with pytest.raises(ValidationError):
            SshRef(host=host)

    @pytest.mark.parametrize("port", [0, -1, 70000])
    def test_a_port_outside_the_range_is_refused(self, port):
        with pytest.raises(ValidationError):
            SshRef(host="h", port=port)

    @pytest.mark.parametrize("field", ["private_key", "known_hosts"])
    def test_a_cleared_path_means_unset_rather_than_an_empty_path(self, field):
        """The GUI sends "" for a field someone emptied, and "" is not a path."""
        assert getattr(SshRef(host="h", **{field: ""}), field) is None

    def test_an_unknown_key_is_refused(self):
        # extra="forbid": a misspelled key must fail rather than be silently ignored.
        with pytest.raises(ValidationError):
            SshRef(host="h", privatekey="~/.ssh/id_ed25519")

    def test_a_source_connects_directly_unless_it_says_otherwise(self):
        assert SourceRef(label="qa", dsn_env="QA_DSN").ssh is None


def config_with_ssh(**ssh: object) -> ComparerConfig:
    return ComparerConfig(
        version=1,
        name="invoicing",
        master=SourceRef(label="prod", dsn_env="PROD_DSN"),
        targets=(SourceRef(label="qa", dsn_env="QA_DSN", ssh=SshRef(host="b", **ssh)),),
    )


class TestPassphraseResolution:
    def test_a_missing_passphrase_is_reported_beside_a_missing_dsn(self, monkeypatch):
        """One run, one list of everything that is missing.

        Reporting the DSN first and the passphrase on the next run turns one mistake into two runs,
        which is the whole reason `resolve_all` aggregates.
        """
        monkeypatch.delenv("PROD_DSN", raising=False)
        monkeypatch.setenv("QA_DSN", "postgresql://u@h/db")
        monkeypatch.delenv("QA_SSH_PASSPHRASE", raising=False)
        config = config_with_ssh(passphrase_env="QA_SSH_PASSPHRASE")

        with pytest.raises(MissingCredentialsError) as raised:
            config.resolve_credentials()

        message = str(raised.value)
        assert "PROD_DSN" in message
        assert "QA_SSH_PASSPHRASE" in message

    def test_a_resolved_passphrase_is_a_self_redacting_secret(self, monkeypatch):
        monkeypatch.setenv("QA_SSH_PASSPHRASE", "hunter2")
        config = config_with_ssh(passphrase_env="QA_SSH_PASSPHRASE")

        passphrase = config.resolve_ssh_passphrases()["qa"]

        assert isinstance(passphrase, Secret)
        assert passphrase.value == "hunter2"
        for rendered in (repr(passphrase), str(passphrase), f"{passphrase}", f"{passphrase!r}"):
            assert "hunter2" not in rendered

    def test_a_gateway_without_a_passphrase_needs_no_variable(self, monkeypatch):
        monkeypatch.setenv("PROD_DSN", "postgresql://u@h/db")
        monkeypatch.setenv("QA_DSN", "postgresql://u@h/db")
        config = config_with_ssh()
        assert config.resolve_ssh_passphrases() == {}
        assert set(config.resolve_credentials()) == {"prod", "qa"}

    def test_the_role_names_the_source_so_the_message_points_somewhere(self, monkeypatch):
        monkeypatch.setenv("PROD_DSN", "postgresql://u@h/db")
        monkeypatch.setenv("QA_DSN", "postgresql://u@h/db")
        monkeypatch.delenv("QA_SSH_PASSPHRASE", raising=False)
        config = config_with_ssh(passphrase_env="QA_SSH_PASSPHRASE")

        with pytest.raises(MissingCredentialsError) as raised:
            config.resolve_credentials()

        assert "qa" in str(raised.value)
        assert "ssh key" in str(raised.value)
