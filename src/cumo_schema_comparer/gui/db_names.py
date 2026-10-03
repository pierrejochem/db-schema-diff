"""Naming things that live in the database layer, without importing it.

`tests/unit/gui/test_import_boundary.py` forbids any module under `gui/` from importing
`cumo_schema_comparer.db`, and the gateway description lives there. Repeating the format here would
be two spellings of one thing, so it is asked for through the runner instead.
"""

from __future__ import annotations

from ..config.model import SshRef


def describe_gateway(ssh: SshRef) -> str:
    """The gateway, in the form the rest of the application already uses for it."""
    from ..runner import describe_gateway as describe

    return describe(ssh)
