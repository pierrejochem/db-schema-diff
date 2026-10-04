"""The connection parts on a source, and what the configuration file may hold.

These fields exist because the desktop application asks for a host, a port, a database and a user
rather than for the name of an environment variable holding a connection string. They are all
non-secret, which is why they may be in a file that is committed; the password that goes with them
is not here and never is.

The command-line tool still connects through ``dsn_env`` alone, so these fields do not change what
a CI run does. What they must do is round-trip through the file and refuse a value libpq would
reject later, where the error would be about a connection rather than about a configuration.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from db_schema_comparer.config.model import SSL_MODES, SourceRef


def source(**overrides) -> SourceRef:
    parts = {"label": "qa", "dsn_env": "QA_DSN"}
    parts.update(overrides)
    return SourceRef(**parts)


class TestTheConnectionParts:
    def test_every_part_is_optional(self):
        """A configuration written by hand names a variable and nothing else, and still works."""
        ref = source()
        assert (ref.host, ref.port, ref.database, ref.user, ref.sslmode) == (None,) * 5

    def test_they_round_trip(self):
        ref = source(
            host="db-qa.internal", port=6432, database="invoicing", user="cumo", sslmode="require"
        )
        assert ref.model_dump(exclude_unset=True) == {
            "label": "qa",
            "dsn_env": "QA_DSN",
            "host": "db-qa.internal",
            "port": 6432,
            "database": "invoicing",
            "user": "cumo",
            "sslmode": "require",
        }

    @pytest.mark.parametrize("port", [0, -1, 65536, 100000])
    def test_a_port_outside_the_range_is_refused(self, port):
        with pytest.raises(ValidationError):
            source(port=port)

    @pytest.mark.parametrize("port", [1, 5432, 65535])
    def test_a_port_inside_the_range_is_kept(self, port):
        assert source(port=port).port == port

    def test_a_port_that_is_not_a_number_is_refused(self):
        with pytest.raises(ValidationError):
            source(port="six thousand")


class TestSslMode:
    @pytest.mark.parametrize("mode", SSL_MODES)
    def test_every_mode_libpq_knows_is_accepted(self, mode):
        assert source(sslmode=mode).sslmode == mode

    def test_the_list_is_libpq_s_own(self):
        assert SSL_MODES == (
            "disable",
            "allow",
            "prefer",
            "require",
            "verify-ca",
            "verify-full",
        )

    @pytest.mark.parametrize("mode", ["verify", "full", "VERIFY-FULL", "on", "true", "yes"])
    def test_a_mode_libpq_does_not_know_is_refused_here_rather_than_at_connect(self, mode):
        """``sslmode: verify`` is a plausible typo, and libpq reports it as a connection failure —
        which sends people to the host and the firewall rather than to the line they typed."""
        with pytest.raises(ValidationError, match="sslmode must be one of"):
            source(sslmode=mode)

    @pytest.mark.parametrize("value", ["", "   ", None])
    def test_nothing_chosen_means_libpq_s_own_default(self, value):
        """The desktop application's combo box has an empty first entry, and an empty string is
        not a mode: libpq rejects ``sslmode=``."""
        assert source(sslmode=value).sslmode is None

    def test_the_error_names_the_modes_rather_than_leaving_them_to_be_guessed(self):
        with pytest.raises(ValidationError) as caught:
            source(sslmode="verify")
        for mode in SSL_MODES:
            assert mode in str(caught.value)
