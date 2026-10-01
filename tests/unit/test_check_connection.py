# tests/unit/test_check_connection.py
"""Checking one source's connection without capturing an inventory.

Used by both the CLI's `probe` command and the GUI's Check connection button, so it lives in the
library rather than in either caller.
"""

from __future__ import annotations

from unittest import mock

from cumo_schema_comparer.config.model import SourceRef
from cumo_schema_comparer.config.secrets import Dsn
from cumo_schema_comparer.errors import ConnectionFailed
from cumo_schema_comparer.runner import ConnectionStatus, check_connection

SOURCE = SourceRef(label="prod", dsn_env="PROD_DSN")
DSN = Dsn("postgresql://u:secret@h:5432/invoicing", env_name="PROD_DSN")


def test_a_failure_is_reported_as_a_result_not_raised():
    # The GUI shows one row per source; one unreachable host must not abort the others.
    with mock.patch(
        "cumo_schema_comparer.runner.open_connection",
        side_effect=ConnectionFailed("prod: cannot connect (host=h, port=5432)"),
    ):
        status = check_connection(SOURCE, DSN)
    assert isinstance(status, ConnectionStatus)
    assert status.ok is False
    assert status.label == "prod"
    assert "cannot connect" in (status.error or "")


def test_the_failure_text_never_contains_a_credential():
    with mock.patch(
        "cumo_schema_comparer.runner.open_connection",
        side_effect=ConnectionFailed("prod: cannot connect (host=h, port=5432, source=$PROD_DSN)"),
    ):
        status = check_connection(SOURCE, DSN)
    assert "secret" not in (status.error or "")
    assert "postgresql://" not in (status.error or "")


def test_a_successful_check_reports_the_server_and_schemas():
    introspector = mock.MagicMock()
    introspector.server_info.return_value = {
        "server_version": "15.19 (Debian)",
        "database": "invoicing",
        "user": "cumo",
        "datcollate": "de_DE.utf8",
        "encoding": "UTF8",
    }
    introspector.schemas.return_value = ["cumo-invoicing", "public"]
    introspector.locate_changelog.return_value = []

    with (
        mock.patch("cumo_schema_comparer.runner.open_connection"),
        mock.patch("cumo_schema_comparer.runner.Introspector", return_value=introspector),
        mock.patch("cumo_schema_comparer.runner.server_features"),
    ):
        status = check_connection(SOURCE, DSN)

    assert status.ok is True
    assert status.server_version == "15.19 (Debian)"
    assert status.database == "invoicing"
    assert status.user == "cumo"
    assert status.collation == "de_DE.utf8"
    assert status.schemas == ("cumo-invoicing", "public")
    assert status.changelog is None
    assert status.encoding == "UTF8"


def test_a_located_changelog_is_reported_with_its_count_and_tag():
    from cumo_schema_comparer.model.changelog import ChangelogLocation, ChangelogState
    from tests.support.builders import changeset

    location = ChangelogLocation(schema="cumo-invoicing", table="DATABASECHANGELOG")
    introspector = mock.MagicMock()
    introspector.server_info.return_value = {
        "server_version": "15.19",
        "database": "invoicing",
        "user": "cumo",
        "datcollate": "C",
        "encoding": "UTF8",
    }
    introspector.schemas.return_value = ["cumo-invoicing"]
    introspector.locate_changelog.return_value = [location]
    introspector.changelog_state.return_value = ChangelogState(
        location=location,
        rows=(changeset("a", order=1, tag="R7.6.2"),),
        lock_held=False,
    )

    with (
        mock.patch("cumo_schema_comparer.runner.open_connection"),
        mock.patch("cumo_schema_comparer.runner.Introspector", return_value=introspector),
        mock.patch("cumo_schema_comparer.runner.server_features"),
    ):
        status = check_connection(SOURCE, DSN)

    assert status.changelog == "cumo-invoicing.DATABASECHANGELOG"
    assert status.changelog_count == 1
    assert status.changelog_tag == "R7.6.2"
    assert status.changelog_locked is False


def test_several_changelog_candidates_are_reported_as_ambiguous():
    # Guessing would make the answer depend on catalog ordering.
    from cumo_schema_comparer.model.changelog import ChangelogLocation

    introspector = mock.MagicMock()
    introspector.server_info.return_value = {
        "server_version": "15.19",
        "database": "invoicing",
        "user": "cumo",
        "datcollate": "C",
        "encoding": "UTF8",
    }
    introspector.schemas.return_value = ["public", "cumo-invoicing"]
    introspector.locate_changelog.return_value = [
        ChangelogLocation(schema="public", table="DATABASECHANGELOG"),
        ChangelogLocation(schema="cumo-invoicing", table="DATABASECHANGELOG"),
    ]

    with (
        mock.patch("cumo_schema_comparer.runner.open_connection"),
        mock.patch("cumo_schema_comparer.runner.Introspector", return_value=introspector),
        mock.patch("cumo_schema_comparer.runner.server_features"),
    ):
        status = check_connection(SOURCE, DSN)

    assert status.changelog is None
    assert status.changelog_candidates == (
        "public.DATABASECHANGELOG",
        "cumo-invoicing.DATABASECHANGELOG",
    )


def test_a_configured_changelog_location_is_used_instead_of_searching():
    from cumo_schema_comparer.model.changelog import ChangelogState

    source = SourceRef(
        label="qa",
        dsn_env="QA_DSN",
        liquibase={"schema": "cumo-invoicing", "table": "DATABASECHANGELOG"},
    )
    introspector = mock.MagicMock()
    introspector.server_info.return_value = {
        "server_version": "15.19",
        "database": "invoicing",
        "user": "cumo",
        "datcollate": "C",
        "encoding": "UTF8",
    }
    introspector.schemas.return_value = ["cumo-invoicing"]
    introspector.changelog_state.return_value = ChangelogState(location=None)

    with (
        mock.patch("cumo_schema_comparer.runner.open_connection"),
        mock.patch("cumo_schema_comparer.runner.Introspector", return_value=introspector),
        mock.patch("cumo_schema_comparer.runner.server_features"),
    ):
        check_connection(source, DSN)

    introspector.locate_changelog.assert_not_called()
    introspector.changelog_state.assert_called_once()
