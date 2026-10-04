"""Assembling a connection string from typed parts.

The desktop application no longer asks for the name of an environment variable holding a DSN: it
asks for a host, a port, a database, a user and a password, and builds the connection string
itself. That makes this module the one place in the GUI that handles a password, so the tests here
are about the two things that go wrong when a connection string is built by concatenation — a
value that changes the meaning of the URI, and a value that escapes into a message.

``libpq``'s own parser is the oracle for the first: a string this module builds is parsed back with
``psycopg.conninfo`` and the parts are compared with what went in. A hand-written expectation would
only prove that the test and the code agree on the same mistake.
"""

from __future__ import annotations

import pytest
from psycopg.conninfo import conninfo_to_dict

from db_schema_diff.config.model import SourceRef
from db_schema_diff.config.secrets import Dsn
from db_schema_diff.gui import connection

#: Every character that means something to a URI parser, in one password. A password like this is
#: ordinary — a generated one frequently contains several — and each of these would otherwise end
#: the user, the host or the path early.
HOSTILE = "p@ss:w/rd?#[]&= x%2F"


def source(**overrides) -> SourceRef:
    parts = {
        "label": "qa",
        "dsn_env": "QA_DSN",
        "host": "db-qa.internal",
        "port": 6432,
        "database": "invoicing",
        "user": "app",
    }
    parts.update(overrides)
    return SourceRef(**parts)


class TestBuilding:
    def test_the_parts_come_back_out_as_they_went_in(self):
        built = connection.build(source(), "hunter2", env_name="QA_DSN")
        assert conninfo_to_dict(built.value) == {
            "user": "app",
            "password": "hunter2",
            "host": "db-qa.internal",
            "port": "6432",
            "dbname": "invoicing",
        }

    @pytest.mark.parametrize(
        "password",
        [
            HOSTILE,
            "p@ssword",
            "has/slash",
            "has:colon",
            "has#hash",
            "has?question",
            "has space",
            "ümlaut",
            "100%",
        ],
    )
    def test_a_password_full_of_uri_punctuation_survives_the_round_trip(self, password):
        """The failure this prevents is silent: the connection goes somewhere else, or the parse
        error quotes the password back."""
        built = connection.build(source(), password, env_name="QA_DSN")
        parsed = conninfo_to_dict(built.value)
        assert parsed["password"] == password
        assert parsed["host"] == "db-qa.internal", "an unescaped password rewrote the host"
        assert parsed["dbname"] == "invoicing"

    def test_a_user_and_database_with_punctuation_survive_too(self):
        built = connection.build(
            source(user="app@corp", database="in/voicing"), "pw", env_name="QA_DSN"
        )
        parsed = conninfo_to_dict(built.value)
        assert parsed["user"] == "app@corp"
        assert parsed["dbname"] == "in/voicing"

    def test_the_default_port_is_written_out_rather_than_left_to_libpq(self):
        built = connection.build(source(port=None), "pw", env_name="QA_DSN")
        assert conninfo_to_dict(built.value)["port"] == str(connection.DEFAULT_PORT)

    def test_an_ssl_mode_is_carried_as_a_parameter(self):
        built = connection.build(source(sslmode="verify-full"), "pw", env_name="QA_DSN")
        assert conninfo_to_dict(built.value)["sslmode"] == "verify-full"

    def test_no_ssl_mode_means_no_parameter_rather_than_an_empty_one(self):
        """``sslmode=`` is not the same as saying nothing: libpq rejects an empty value."""
        built = connection.build(source(), "pw", env_name="QA_DSN")
        assert "sslmode" not in conninfo_to_dict(built.value)
        assert "?" not in built.value

    def test_the_result_redacts_itself_everywhere_a_string_is_wanted(self):
        built = connection.build(source(), "hunter2", env_name="QA_DSN")
        assert isinstance(built, Dsn)
        for rendered in (str(built), repr(built), f"{built}", f"{built!s}", format(built, "s")):
            assert "hunter2" not in rendered
            assert rendered == "<Dsn from $QA_DSN>"

    def test_it_is_filed_under_the_name_it_was_given(self):
        built = connection.build(source(), "pw", env_name="DB_QA_DSN")
        assert built.env_name == "DB_QA_DSN"

    def test_an_empty_password_leaves_the_credentials_without_a_separator(self):
        """Not ``user:@host``: an empty password is different from no password, and libpq would
        read the first as a password that is the empty string."""
        built = connection.build(source(), "", env_name="QA_DSN")
        assert "password" not in conninfo_to_dict(built.value)
        assert built.value.startswith("postgresql://app@")


class TestWhatIsStillMissing:
    def test_a_source_with_nothing_filled_in_names_every_part(self):
        missing = connection.missing_parts(SourceRef(label="qa", dsn_env="Q"), has_password=False)
        assert missing.fields == ("host", "database", "user", "password")
        assert str(missing) == "host, database, user, password"
        assert bool(missing) is True

    def test_a_complete_source_is_missing_nothing(self):
        missing = connection.missing_parts(source(), has_password=True)
        assert missing.fields == ()
        assert str(missing) == ""
        assert bool(missing) is False

    def test_a_port_is_not_required_because_there_is_a_default(self):
        assert connection.missing_parts(source(port=None), has_password=True).fields == ()

    def test_whitespace_is_not_a_filled_in_field(self):
        missing = connection.missing_parts(source(host="   "), has_password=True)
        assert missing.fields == ("host",)

    def test_the_password_is_counted_without_being_looked_at(self):
        """Whether one is stored is a question for the keychain; its value has no business here."""
        assert connection.missing_parts(source(), has_password=False).fields == ("password",)


class TestTheGeneratedVariableName:
    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            ("qa", "DB_QA_DSN"),
            ("prod", "DB_PROD_DSN"),
            ("rmv-qa", "DB_RMV_QA_DSN"),
            ("staging 2", "DB_STAGING_2_DSN"),
            ("a.b.c", "DB_A_B_C_DSN"),
            ("--odd--", "DB_ODD_DSN"),
            ("", "DB_SOURCE_DSN"),
            ("!!!", "DB_SOURCE_DSN"),
            ("ümlaut", "DB_MLAUT_DSN"),
        ],
    )
    def test_a_label_becomes_an_environment_variable_name(self, label, expected):
        assert connection.variable_name(label) == expected

    @pytest.mark.parametrize(
        "label", ["qa", "rmv-qa", "staging 2", "a.b.c", "--odd--", "", "!!!", "ümlaut"]
    )
    def test_the_result_is_always_a_name_the_config_will_accept(self, label):
        """``config_vm`` refuses a ``dsn_env`` that is not a POSIX variable name, and nobody types
        this one — so a label nobody thought of must not produce a file that cannot be saved."""
        from db_schema_diff.gui.config_vm import _ENV_NAME

        assert _ENV_NAME.fullmatch(connection.variable_name(label))

    def test_the_same_label_always_gives_the_same_name(self):
        """A rename moves the keychain entry by recomputing the name, so it cannot be a guess."""
        assert connection.variable_name("qa") == connection.variable_name("qa")
