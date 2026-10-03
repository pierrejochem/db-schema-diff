"""Wiring between the window and the view models.

Every callback the window can fire lands here, is translated into a view-model call, and has its
errors turned into a status message. Nothing in this module compares anything; it moves values
between a Slint property and a Python object, and starts coroutines on the event loop.

The session lifecycle is the sharp edge, so it is spelled out here.

``Session.compare``, ``Session.check_all`` and ``Session.check_connection`` are plain functions
returning a coroutine. They claim the session when they are **called**, which means:

* the call must happen on the event-loop thread. A Slint callback is invoked from the native loop
  while ``SlintEventLoop`` has set itself as the thread's running loop, so a handler here *is* on
  the loop thread and calls the session directly.
  ``asyncio.run_coroutine_threadsafe(session.compare(), loop)`` would be wrong: it evaluates
  ``session.compare()`` on the calling thread. It is never used here.
* "already running" arrives as a ``GuiError`` from the call, not from the first await, so it
  becomes a status message before any task exists.
* the coroutine it returns is handed straight to ``loop.create_task`` and therefore always
  awaited. Nothing between the call and the task creation can fail.

There is exactly one ``Session`` at a time. It is rebuilt only when the configuration it was built
from has changed *and* nothing is in flight, because the run lock is Session-scoped: two sessions
over one config could run two comparisons against the same production databases at once.

A cancelled or failed run never renders as success. ``verdict_level`` is set from
``ResultsModel.verdict`` for a finished report, to ``"cancelled"`` for a cancellation, and to the
empty string for a run that failed before producing a report — never from per-source progress,
which is history rather than a verdict.

No credential crosses into the window. Only ``Dsn.safe_summary()`` output (via
``CredentialStore.describe``) and variable *names* are shown, and every piece of free text from a
backend, a driver or the operating system passes through :func:`_sanitise` on the way to a
property.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import logging
import re
import sys
import webbrowser
from collections.abc import Callable, Coroutine, Mapping, Sequence
from pathlib import Path
from typing import Any

import slint

from .. import __version__
from ..config.loader import load_config_files
from ..config.model import (
    ComparerConfig,
    FailOn,
    IgnoreConfig,
    LiquibaseRef,
    SourceRef,
    SshRef,
)
from ..diff.changelog import ChangelogOptions
from ..diff.ignores import IgnoreRuleSet, load_default_ignores
from ..diff.model import ComparisonReport
from ..errors import ComparerError
from ..runner import ConnectionStatus, describe_gateway
from . import connection, dialogs, home
from .config_vm import ConfigDocument
from .credentials import CredentialSource, CredentialStatus, CredentialStore
from .errors import GuiError
from .ignores_vm import IgnoresDocument
from .results_vm import ResultsModel, checked_delta_rows, checked_diff_rows
from .session import (
    CAPTURE_PHASE,
    CAPTURE_ROWS,
    CHANGELOG_STEP,
    COMPARE_PHASE,
    REPORT_PHASE,
    SCHEMA_STEP,
    STEP_KINDS,
    ProgressEvent,
    RunDetail,
    Session,
    SourceState,
)

log = logging.getLogger(__name__)

UI = Path(__file__).parent / "ui" / "main.slint"

#: The rail's index for Results. The dialog's one forward button goes here, and the rail asserts
#: this mapping in `test_ui_smoke`.
RESULTS_VIEW = 2

#: A source's state, as the run dialog's heading row for it. ``CAPTURING`` has no entry: it is
#: the running state, which is this mapping's default.
_STEP_STATE = {
    SourceState.CAPTURED: "ok",
    SourceState.FAILED: "failed",
    SourceState.CANCELLED: "skipped",
    SourceState.IDLE: "pending",
}

#: The four values the config schema and the Run tab's combo box both accept.
FAIL_ON_CHOICES: tuple[str, ...] = ("error", "warning", "any", "never")

#: Config-level fields the Config tab edits through ``source_changed("", field, value)``.
#: The ssh widgets in the Config tab, and the ``SshRef`` field each one stands for.
_SSH_FIELDS = {
    "ssh_host": "host",
    "ssh_user": "user",
    "ssh_key": "private_key",
    "ssh_passphrase_env": "passphrase_env",
}

_OPTION_INTS = (
    "connect_timeout_seconds",
    "statement_timeout_seconds",
    "lock_timeout_seconds",
    "max_workers",
)
_OPTION_BOOLS = (
    "parallel",
    "ignore_column_order",
    "include_owners",
    "include_comments",
    "include_grants",
)

#: What the markup treats as a finished comparison, i.e. one with a report to write.
_REPORT_LEVELS = frozenset({"ok", "error", "warning", "info"})

CANCELLED_VERDICT = (
    "The comparison was cancelled, so there is no report. "
    "Nothing here describes the state of any database."
)
CANCELLED_STATUS = (
    "Cancelled. Captures already in flight were abandoned without being read; they can keep "
    "querying until their statement timeout expires."
)

_MAX_MESSAGE = 240
#: A libpq keyword's value, which is **not** simply "up to the next space". libpq lets a value be
#: single-quoted, and lets a backslash escape the next character inside or outside the quotes, so
#: ``password='a b'`` and ``password=a\ b`` both carry a space. Matching ``\S*`` redacted only the
#: first token of those and left the rest of the password in the message. Double quotes are
#: accepted too: libpq does not treat them specially, but a backend echoing a value may. An
#: unterminated quote is redacted to the end of the line, because a truncated echo must not be the
#: one shape that gets through — and so is an *unquoted* value carrying ``://`` or ``@``, for the
#: same reason: in free text there is no telling whether what follows the space is the rest of a
#: URL-shaped secret, and ``sslpassword=p://x rest-of-it`` left that tail behind when the value was
#: taken to end at the space. That branch's prefix is lazy and bounded, so the work stays linear in
#: the length of the token rather than backtracking over it.
_KEYWORD_VALUE = (
    r"(?:'(?:\\.|[^'\\])*'"  # 'single quoted', backslash escapes honoured
    r'|"(?:\\.|[^"\\])*"'  # "double quoted": libpq does not, but an echo may
    r"|['\"][^\n]*"  # an unterminated quote: to the end of the line
    r"|(?:\\.|[^\s\\]){0,256}?(?:://|@)[^\n]*"  # URL-shaped: to the end of the line
    r"|(?:\\.|[^\s\\])*)"  # plain, with backslash-escaped whitespace
)
#: A libpq secret keyword and its value. Only the keyword is matched by a regex, because only a
#: keyword's value can legitimately span a space and therefore needs a grammar.
_SECRET_KEYWORD = re.compile(
    rf"(?:password|passfile|sslpassword|sslkey)\s*=\s*{_KEYWORD_VALUE}", re.IGNORECASE
)

#: What makes a whitespace-delimited token credential-bearing on its own: a scheme, or userinfo.
_TOKEN_MARKERS = ("://", "@")


def _collapse(text: str) -> str:
    """``text`` as one line of single-spaced printable characters."""
    return " ".join("".join(c if c.isprintable() else " " for c in text).split())


def _bounded(text: str, limit: int = _MAX_MESSAGE) -> str:
    """``text`` cut to ``limit``: a Slint ``Text`` is not a scrollback."""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _one_line(text: str, limit: int = _MAX_MESSAGE) -> str:
    return _bounded(_collapse(text), limit)


def _sanitise(text: str) -> str:
    """Free text on its way to a property: one bounded line with no credential shape left in it.

    Used for everything this module did not write itself — keyring backend errors, driver errors,
    OS messages. Those are already redacted upstream where upstream knows the DSN; this is the
    layer that does not need to know it, so it recognises shapes rather than values.

    Two passes, and **the keyword pass must run first**. A password may contain ``@`` or ``://``
    — ``password='P@ss word'`` is ordinary — so the two kinds of shape overlap. Done the other way
    round, the token pass redacts ``password='P@ss`` and leaves ``word'`` behind, which is exactly
    the leak this ordering exists to prevent; ``test_the_keyword_pass_runs_first`` pins it.

    The second pass is a substring test over whitespace-delimited tokens rather than a regex.
    ``\\S*(?:://|@)\\S*`` was quadratic — the prefix could consume the message and then backtrack
    one character at a time, at every start offset, so a 200 000-character message took 103
    seconds, on the event-loop thread, freezing the window. A token scan is linear and has nothing
    to backtrack over at all. It redacts the whole token, which is the conservative direction.
    """
    tokens = _collapse(_SECRET_KEYWORD.sub("***", text)).split(" ")
    return _bounded(
        " ".join(
            "***" if any(marker in token for marker in _TOKEN_MARKERS) else token
            for token in tokens
        )
    )


def tunnelling_available() -> bool:
    """Whether SSH tunnelling can be used, for the About view to say so.

    Asked through the runner rather than imported here: `tests/unit/gui/test_import_boundary.py`
    forbids any module under `gui/` from importing `cumo_schema_comparer.db`, and the tunnel lives
    there.
    """
    from ..runner import tunnelling_supported

    return tunnelling_supported()


def _no_dialog(purpose: str, current: str) -> str | None:
    """The default path chooser: there is none.

    `run` replaces this with the real pickers. The default is inert so that constructing an
    Application never shows anything: a test that built one would otherwise open a real dialog and
    block until a person dismissed it, which is exactly how this was first written and caught.
    """
    return None


def _no_dialogs_available() -> bool:
    """Agrees with :func:`_no_dialog`: with no chooser installed, there is no dialog to offer."""
    return False


async def _as_list(coro: Coroutine[Any, Any, ConnectionStatus]) -> list[ConnectionStatus]:
    """One connection check with the shape of many, so both paths share a completion handler."""
    return [await coro]


class Application:
    """Owns the window, the documents and the current session."""

    def __init__(
        self,
        environ: Mapping[str, str] | None = None,
        keychain: Any | None = ...,
    ) -> None:
        self.window: Any = slint.load_file(str(UI)).MainWindow()
        self.credentials = (
            CredentialStore(environ=environ)
            if keychain is ...
            else CredentialStore(backend=keychain, environ=environ, _import_keyring=False)
        )
        self.config: ConfigDocument = ConfigDocument.blank()
        self.ignores: IgnoresDocument | None = None
        self.results: ResultsModel | None = None
        #: ``(purpose, current value) -> chosen path or None``. Defaults to no picker at all,
        #: and `run` installs the real one: an Application built in a test would otherwise open a
        #: Finder window and sit there until someone dismissed it.
        self.choose_path: Callable[[str, str], str | None] = _no_dialog
        #: Whether a picker exists here at all. Both a cancelled dialog and a machine with no
        #: dialog produce no path; only the second is worth telling someone to type it instead,
        #: so the two are told apart by this. Installed by `run` alongside `choose_path`, and the
        #: default must agree with the default chooser.
        self.path_dialogs_available: Callable[[], bool] = _no_dialogs_available
        #: Indirection so a test never launches a browser.
        self.open_url: Callable[[str], bool] = webbrowser.open
        self._session: Session | None = None
        self._session_config: ComparerConfig | None = None
        self._task: asyncio.Task[None] | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._checks: dict[str, ConnectionStatus] = {}
        #: The dialog's step rows, updated in place as the probe reports.
        self._tunnel_steps: Any = slint.ListModel([])
        #: Which tab each source card is showing, by label. Here rather than in the markup
        #: because every refresh replaces the row model and a card's own property would be
        #: rebuilt back to zero — cancelling a file dialog would jump you to another tab.
        self._source_tabs: dict[str, int] = {}
        self._described: dict[str, CredentialStatus] = {}
        #: The gate this module last wrote into the Run tab. The ComboBox there reports no
        #: change, so comparing against this is the only way to tell an untouched widget from a
        #: deliberate per-run choice.
        self._seeded_gate: str = str(self.window.run_fail_on)
        self._verdict: tuple[str, str] = ("", "")
        self._progress: Any = slint.ListModel([])
        self._progress_index: dict[str, int] = {}
        #: The run dialog's two models, and where each checklist row lives in the second. Rows are
        #: updated in place through this index: reassigning the model rebuilds every repeated
        #: element under it and would discard the scroll position halfway through a run.
        self._run_inputs: Any = slint.ListModel([])
        self._run_steps: Any = slint.ListModel([])
        #: ``(source label, step name)`` to row index. An empty label is a run-wide phase, and an
        #: empty step is the source's own heading row.
        self._run_step_index: dict[tuple[str, str], int] = {}
        #: How many objects each source's capture reported, so a capture that found none can be
        #: flagged. The checklist's own rows cannot be summed for this: a sub-row is a breakdown
        #: of its step, and adding both would count every view twice.
        self._run_found: dict[str, int] = {}
        #: Each source's last reported state, so its heading row can be redrawn when either the
        #: state or the count changes. The two arrive in no fixed order.
        self._run_state: dict[str, str] = {}
        #: What to say about the saved configuration, and whether it is a failure. Held rather
        #: than announced on the spot: `_refresh` runs last and would leave the toast empty.
        notice = self._reopen()
        self._adopt_documents()
        self._describe_itself()
        self._bind()
        self._refresh()
        if notice is not None:
            message, is_error = notice
            self._announce(message, is_error=is_error)

    # -- loading -----------------------------------------------------------------------------

    def _reopen(self) -> tuple[str, bool] | None:
        """Read back the one file this application saves.

        Returns ``(message, is_error)`` for the toast, or ``None`` when there is nothing to say.

        It saves to a single known path, so not reading that path back meant the only way to see
        yesterday's configuration was to open the YAML in an editor. There is still no Open
        button and no path on the command line: this is that one file, not a file chooser.

        A file that cannot be read must not stop the window opening — refusing to start would
        leave no way to recover from inside the application. The configuration stays blank, the
        reason goes to a toast, and saving replaces the bad file. The loader is the library's own,
        the one the command-line tool uses, so a file written here and a file written by hand are
        read exactly alike.
        """
        path = home.default_path()
        try:
            if not path.exists():
                return None
            if not path.is_file():
                # A directory where the file goes is somebody's stray mkdir. Saying so now beats
                # saying it at the end of the first session, when Save is the thing that fails.
                return f"{path.name} is not a file; starting from an empty configuration", True
            ((_, config),) = load_config_files([path])
        except Exception as exc:
            # Only the type for anything unexpected: a parse error quotes the text it choked on,
            # and somebody may have pasted a connection string into the file. `_announce`
            # redacts what it recognises, and a ComparerError is already written for a person.
            detail = str(exc) if isinstance(exc, ComparerError) else type(exc).__name__
            log.warning("cannot reopen %s (%s)", path.name, type(exc).__name__)
            return (
                f"{path.name} could not be read ({detail}); starting from an empty configuration",
                True,
            )
        self.config = ConfigDocument(path=path, config=config)
        if _has_comments(path):
            # Saving rewrites the file from the configuration rather than patching it, so a
            # hand-written comment does not survive the next Save. Said on the way in, while the
            # comments are still there to rescue — saying it afterwards would be an obituary.
            return (
                f"Loaded {path.name}. It has comments, which Save will not keep.",
                False,
            )
        return None

    def _adopt_documents(self) -> None:
        """Attach the ignore ruleset that belongs to the current configuration.

        An unreadable ruleset leaves an empty editable one behind, so the tab still renders while
        the status line explains the problem.
        """
        self.ignores = IgnoresDocument(path=None, config=IgnoreConfig())
        self.ignores = IgnoresDocument.for_config(self.config)

    def _describe_itself(self) -> None:
        """The About view's facts, read from what is actually installed.

        Written once at start-up rather than in `_refresh`: none of it can change while the window
        is open, and a version typed into markup is a version that will be wrong.
        """
        window = self.window
        window.about_version = __version__
        window.about_python = ".".join(str(part) for part in sys.version_info[:3])
        window.about_slint = str(getattr(slint, "__version__", "unknown"))
        # The whole path, not the folder: it is the one file this writes and reads back, and it is
        # the only thing to point an editor at when a configuration needs hand-editing.
        window.about_config_path = str(home.default_path())
        window.about_tunnelling = tunnelling_available()

    # -- the window's view of everything -----------------------------------------------------

    @staticmethod
    def _fill(current: Any, rows: list[dict[str, Any]]) -> Any | None:
        """Write ``rows`` into ``current``. Returns a new model only if one had to be built.

        Replacing a model rebuilds every repeated element under it, which destroys the one the
        person is using: because every edit ends in a refresh, a text field lost focus after a
        single character. Rows are therefore written into the existing model, and only a change in
        how many there are forces a rebuild — at which point nothing is being typed into anyway.
        """
        try:
            unchanged = len(current) == len(rows)
        except TypeError:  # pragma: no cover - a model that cannot be measured is rebuilt
            unchanged = False
        if not unchanged:
            return slint.ListModel(rows)
        for index, row in enumerate(rows):
            current[index] = row
        return None

    def _refresh(self) -> None:
        """Write the documents into the window.

        Every handler ends here, so the window and the documents cannot disagree. Deliberately
        does not touch ``running`` or the progress rows: those belong to the run in flight.
        """
        window = self.window
        config = self.config.config
        window.config_path = "" if self.config.path is None else str(self.config.path)
        window.dirty = self.config.dirty

        described = {s.dsn_env: self._describe(s.dsn_env) for s in config.sources}
        rebuilt = self._fill(
            window.sources, [self._source_row(s, described[s.dsn_env]) for s in config.sources]
        )
        if rebuilt is not None:
            window.sources = rebuilt
        window.exclude_schemas = slint.ListModel(list(config.exclude_schemas))
        # Both forms of the same value, but the text is left alone while it is being typed: the
        # document's tuple cannot represent a trailing comma or a half-typed name.
        if _split(str(window.exclude_schemas_text)) != tuple(config.exclude_schemas):
            window.exclude_schemas_text = ", ".join(config.exclude_schemas)

        options = config.options
        window.connect_timeout_seconds = options.connect_timeout_seconds
        window.statement_timeout_seconds = options.statement_timeout_seconds
        window.lock_timeout_seconds = options.lock_timeout_seconds
        window.parallel = options.parallel
        window.max_workers = options.max_workers
        window.fail_on = options.fail_on
        # The Run tab's gate is a per-run override of this one, and it defaults to "error" in the
        # markup. Left unseeded it silently replaced a configured `never` or `any` -- two visible
        # widgets disagreeing, with the untouched one winning, and the wrong fail_on written into
        # every report.
        #
        # `_seeded_gate` is what *this module* last wrote there, and only that. Re-reading the
        # window into it (`= str(window.run_fail_on)` unconditionally) adopted the user's own
        # choice as "ours", so the choice survived one refresh and was reverted on the next --
        # towards the config's gate, which fails open. Every finished run refreshes, so an
        # operator who chose `any` got `any`, `any`, then `never`. Now a value that differs from
        # what we wrote is a deliberate override: it is left alone, for good.
        if str(window.run_fail_on) == self._seeded_gate:
            window.run_fail_on = self._seeded_gate = options.fail_on
        window.ignore_column_order = options.ignore_column_order
        window.include_owners = options.include_owners
        window.include_comments = options.include_comments
        window.include_grants = options.include_grants

        ignores = self.ignores
        rebuilt = self._fill(
            window.rules,
            [] if ignores is None else [_rule_row(row) for row in ignores.rule_rows()],
        )
        if rebuilt is not None:
            window.rules = rebuilt
        rebuilt = self._fill(
            window.default_rules,
            [] if ignores is None else [_rule_row(row) for row in ignores.default_rows()],
        )
        if rebuilt is not None:
            window.default_rules = rebuilt
        window.case_insensitive_globs = (
            True if ignores is None else ignores.config.options.case_insensitive_globs
        )

        window.keychain_available = self.credentials.storage_available
        window.keychain_problem = _sanitise(self.credentials.storage_problem or "")

        self._refresh_results()

    def _describe(self, env_name: str) -> CredentialStatus:
        """Where ``env_name`` resolves from, looked up once per variable.

        Every handler refreshes the window, so without this a keystroke in the Config tab would
        read the OS keychain once per source. The environment side is already a snapshot taken by
        ``CredentialStore``, and the keychain side is invalidated by the two handlers that change
        it, so the cache cannot disagree with anything this application did.
        """
        cached = self._described.get(env_name)
        if cached is None:
            cached = self._described[env_name] = self.credentials.describe(env_name)
        return cached

    def _refresh_results(self) -> None:
        window = self.window
        level, sentence = self._verdict
        window.verdict_level = level
        window.verdict = sentence
        results = self.results
        if results is None:
            window.findings = slint.ListModel([])
            window.total_findings = 0
            self._clear_selection()
            return
        shown = results.finding_rows(needle=str(window.filter_text), severities=self._severities())
        window.findings = slint.ListModel([_finding_row(row) for row in shown])
        # The unfiltered count, so the tab can say how many rows the filter is hiding.
        window.total_findings = len(results.finding_rows())

    def _severities(self) -> frozenset[str] | None:
        chosen = {
            name
            for name, shown in (
                ("error", bool(self.window.show_error)),
                ("warning", bool(self.window.show_warning)),
                ("info", bool(self.window.show_info)),
            )
            if shown
        }
        # All three on is not a filter: "no filter" is what the view model wants, so a severity
        # the tab has no checkbox for can never be hidden by an untouched form.
        return None if len(chosen) == 3 else frozenset(chosen)

    def _source_row(self, source: SourceRef, credential: CredentialStatus) -> dict[str, Any]:
        """All seventeen ``SourceRow`` fields. Slint neither defaults nor rejects a partial row."""
        status = self._checks.get(source.label)
        ssh = source.ssh
        return {
            "label": source.label,
            "host": source.host or "",
            "port": "" if source.port is None else str(source.port),
            "database": source.database or "",
            "user": source.user or "",
            "sslmode": source.sslmode or "",
            "has_password": credential.source is not CredentialSource.UNSET,
            "missing": str(
                connection.missing_parts(
                    source, has_password=credential.source is not CredentialSource.UNSET
                )
            ),
            "schemas": ", ".join(source.schemas or ()),
            "schema_map": ", ".join(f"{k}={v}" for k, v in source.schema_map.items()),
            "liquibase_schema": "" if source.liquibase is None else source.liquibase.schema_name,
            "liquibase_table": "" if source.liquibase is None else source.liquibase.table,
            "ssh_host": "" if ssh is None else ssh.host,
            "ssh_user": "" if ssh is None or ssh.user is None else ssh.user,
            "ssh_key": "" if ssh is None or ssh.private_key is None else ssh.private_key,
            "ssh_passphrase_env": (
                "" if ssh is None or ssh.passphrase_env is None else ssh.passphrase_env
            ),
            "tab": self._source_tabs.get(source.label, 0),
            "is_master": source.label == self.config.config.master.label,
            "connection_status": "" if status is None else _describe_check(status),
            "connection_ok": False if status is None else status.ok,
            "checked": status is not None,
        }

    def _guard(self, fn: Callable[..., None]) -> Callable[..., None]:
        """Wrap a handler so an expected failure becomes a message and the window stays true.

        Anything unexpected is re-raised: a mis-wired binding should be visible, not swallowed.
        """

        def handler(*args: Any) -> None:
            try:
                fn(*args)
            except (GuiError, ComparerError) as exc:
                self._fail(exc)
            finally:
                self._refresh()

        return handler

    def _announce(self, message: str, *, is_error: bool) -> None:
        """Put one message in front of the person, as a toast.

        The token is what makes a repeat visible. Two failed checks of the same source produce the
        same text, and the toast is driven by property changes, so without a token the second would
        change nothing on screen and the run would look like it had stopped.
        """
        self.window.status_message = _sanitise(message)
        self.window.status_is_error = is_error
        self.window.status_token = int(self.window.status_token) + 1

    def _fail(self, exc: BaseException | str) -> None:
        self._announce(str(exc), is_error=True)

    def _ok(self, message: str) -> None:
        self._announce(message, is_error=False)

    def _clear_status(self) -> None:
        # Not a message: nothing to show, and nothing to re-show, so the token stays where it is.
        self.window.status_message = ""
        self.window.status_is_error = False

    # -- binding -----------------------------------------------------------------------------

    def _bind(self) -> None:
        window = self.window
        window.source_changed = self._guard(self._source_changed)
        window.config_changed = self._guard(self._config_changed)
        window.add_target = self._guard(self._add_target)
        window.remove_target = self._guard(self._remove_target)
        window.check_connection = self._guard(self._check_connection)
        window.store_password = self._guard(self._store_password)
        window.forget_password = self._guard(self._forget_password)
        window.test_tunnel = self._guard(self._test_tunnel)
        window.close_tunnel_dialog = self._guard(self._close_tunnel_dialog)
        window.close_run_dialog = self._guard(self._close_run_dialog)
        window.show_run_results = self._guard(self._show_run_results)
        window.choose_ssh_key = self._guard(self._choose_ssh_key)
        window.select_source_tab = self._guard(self._select_source_tab)
        window.check_all = self._guard(self._check_all)
        window.validate_config = self._guard(self._validate_config)
        window.save_config = self._guard(self._save_config)

        window.rule_changed = self._guard(self._rule_changed)
        window.add_rule = self._guard(self._add_rule)
        window.remove_rule = self._guard(self._remove_rule)
        window.save_ignores = self._guard(self._save_ignores)

        # Not _guard: the markup sets `running` itself when Start is pressed, so this one has to
        # clear it on every exit path of its own.
        window.start_run = self._start_run_handler
        window.cancel_run = self._guard(self._cancel_run)
        window.choose_baseline = self._guard(self._choose_baseline)
        window.choose_output_directory = self._guard(self._choose_output_directory)
        window.write_reports = self._guard(self._write_reports)
        window.open_html = self._guard(self._open_html)
        window.filter_changed = self._guard(self._filter_changed)
        window.select_finding = self._guard(self._select_finding)

    # -- the Config tab ----------------------------------------------------------------------

    def _source_changed(self, label: str, field: str, value: str) -> None:
        """One edit to one source.

        ``label`` may be empty, and that means a source whose label has been backspaced to
        nothing — not a config-level field, which has its own callback. Overloading it here made
        the label the one field in the window that could not be retyped: clearing it left a source
        nothing could address, and every keystroke after that was reported as an unknown option.
        """
        label, field, value = str(label), str(field), str(value)
        if field in ("liquibase_schema", "liquibase_table"):
            self._liquibase_changed(label, field, value)
        elif field.startswith("ssh_"):
            self._ssh_changed(label, field, value)
        else:
            if field == "label":
                self._check_rename(label, value)
                was = self._source(label).dsn_env
            parsed: Any = _parse_schema_map(value) if field == "schema_map" else value
            self.config.update_source(label, field, parsed)
            if field == "label" and label != value:
                self._restore_password(was, self._source(value).dsn_env)
                self._checks.pop(label, None)
                moved = self._source_tabs.pop(label, None)
                if moved is not None:
                    self._source_tabs[value] = moved
        self._clear_status()

    def _check_rename(self, label: str, new_label: str) -> None:
        taken = {s.label for s in self.config.config.sources} - {label}
        if new_label == "" and "" in taken:
            # Both would be addressed by the same empty label and neither could be typed into
            # again. Saying "a source labelled '' already exists" would read like a broken message.
            raise GuiError("another source's label is empty; finish that one first", field="label")
        if new_label in taken:
            raise GuiError(f"a source labelled {new_label!r} already exists", field="label")

    def _liquibase_changed(self, label: str, field: str, value: str) -> None:
        source = self._source(label)
        current = source.liquibase
        schema = current.schema_name if current is not None else ""
        table = current.table if current is not None else ""
        if field == "liquibase_schema":
            schema = value.strip()
            if not schema:
                # The table alone locates nothing, so clearing the schema clears the reference.
                self.config.update_source(label, "liquibase", None)
                return
        else:
            table = value.strip()
        if not schema and not table:
            self.config.update_source(label, "liquibase", None)
            return
        if not schema:
            raise GuiError(
                "name the liquibase schema before its table; the table alone cannot be located",
                field="liquibase",
            )
        self.config.update_source(
            label,
            "liquibase",
            LiquibaseRef(schema=schema, table=table or "DATABASECHANGELOG"),
        )

    def _ssh_changed(self, label: str, field: str, value: str) -> None:
        """Four widgets over one submodel, as the liquibase pair already is.

        Clearing the gateway clears the whole block: a key or a passphrase with nothing to connect
        to is not a half-finished setting, it is a setting that cannot mean anything.
        """
        source = self._source(label)
        current = source.ssh
        parts = {
            "host": "" if current is None else current.host,
            "user": "" if current is None or current.user is None else current.user,
            "private_key": (
                "" if current is None or current.private_key is None else current.private_key
            ),
            "passphrase_env": (
                "" if current is None or current.passphrase_env is None else current.passphrase_env
            ),
        }
        parts[_SSH_FIELDS[field]] = value.strip()

        if field == "ssh_host" and not parts["host"]:
            # Clearing the gateway clears the block, exactly as clearing the liquibase schema
            # clears that reference: the rest of it cannot mean anything on its own.
            self.config.update_source(label, "ssh", None)
            return

        if not parts["host"]:
            if any(parts.values()):
                raise GuiError(
                    "name the ssh gateway before its key; a key with no host to use it on "
                    "cannot connect to anything",
                    field="ssh",
                )
            self.config.update_source(label, "ssh", None)
            return

        self.config.update_source(
            label,
            "ssh",
            SshRef(
                host=parts["host"],
                port=22 if current is None else current.port,
                user=parts["user"] or None,
                private_key=parts["private_key"] or None,
                passphrase_env=parts["passphrase_env"] or None,
                known_hosts=None if current is None else current.known_hosts,
            ),
        )

    def _config_changed(self, field: str, value: str) -> None:
        config = self.config.config
        if field == "exclude_schemas":
            self.config.config = config.model_copy(update={"exclude_schemas": _split(value)})
        elif field in _OPTION_INTS:
            self._option_changed(field, _parse_int(field, value))
        elif field in _OPTION_BOOLS:
            self._option_changed(field, value.strip().lower() == "true")
        elif field == "fail_on":
            if value not in FAIL_ON_CHOICES:
                raise GuiError(
                    f"fail_on must be one of {', '.join(FAIL_ON_CHOICES)}", field="options.fail_on"
                )
            self._option_changed(field, value)
        else:
            raise GuiError(f"unknown configuration field {field!r}", field=field)
        self.config.dirty = True

    def _option_changed(self, field: str, value: object) -> None:
        config = self.config.config
        options = config.options.model_copy(update={field: value})
        self.config.config = config.model_copy(update={"options": options})

    def _source(self, label: str) -> SourceRef:
        for source in self.config.config.sources:
            if source.label == label:
                return source
        raise GuiError(f"no source labelled {label!r}")

    def _add_target(self) -> None:
        label = _unique("target", {s.label for s in self.config.config.sources})
        self.config.add_target(label)
        self._ok(f"Added target {label!r}. Give it a label and the name of its DSN variable.")

    def _remove_target(self, label: str) -> None:
        self.config.remove_target(str(label))
        self._checks.pop(str(label), None)
        self._ok(f"Removed target {str(label)!r}.")

    def _validate_config(self) -> None:
        errors = self.config.validate()
        if not errors:
            self._ok("The configuration is valid.")
            return
        self._fail(_summarise(errors))

    def _save_config(self) -> None:
        errors = self.config.validate()
        if errors:
            raise GuiError(_summarise(errors), errors[0].field)
        if self.config.path is None:
            # A config made here has nowhere of its own yet. It goes to the application's own
            # folder rather than refusing to save; one opened from a repository still saves back
            # to the repository, because `path` is set and this does not run.
            if home.ensure() is None:
                raise GuiError(
                    f"cannot create {home.directory()}; save it somewhere else by opening "
                    "an existing configuration first"
                )
            self.config.save(home.default_path())
        else:
            self.config.save()
        self._ok(f"Saved {self.config.path}.")

    # -- the Ignores tab ---------------------------------------------------------------------

    def _ignores(self) -> IgnoresDocument:
        ignores = self.ignores
        if ignores is None:  # pragma: no cover - set in __init__ and on every load
            raise GuiError("no ignore ruleset is loaded")
        return ignores

    def _rule_changed(self, rule_id: str, field: str, value: str) -> None:
        rule_id, field, value = str(rule_id), str(field), str(value)
        ignores = self._ignores()
        if not rule_id and field == "case_insensitive_globs":
            # The tab reuses rule-changed for the ruleset's own option; update_rule would call it
            # an unknown rule field.
            options = ignores.config.options.model_copy(
                update={"case_insensitive_globs": value.strip().lower() == "true"}
            )
            ignores.config = ignores.config.model_copy(update={"options": options})
            ignores.dirty = True
        else:
            ignores.update_rule(rule_id, field, value)
        # Free-text fields can build a rule that matches nothing, so say so as it is typed
        # rather than when Save is pressed.
        errors = [e for e in ignores.validate() if _mentions(e, ignores, rule_id)]
        if errors:
            self._fail(_summarise(errors))
        else:
            self._clear_status()

    def _add_rule(self) -> None:
        ignores = self._ignores()
        taken = {rule.id for rule in ignores.config.rules}
        taken.update(rule.id for rule in load_default_ignores().rules)
        rule_id = _unique("rule", taken)
        ignores.add_rule(rule_id)
        self._ok(
            f"Added rule {rule_id!r}. Replace its placeholder name pattern before saving: "
            "as added it matches nothing."
        )

    def _remove_rule(self, rule_id: str) -> None:
        self._ignores().remove_rule(str(rule_id))
        self._ok(f"Removed rule {str(rule_id)!r}.")

    def _save_ignores(self) -> None:
        ignores = self._ignores()
        created = False
        if ignores.path is None:
            ignores.path = self._default_ignores_path()
            created = True
        ignores.save()
        message = f"Saved {ignores.path}."
        if created:
            relative = ignores.path.name
            self.config.config = self.config.config.model_copy(update={"ignores_file": relative})
            self.config.dirty = True
            message += f" The configuration now names it as ignores_file: {relative} — save it too."
        self._ok(message)

    def _default_ignores_path(self) -> Path:
        path = self.config.path
        if path is None:
            raise GuiError("save the configuration first; the ruleset is stored beside it")
        return path.with_name(f"{path.stem}.ignores.yaml")

    # -- passwords -----------------------------------------------------------------

    def _store_password(self, label: str, secret: str) -> None:
        """Keep one source's password, as part of the connection string it belongs to.

        The password is not stored on its own: the parts in the configuration file plus the typed
        password make one connection string, and that is what goes into the keychain — so the rest
        of the application keeps resolving a credential exactly as it always has.
        """
        label, secret = str(label), str(secret)
        if not secret.strip():
            raise GuiError("nothing typed to store", field="password")
        source = self._source(label)
        env_name = source.dsn_env
        dsn = connection.build(source, secret, env_name=env_name)
        try:
            self.credentials.store(env_name, dsn.value)
        except Exception as exc:
            raise GuiError(_sanitise(str(exc))) from None
        self._described.pop(env_name, None)
        self._ok(f"Password stored for {label}.")

    def _forget_password(self, label: str) -> None:
        label = str(label)
        source = self._source(label)
        try:
            self.credentials.forget(source.dsn_env)
        except Exception as exc:
            raise GuiError(_sanitise(str(exc))) from None
        self._described.pop(source.dsn_env, None)
        self._ok(f"Password forgotten for {label}.")

    def _restore_password(self, old_env: str, new_env: str) -> None:
        """Move a stored credential when a rename moves the variable it is filed under.

        Without this a rename orphans the password: the source looks for a name nothing was ever
        stored against, and the old entry sits in the keychain belonging to nobody.
        """
        if old_env == new_env:
            return
        try:
            existing = self.credentials.resolve(old_env)
        except Exception:
            return
        with contextlib.suppress(Exception):
            self.credentials.store(new_env, existing.value)
            self.credentials.forget(old_env)
        self._described.pop(old_env, None)
        self._described.pop(new_env, None)

    def _loop(self) -> asyncio.AbstractEventLoop:
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            raise GuiError(
                "the application's event loop is not running, so nothing can be started"
            ) from None

    def _busy(self) -> bool:
        return self._task is not None and not self._task.done()

    def _run_in_flight(self) -> bool:
        """Whether a *comparison* is in flight, which is whose claim ``running`` is."""
        return self._run_task is not None and not self._run_task.done()

    def _release_gate(self) -> None:
        """Clear the markup's ``running`` claim, unless the run that owns it is still going.

        A stale Start click while a comparison runs is refused at the session, and clearing the
        flag there would re-enable Start and disable Cancel in the middle of a real run.
        """
        if not self._run_in_flight():
            self.window.running = False

    async def wait_for_idle(self) -> None:
        """Await whatever check or comparison is in flight, if any.

        Nothing in the application needs this — the window is updated by the task itself — but a
        host driving the window headlessly has no other way to know when a run has finished.
        Never raises: the task reports its own outcome into the window.
        """
        task = self._task
        if task is not None and not task.done():
            await asyncio.wait({task})

    def _release_session(self) -> None:
        if self._busy():
            raise GuiError("a check or comparison is already running")
        self._session = None
        self._session_config = None

    def _session_now(self) -> Session:
        """The one session, built from the configuration as it stands.

        Rebuilt when the configuration has changed, and only while nothing is in flight: the run
        lock lives on the Session, so two of them could compare the same databases at once.
        """
        config = self._effective_config()
        if self._session is not None and self._session_config == config:
            return self._session
        if self._busy():
            raise GuiError("a check or comparison is already running")
        self._session = Session(
            config,
            self.credentials,
            on_progress=self._on_progress,
            on_detail=self._on_detail,
        )
        self._session_config = config
        return self._session

    def _effective_config(self) -> ComparerConfig:
        """The configuration plus the Run tab's gate, which is a per-run choice."""
        config = self.config.config
        chosen = str(self.window.run_fail_on)
        if chosen not in FAIL_ON_CHOICES or chosen == config.options.fail_on:
            return config
        gate: FailOn = chosen  # type: ignore[assignment]
        return config.model_copy(
            update={"options": config.options.model_copy(update={"fail_on": gate})}
        )

    def _rule_set(self) -> IgnoreRuleSet:
        ignores = self._ignores()
        defaults = None if bool(self.window.no_default_ignores) else load_default_ignores()
        return IgnoreRuleSet.from_config(ignores.config, defaults=defaults)

    def _baseline(self) -> ComparisonReport | None:
        path = str(self.window.baseline_path).strip()
        return None if not path else Session.load_baseline(Path(path).expanduser())

    def _start_run_handler(self) -> None:
        """``start_run``, with the markup's ``running`` claim cleared on every exit path.

        The markup sets ``running`` before calling this so the gate does not depend on Python. If
        this raises, Slint only prints the exception, and a ``running`` left set disables Start
        and enables Cancel for good.
        """
        try:
            self._start_run()
        except (GuiError, ComparerError) as exc:
            self._release_gate()
            self._fail(exc)
            self._refresh()
        except BaseException:
            self._release_gate()
            self._refresh()
            raise

    def _start_run(self) -> None:
        loop = self._loop()
        session = self._session_now()
        # Everything that can fail does so before the claim: an invalid baseline or ruleset must
        # not leave the session held by a coroutine nobody awaits.
        ignores = self._rule_set()
        baseline = self._baseline()
        changelog = ChangelogOptions(strict=bool(self.window.strict_changelog))
        skip_liquibase = bool(self.window.skip_liquibase)
        sequential = bool(self.window.sequential)
        show_cosmetic = bool(self.window.show_cosmetic)
        # Called here, on the loop thread: this claims the session, and the coroutine it returns
        # is handed to create_task below with nothing in between that can fail.
        work = session.compare(
            ignores=ignores,
            changelog_options=changelog,
            skip_liquibase=skip_liquibase,
            sequential=sequential,
            baseline=baseline,
            show_cosmetic=show_cosmetic,
        )
        self._begin_run()
        self._task = self._run_task = loop.create_task(self._finish_run(work))

    def _begin_run(self) -> None:
        """Clear the last run's verdict, seed one progress row per source, open the dialog."""
        self.results = None
        self._verdict = ("", "")
        self._clear_status()
        config = self._effective_config()
        labels = [source.label for source in config.sources]
        self._progress = slint.ListModel(
            [_progress_row(label, SourceState.IDLE, "") for label in labels]
        )
        self._progress_index = {label: index for index, label in enumerate(labels)}
        self.window.progress = self._progress
        self._open_run_dialog(config)
        self._refresh_results()

    # -- the run dialog ----------------------------------------------------------------------

    def _open_run_dialog(self, config: ComparerConfig) -> None:
        """Say what is about to be compared, and lay out the checklist it will fill in.

        The whole checklist is written before anything runs, so the rows a run will touch are
        visible from the start — a list that grows as results arrive cannot be told from a list
        that has stopped. Rows are then updated in place; the model is never reassigned, because
        replacing it rebuilds every repeated element and would throw away the scroll position
        halfway through a run.
        """
        window = self.window
        window.run_title = f"Comparing {config.name}"
        window.run_summary = ""
        window.run_summary_level = "ok"
        window.run_busy = True
        window.run_open = True

        self._run_inputs = slint.ListModel(_run_inputs(config, self._run_flags()))
        window.run_inputs = self._run_inputs

        rows: list[dict[str, Any]] = []
        self._run_step_index = {}
        self._run_found = {}
        self._run_state = {}
        skip_liquibase = bool(window.skip_liquibase)
        steps = list(CAPTURE_ROWS) + ([] if skip_liquibase else [CHANGELOG_STEP])
        for source in config.sources:
            role = "master" if source.label == config.master.label else "target"
            self._run_step_index[(source.label, "")] = len(rows)
            rows.append(_run_step(f"{source.label} ({role})", "pending", "", indent=0))
            self._run_found[source.label] = 0
            for step in steps:
                self._run_step_index[(source.label, step)] = len(rows)
                # A sub-row sits a level deeper than the step it belongs to: "materialized views"
                # is part of what the views query returned, not another query.
                rows.append(_run_step(step, "pending", "", indent=2 if _is_sub_row(step) else 1))
        for phase, label in ((COMPARE_PHASE, "compare"), (REPORT_PHASE, "build report")):
            self._run_step_index[("", phase)] = len(rows)
            rows.append(_run_step(label, "pending", "", indent=0))
        self._run_steps = slint.ListModel(rows)
        window.run_steps = self._run_steps

    def _run_flags(self) -> list[tuple[str, str]]:
        """The per-run choices, which are in the window rather than in the configuration."""
        window = self.window
        flags = [("gate", f"fail on {window.run_fail_on}")]
        for label, on in (
            ("cosmetic differences shown", bool(window.show_cosmetic)),
            ("Liquibase skipped", bool(window.skip_liquibase)),
            ("changelog compared strictly", bool(window.strict_changelog)),
            ("bundled rules off", bool(window.no_default_ignores)),
            ("one source at a time", bool(window.sequential)),
        ):
            if on:
                flags.append(("", label))
        baseline = str(window.baseline_path)
        if baseline:
            flags.append(("baseline", baseline))
        return flags

    def _set_run_step(self, key: tuple[str, str], state: str, detail: str = "") -> None:
        """Update one checklist row in place, keeping its label and its indent."""
        index = self._run_step_index.get(key)
        if index is None:
            return
        existing = dict(self._run_steps[index])
        self._run_steps[index] = _run_step(
            str(existing["label"]), state, detail, indent=int(existing["indent"])
        )

    def _render_heading(self, label: str) -> None:
        """Draw one source's heading row from its state and what it found.

        Called from both sides, because the two arrive in no fixed order: a step report is queued
        onto the loop from the capture's worker thread, while the source's state is emitted on the
        loop itself. Deciding the row at either moment alone made a healthy capture read "no
        objects found" whenever the state won the race — intermittently, and only in a full run.
        """
        state = self._run_state.get(label, "running")
        found = self._run_found.get(label)
        if state == "ok" and found == 0:
            # Connected, and read nothing. One source's problem, said against that source: a
            # schema filter can match on one server and not on another.
            self._set_run_step((label, ""), "failed", "no objects found")
            return
        detail = "" if found is None or state == "running" else f"{found} objects"
        self._set_run_step((label, ""), state, detail)

    def _on_detail(self, detail: RunDetail) -> None:
        """One row of the run. Always on the loop thread; see ``Session._capture_observer``."""
        if detail.phase == CAPTURE_PHASE:
            self._run_state.setdefault(detail.source, "running")
            # The schemas row says which ones matched; every other row says how many it found.
            if detail.step == SCHEMA_STEP:
                state = "ok" if detail.count else "failed"
                self._set_run_step((detail.source, detail.step), state, detail.detail)
            else:
                self._set_run_step((detail.source, detail.step), "ok", f"{detail.count}")
                # Sub-rows are a breakdown of their step, so counting both would double. The
                # changelog is not an object either: a database with a DATABASECHANGELOG and no
                # schema of its own has been looked at and found empty.
                if not _is_sub_row(detail.step) and detail.step != CHANGELOG_STEP:
                    self._run_found[detail.source] = (
                        self._run_found.get(detail.source, 0) + detail.count
                    )
            self._render_heading(detail.source)
            return
        if detail.phase == COMPARE_PHASE:
            self._finish_pending_captures()
            self._set_run_step(("", COMPARE_PHASE), "running")
        elif detail.phase == REPORT_PHASE:
            self._set_run_step(("", COMPARE_PHASE), "ok")
            self._set_run_step(("", REPORT_PHASE), "running")

    def _finish_pending_captures(self) -> None:
        """Mark as skipped every capture step that never reported.

        A source that failed to connect reports no steps at all, and a run that left ten rows
        saying "pending" would read as a run that stalled there rather than one that never got in.
        """
        for (label, step), index in self._run_step_index.items():
            if not label or not step:
                continue
            if str(dict(self._run_steps[index])["state"]) == "pending":
                self._set_run_step((label, step), "skipped")

    def _close_run_dialog(self) -> None:
        self.window.run_open = False

    def _show_run_results(self) -> None:
        """Close the dialog and go where the report is. The one button that moves you on."""
        self.window.run_open = False
        self.window.current_view = RESULTS_VIEW

    async def _finish_run(self, work: Coroutine[Any, Any, ComparisonReport]) -> None:
        try:
            report = await work
        except asyncio.CancelledError:
            # The run-level outcome. Every source can have reported CAPTURED and the run still
            # end here, so progress is never read to decide this.
            self.results = None
            self._verdict = ("cancelled", CANCELLED_VERDICT)
            # Not _ok: StatusLine has exactly two colours, and green is the one that means "that
            # worked". A cancelled run produced no report, so the always-on widget has to agree
            # with the banner instead of contradicting it in green.
            self._fail(CANCELLED_STATUS)
        except (GuiError, ComparerError) as exc:
            self._run_failed(str(exc))
        except Exception as exc:
            log.warning("the comparison failed unexpectedly (%s)", type(exc).__name__)
            self._run_failed(f"the comparison failed unexpectedly ({type(exc).__name__})")
        else:
            self._finished(report)
        finally:
            self.window.running = False
            self._settle_run_dialog()
            self._refresh()

    def _settle_run_dialog(self) -> None:
        """Stop the dialog claiming to be busy, and put the outcome in it.

        Runs on every exit path, including a cancel and an unexpected failure: a dialog left busy
        shows Cancel and no way out, which is the one state the window must never be in.
        """
        window = self.window
        if not bool(window.run_open):
            return
        window.run_busy = False
        self._finish_pending_captures()
        level, sentence = self._verdict
        for phase in (COMPARE_PHASE, REPORT_PHASE):
            index = self._run_step_index.get(("", phase))
            if index is None:
                continue
            was = str(dict(self._run_steps[index])["state"])
            if was in ("pending", "running"):
                self._set_run_step(("", phase), "ok" if self.results is not None else "skipped")
        if sentence:
            window.run_summary = sentence
            window.run_summary_level = level or "ok"
        else:
            # No report, so no verdict. The status line says why; the dialog must not imply one.
            window.run_summary = _sanitise(str(window.status_message))
            window.run_summary_level = "error"

    def _run_failed(self, message: str) -> None:
        """No report, so no verdict: the banner says nothing has finished and the status says why.

        An "error" level would make the markup offer to write a report that does not exist.
        """
        self.results = None
        self._verdict = ("", "")
        self._fail(message)

    def _finished(self, report: ComparisonReport) -> None:
        self.results = ResultsModel(report)
        self._verdict = self.results.verdict
        note = f"Comparison finished: {len(report.targets)} target(s)."
        if report.probe_failed and bool(self.window.allow_unreachable):
            note += (
                " 'Allow unreachable' would waive the command line's exit code for the targets "
                "that were reachable; this comparison is still incomplete."
            )
        self._ok(note)

    def _cancel_run(self) -> None:
        session = self._session
        if session is None or not self._run_in_flight():
            # Nothing to stop, and the gate must not stay latched.
            self.window.running = False
            self._ok("Nothing is running.")
            return
        session.cancel()
        # Not _ok either: this says production may still be under load for minutes. StatusLine's
        # green is the colour that means "that worked", and this needs attention, not reassurance.
        self._fail(
            "Cancelling. Sources not yet started will not start; those already connected are "
            "abandoned and can keep querying until their statement timeout expires."
        )

    def _select_source_tab(self, label: str, index: int) -> None:
        """Remember which tab a card is showing, so a refresh does not move it."""
        self._source_tabs[str(label)] = int(index)

    def _choose_ssh_key(self, label: str) -> None:
        """The private key's Choose… button. Nothing is read: only the path is taken."""
        label = str(label)
        source = self._source(label)
        current = (
            "" if source.ssh is None or source.ssh.private_key is None else source.ssh.private_key
        )
        chosen = self.choose_path("ssh-key", current)
        if chosen is None:
            if not self.path_dialogs_available():
                raise GuiError("no file dialog on this system; type the key's path into the field")
            self._ok("No key chosen.")
            return
        self._ssh_changed(label, "ssh_key", chosen)
        self._ok(f"Key: {chosen}")

    #: The steps a gateway test goes through, in order, with what to call them on screen. Laid out
    #: before anything runs so the dialog shows the whole shape of the test rather than growing a
    #: line at a time.
    _TUNNEL_STEPS = (
        ("key", "Key"),
        ("gateway", "Gateway"),
        ("database", "Channel to the database"),
    )

    def _test_tunnel(self, label: str) -> None:
        """The gateway's own test, separate from Check connection.

        A refused key and a database that is down are different problems with different fixes, and
        one message covering both sends people to the wrong field.

        Opens its dialog straight away, with every step pending. The gateway can take seconds to
        answer and a window that shows nothing until it does looks like a window that has stopped.
        """
        label = str(label)
        source = self._source(label)
        if source.ssh is None:
            raise GuiError("this source has no ssh gateway to test", field="ssh")

        window = self.window
        window.tunnel_gateway = describe_gateway(source.ssh)
        window.tunnel_summary = ""
        window.tunnel_ok = False
        window.tunnel_busy = True
        window.tunnel_open = True
        self._tunnel_steps = slint.ListModel(
            [{"label": name, "state": "pending", "detail": ""} for _, name in self._TUNNEL_STEPS]
        )
        window.tunnel_steps = self._tunnel_steps

        loop = self._loop()
        session = self._session_now()
        work = session.check_tunnel(label, self._tunnel_step)
        self._task = loop.create_task(self._finish_tunnel(work))

    def _tunnel_step(self, step: str, state: str, detail: str) -> None:
        """One step's progress. Called on the loop thread; see Session.check_tunnel for why."""
        for index, (key, _) in enumerate(self._TUNNEL_STEPS):
            if key == step:
                row = dict(self._tunnel_steps[index])
                row["state"] = state
                row["detail"] = detail or row["detail"]
                self._tunnel_steps[index] = row
                return

    def _close_tunnel_dialog(self) -> None:
        self.window.tunnel_open = False

    async def _finish_tunnel(self, work: Coroutine[Any, Any, Any]) -> None:
        window = self.window
        try:
            status = await work
        except asyncio.CancelledError:
            window.tunnel_busy = False
            raise
        except Exception as exc:
            window.tunnel_busy = False
            window.tunnel_ok = False
            window.tunnel_summary = f"the test failed unexpectedly ({type(exc).__name__})"
            return
        finally:
            self._refresh()
        window.tunnel_busy = False
        window.tunnel_ok = status.ok
        # The dialog carries the answer, so the toast would be the same thing said twice over a
        # window that is already showing it.
        window.tunnel_summary = _sanitise(status.detail)

    def _check_connection(self, label: str) -> None:
        # Named before anything else can fail, so a mistyped label says so.
        self._start_check([self._source(str(label)).label])

    def _check_all(self) -> None:
        self._start_check([source.label for source in self.config.config.sources])

    def _start_check(self, labels: list[str]) -> None:
        loop = self._loop()
        session = self._session_now()
        work: Coroutine[Any, Any, list[ConnectionStatus]]
        if len(labels) == 1:
            # An unknown label raises before the claim is taken.
            work = _as_list(session.check_connection(labels[0]))
        else:
            work = session.check_all()
        self._task = loop.create_task(self._finish_check(work))
        self._ok(f"Checking {', '.join(labels)}…")

    async def _finish_check(self, work: Coroutine[Any, Any, list[ConnectionStatus]]) -> None:
        try:
            statuses = await work
        except asyncio.CancelledError:
            # Red for the same reason a cancelled comparison is: no result was produced.
            self._fail("The connection check was cancelled.")
        except (GuiError, ComparerError) as exc:
            self._fail(exc)
        except Exception as exc:
            log.warning("the connection check failed unexpectedly (%s)", type(exc).__name__)
            self._fail(f"the connection check failed unexpectedly ({type(exc).__name__})")
        else:
            for status in statuses:
                self._checks[status.label] = status
            reachable = [status.label for status in statuses if status.ok]
            unreachable = [status.label for status in statuses if not status.ok]
            if unreachable:
                self._fail(f"not reachable: {', '.join(unreachable)}")
            else:
                self._ok(f"reachable: {', '.join(reachable)}")
        finally:
            self._refresh()

    def _on_progress(self, event: ProgressEvent) -> None:
        """One source's state changed. Always on the loop thread, so it writes properties here.

        The row is replaced whole: Slint neither defaults nor rejects a partial row dict.
        """
        row = _progress_row(event.label, event.state, event.detail or "")
        index = self._progress_index.get(event.label)
        if index is None:
            self._progress_index[event.label] = self._progress.row_count()
            self._progress.append(row)
        else:
            self._progress[index] = row
        # The dialog's heading row for this source is its state, so it comes from here rather than
        # from the step observer: a source that fails to connect reports no steps at all, and its
        # heading would otherwise sit at "pending" for the rest of the run.
        self._run_state[event.label] = _STEP_STATE.get(event.state, "running")
        self._render_heading(event.label)

    # -- the Results tab ---------------------------------------------------------------------

    def _filter_changed(self) -> None:
        # The detail pane describes a finding chosen under the previous filter. Identity makes it
        # impossible to show the wrong one; clearing keeps it from showing a stale one.
        self._clear_selection()
        self._refresh_results()

    def select_finding(self, target: str, kind: str, path: str) -> None:
        """Show the detail of the finding named ``(target, kind, path)``."""
        self._guard(self._select_finding)(target, kind, path)

    def _select_finding(self, target: str, kind: str, path: str) -> None:
        """Fill the detail pane for one finding, named by identity and not by position.

        An index into the filtered rows only means something under the exact filter state it was
        clicked under, and a reorder once showed one finding's diff under another's heading.
        ``(target, path)`` was not enough either: ``ObjectKey.path`` omits the kind, so a column,
        a constraint and a trigger can share one. The live filter state is passed through because
        the view model resolves the identity against the filtered list.

        The models are assigned only through ``checked_delta_rows`` / ``checked_diff_rows``. Slint
        accepts a row dict with a key missing and then aborts the process at the next repaint
        (exit 134, no Python exception); the validator raises a ``GuiError`` instead, which the
        guard turns into the status line.

        An empty delta list is not an empty selection. A missing or extra object has no attribute
        differences — there is no second version of it to differ from — and it is the commonest
        finding there is; clearing the pane for it made three clicks out of four read as a no-op.
        Whether the finding resolves at all is asked separately, and only that clears the pane.
        """
        target, kind, path = str(target), str(kind), str(path)
        results = self.results
        if results is None:
            self._clear_selection()
            return
        needle, severities = str(self.window.filter_text), self._severities()
        status = results.finding_status(target, kind, path, needle=needle, severities=severities)
        if status is None:
            self._clear_selection()
            return
        deltas = results.delta_rows(target, kind, path, needle=needle, severities=severities)
        diff = results.diff_rows(target, kind, path, needle=needle, severities=severities)
        deltas_checked = checked_delta_rows(deltas)
        diff_checked = checked_diff_rows(diff)
        heading = f"{kind} {path} on {target}: {status}"
        if not deltas:
            heading = f"{heading}; no attribute differences"
        self.window.selected_heading = _one_line(heading)
        self.window.selected_deltas = slint.ListModel(deltas_checked)
        self.window.selected_diff = slint.ListModel(diff_checked)

    def _clear_selection(self) -> None:
        self.window.selected_heading = ""
        self.window.selected_deltas = slint.ListModel([])
        self.window.selected_diff = slint.ListModel([])

    def _results(self) -> ResultsModel:
        results = self.results
        if results is None or self._verdict[0] not in _REPORT_LEVELS:
            raise GuiError("no finished comparison to report on; run a comparison first")
        return results

    def _output_directory(self) -> Path:
        chosen = str(self.window.output_directory).strip()
        if not chosen:
            raise GuiError("choose an output directory first")
        path = Path(chosen).expanduser()
        if not path.is_dir():
            raise GuiError(f"{path}: not a directory")
        return path

    def _write_reports(self) -> None:
        results = self._results()
        directory = self._output_directory()
        written = results.write_reports(directory)
        self._ok(f"Wrote {', '.join(p.name for p in written)} to {directory}.")

    def _open_html(self) -> None:
        results = self._results()
        path = results.html_path(self._output_directory())
        if not path.exists():
            raise GuiError(f"{path.name} has not been written yet; write the reports first")
        self.open_url(path.as_uri())
        self._ok(f"Opened {path}.")

    def _choose_baseline(self) -> None:
        chosen = self.choose_path("baseline", str(self.window.baseline_path))
        if chosen is None:
            # Cancelling is not a failure, and must not read as one. Only a machine with no
            # picker gets told to type the path instead.
            if not self.path_dialogs_available():
                raise GuiError(
                    "no file dialog on this system; type the baseline report's path into the field"
                )
            self._ok("No baseline chosen.")
            return
        self.window.baseline_path = chosen
        self._ok(f"Baseline: {chosen}")

    def _choose_output_directory(self) -> None:
        chosen = self.choose_path("output-directory", str(self.window.output_directory))
        if chosen is None:
            if not self.path_dialogs_available():
                raise GuiError(
                    "no file dialog on this system; type the output directory into the field"
                )
            self._ok("No output directory chosen.")
            return
        self.window.output_directory = chosen
        self._ok(f"Reports will be written to {chosen}.")


def _describe_check(status: ConnectionStatus) -> str:
    """One line about a checked source. ``error`` is already redacted; sanitised again anyway."""
    if not status.ok:
        return _sanitise(status.error or "not reachable")
    version = (status.server_version or "").split(" ")[0]
    parts = [f"PostgreSQL {version}" if version else "connected"]
    if status.database:
        parts.append(f"{status.database} as {status.user}")
    parts.append(f"schemas: {', '.join(status.schemas) or '(none)'}")
    if status.changelog:
        count = "" if status.changelog_count is None else f": {status.changelog_count} changeset(s)"
        parts.append(f"liquibase {status.changelog}{count}")
    elif status.changelog_candidates:
        parts.append(f"liquibase ambiguous: {', '.join(status.changelog_candidates)}")
    return _sanitise(" · ".join(parts))


def _progress_row(label: str, state: SourceState, detail: str) -> dict[str, Any]:
    """All three ``ProgressRow`` fields."""
    return {"label": label, "state": str(state), "detail": _one_line(detail)}


def _rule_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """All nine ``RuleRow`` fields, with the list fields joined.

    The view model hands back Python lists, and Slint accepts a list in a string field without
    complaint — rendering ``[]`` or ``['quartz.*']`` into the form.
    """
    return {
        "id": str(row["id"]),
        "reason": str(row["reason"]),
        "kinds": ", ".join(row["kinds"]),
        "names": ", ".join(row["names"]),
        "attributes": ", ".join(row["attributes"]),
        "targets": ", ".join(row["targets"]),
        "statuses": ", ".join(row["statuses"]),
        "action": str(row["action"]),
        "read_only": bool(row["read_only"]),
    }


def _finding_row(row: Any) -> dict[str, Any]:
    """All seven ``FindingRow`` fields."""
    return {
        "target": row.target,
        "kind": row.kind,
        "path": row.path,
        "status": row.status,
        "severity": row.severity,
        "detail": _one_line(row.detail),
        "suppressed_by": row.suppressed_by or "",
    }


def _split(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _parse_int(field: str, value: str) -> int:
    try:
        return int(float(value.strip()))
    except (TypeError, ValueError):
        raise GuiError(f"{field}: a whole number is required", field=f"options.{field}") from None


def _parse_schema_map(value: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for entry in value.split(","):
        if not entry.strip():
            continue
        master, _, target = entry.partition("=")
        if not master.strip() or not target.strip():
            raise GuiError(
                "schema_map is written master=target, comma separated", field="schema_map"
            )
        mapping[master.strip()] = target.strip()
    return mapping


def _unique(stem: str, taken: set[str]) -> str:
    """``stem``, or ``stem-2``, ``stem-3``… — never a name already in use."""
    if stem not in taken:
        return stem
    index = 2
    while f"{stem}-{index}" in taken:
        index += 1
    return f"{stem}-{index}"


def _has_comments(path: Path) -> bool:
    """Whether a configuration file carries comment lines of its own.

    A line whose first non-space character is ``#``, which is what a comment is in YAML. A ``#``
    inside a quoted value is not one and is not looked for: this decides whether to mention that
    Save rewrites the file, and being wrong about it costs a sentence.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:  # pragma: no cover - the loader has just read this file
        return False
    return any(line.lstrip().startswith("#") for line in text.splitlines())


def _run_step(label: str, state: str, detail: str, *, indent: int) -> dict[str, Any]:
    """One checklist row. Every field, because Slint neither defaults nor rejects a partial row."""
    return {"label": label, "state": state, "detail": detail, "indent": indent}


def _is_sub_row(step: str) -> bool:
    """Whether a row is a breakdown of the step above it rather than a step of its own."""
    return any(step in subs for subs in STEP_KINDS.values())


def _run_input(label: str, value: str, *, nested: bool = False) -> dict[str, Any]:
    return {"label": label, "value": value, "nested": nested}


def _run_inputs(config: ComparerConfig, flags: Sequence[tuple[str, str]]) -> list[dict[str, Any]]:
    """What is about to be compared, as lines for the dialog.

    Every value here is one the configuration file holds, so none of it is secret and no
    connection string is assembled to show it: a source is described by where it is, not by how it
    is reached.
    """
    rows = [_run_input("name", config.name)]
    for source in config.sources:
        role = "master" if source.label == config.master.label else "target"
        rows.append(_run_input(role, f"{source.label}  {_where(source)}"))
        if source.schemas:
            rows.append(_run_input("", f"schemas: {', '.join(source.schemas)}", nested=True))
        else:
            rows.append(_run_input("", "schemas: every non-system schema", nested=True))
        if source.schema_map:
            mapped = ", ".join(f"{k}={v}" for k, v in source.schema_map.items())
            rows.append(_run_input("", f"schema map: {mapped}", nested=True))
        if source.ssh is not None:
            rows.append(_run_input("", f"through {describe_gateway(source.ssh)}", nested=True))
    if config.exclude_schemas:
        rows.append(_run_input("excluded", ", ".join(config.exclude_schemas)))
    rows.extend(_run_input(label, value, nested=not label) for label, value in flags)
    return rows


def _where(source: SourceRef) -> str:
    """A source's location, as the configuration spells it. Never a connection string.

    An empty host is normal: a configuration written by hand names only a variable, and the window
    has nothing to show but the name of the credential it will resolve.
    """
    host = source.host or ""
    database = source.database or ""
    if host and database:
        return f"{host}/{database}"
    if host or database:
        return host or database
    return f"${source.dsn_env}"


def _summarise(errors: Sequence[GuiError]) -> str:
    return "; ".join(f"{e.field}: {e}" if e.field else str(e) for e in errors)


def _mentions(error: GuiError, ignores: IgnoresDocument, rule_id: str) -> bool:
    """Whether ``error`` is about the rule just edited, so typing reports only its own problems."""
    if not rule_id or error.field is None:
        return True
    for index, rule in enumerate(ignores.config.rules):
        if rule.id == rule_id:
            return error.field.startswith(f"rules.{index}.")
    return True


#: How often the application collects its own garbage. See :func:`_show`.
GC_INTERVAL_SECONDS = 2.0


async def _show(window: Any, interval: float = GC_INTERVAL_SECONDS) -> None:
    """Show the window, then own the garbage collector for the rest of the process.

    Slint's Python values are pyo3 ``unsendable`` classes: freeing one on a thread other than the
    one that created it aborts the process, and the cyclic collector runs on whichever thread
    happens to cross its allocation threshold. Every comparison runs captures on worker threads,
    so with the collector enabled a long run can abort the application outright — reproducibly,
    with no Python traceback, only ``PyStruct is unsendable, but sent to another thread``.

    The collector is therefore disabled in :func:`run` and driven from here instead, on the loop
    thread, which is the thread that created every one of those values. Reference-counted frees
    are unaffected, so this changes when cycles are reclaimed and nothing else.
    """
    window.show()
    while True:
        await asyncio.sleep(interval)
        gc.collect()


def run(argv: Sequence[str] | None = None) -> int:
    """Start the application. ``0`` on a clean exit, ``1`` on a failure it could not show."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    collecting = gc.isenabled()
    try:
        # Must be off before any worker thread exists; see _show for why.
        gc.disable()
        # Before the window, so a first run has somewhere to save to and the reopen below has a
        # directory to look in.
        # Here rather than in the launcher: this is where the application starts, whatever started
        # it — the console script, `python -m`, or the compiled binary.
        home.ensure()
        application = Application()
        # The real pickers belong to the host, not to the Application: see _no_dialog.
        application.choose_path = dialogs.choose
        application.path_dialogs_available = dialogs.available
        if arguments:
            print(
                "cumo-schema-diff-gui takes no arguments. It reads and writes one "
                f"configuration, {home.default_path()}.",
                file=sys.stderr,
            )
        slint.run_event_loop(_show(application.window))
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        # Only the type: the text of an unexpected failure is under nobody's redaction.
        print(
            f"cumo-schema-diff-gui could not run ({type(exc).__name__}).",
            file=sys.stderr,
        )
        log.warning("the application failed to run (%s)", type(exc).__name__)
        return 1
    finally:
        if collecting:
            gc.enable()
    return 0


__all__ = ["Application", "run"]
