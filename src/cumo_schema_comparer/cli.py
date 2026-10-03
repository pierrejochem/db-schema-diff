"""Command-line surface.

This module wires click to :mod:`cumo_schema_comparer.runner` and does nothing else: no
introspection, no diffing, no reporting. Keeping the logic out of here is what lets the
commands be tested through their collaborators instead of through click.

There is deliberately no ``--dsn`` and no ``--password`` flag, and there never will be:
credentials arrive only through the environment, so argv and shell history cannot carry one.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import click

from . import __version__, runner
from .baseline import apply_baseline
from .config.loader import load_config, load_config_files, load_ignores, resolve_ignores
from .config.model import ComparerConfig
from .diff.changelog import ChangelogOptions
from .diff.engine import diff_inventories
from .diff.ignores import IgnoreRuleSet, load_default_ignores
from .diff.model import ComparisonReport, DiffOptions
from .diff.severity import gate
from .errors import ComparerError, ConfigError, ProbeError
from .exit_codes import ExitCode
from .logging_setup import configure
from .model.inventory import Inventory
from .report.base import Reporter, render_to_path
from .report.console import DEFAULT_MAX_PER_KIND, ConsoleReporter
from .report.html import HtmlReporter
from .report.json_report import JsonReporter, load_report
from .report.junit import DEFAULT_MAX_CASES, JUnitReporter

CONTEXT_SETTINGS = {
    "help_option_names": ["-h", "--help"],
    "auto_envvar_prefix": "CUMO_SCHEMA_DIFF",
    "max_content_width": 100,
}


@click.group(context_settings=CONTEXT_SETTINGS)
@click.version_option(__version__, "-V", "--version", prog_name="cumo-schema-diff")
@click.option("-v", "--verbose", count=True, help="Increase log verbosity. Repeatable.")
@click.option("-q", "--quiet", is_flag=True, help="Suppress progress output.")
@click.option("--debug", is_flag=True, help="Show tracebacks for expected failures too.")
@click.pass_context
def cli(ctx: click.Context, verbose: int, quiet: bool, debug: bool) -> None:
    """Compare a master PostgreSQL schema against 1..n other environments."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    ctx.obj["quiet"] = quiet
    ctx.obj["debug"] = debug


@cli.command("compare")
@click.option(
    "-c",
    "--config",
    "config_paths",
    multiple=True,
    required=True,
    type=click.Path(path_type=Path),
    help="Config file, or a directory of them. Repeatable.",
)
@click.option(
    "--target",
    "target_labels",
    multiple=True,
    help="Compare only these targets, by label. Default: all of them.",
)
@click.option(
    "--exclude-schema",
    "exclude_schemas",
    multiple=True,
    help="Schema to leave out of the comparison. Repeatable.",
)
@click.option(
    "--fail-on",
    type=click.Choice(["error", "warning", "any", "never"]),
    help="Lowest severity that should fail the run. Default: the config's value, else error.",
)
@click.option(
    "--ignore-column-order/--no-ignore-column-order",
    default=None,
    help="Treat a different column order as acceptable.",
)
@click.option("--show-cosmetic", is_flag=True, help="Also show differences that normalise away.")
@click.option(
    "--ignores",
    "ignores_path",
    type=click.Path(exists=True, path_type=Path),
    help="Ignore ruleset, layered over the bundled defaults. Overrides the config's ignores_file.",
)
@click.option(
    "--no-default-ignores",
    is_flag=True,
    help="Do not apply the bundled ruleset (Quartz state, Liquibase tables, scratch objects).",
)
@click.option(
    "--show-ignored",
    is_flag=True,
    help="List the findings the ignore rules suppressed, and which rule suppressed each.",
)
@click.option(
    "--skip-liquibase",
    is_flag=True,
    help="Do not read DATABASECHANGELOG. Use when a database is not Liquibase-managed.",
)
@click.option(
    "--strict-changelog",
    is_flag=True,
    help="Also compare COMMENTS, CONTEXTS and LABELS, which changelog refactoring rewrites.",
)
@click.option(
    "--baseline",
    "baseline_path",
    type=click.Path(exists=True, path_type=Path),
    help="A previously-approved JSON report. Only findings it does not contain fail the run.",
)
@click.option(
    "--allow-unreachable",
    is_flag=True,
    help="Report an unreachable target as skipped instead of failing the run.",
)
@click.option("--sequential", is_flag=True, help="Capture sources one at a time, for debugging.")
@click.option(
    "--max-findings-per-kind",
    type=int,
    default=DEFAULT_MAX_PER_KIND,
    show_default=True,
    help="Truncate each section in the console report.",
)
@click.option("--json", "json_path", type=click.Path(path_type=Path), help="Write a JSON report.")
@click.option(
    "--junit", "junit_path", type=click.Path(path_type=Path), help="Write a JUnit XML report."
)
@click.option(
    "--html",
    "html_path",
    type=click.Path(path_type=Path),
    help="Write a standalone HTML report. One file, no network needed to read it.",
)
@click.option(
    "--out-dir",
    type=click.Path(path_type=Path),
    help="Write report.json, junit.xml and report.html here. With several configs, one "
    "subdirectory each.",
)
@click.option(
    "--junit-max-cases",
    type=int,
    default=DEFAULT_MAX_CASES,
    show_default=True,
    help="Cap the test cases per JUnit suite; the remainder is summarised in one case.",
)
@click.option(
    "--redact-literals/--no-redact-literals",
    default=True,
    help=(
        "Mask credential-shaped string literals in definition text (defaults, view bodies, "
        "check expressions, routine bodies) and in enum labels before they reach any output. "
        "On by default: a "
        "report is often uploaded as a CI artifact, and a hardcoded connection string in a "
        "function body would travel with it. Masks keep a short digest, so two different "
        "secrets still compare as different. Pass --no-redact-literals to inspect the real "
        "text locally; the captured inventory is then no longer safe to share. Redaction also "
        "changes baseline signatures, so a baseline approved in one mode will not match the "
        "other (accepted findings reappear as new; nothing is hidden)."
    ),
)
@click.pass_context
def compare_command(
    ctx: click.Context,
    config_paths: tuple[Path, ...],
    target_labels: tuple[str, ...],
    exclude_schemas: tuple[str, ...],
    fail_on: str | None,
    ignore_column_order: bool | None,
    show_cosmetic: bool,
    ignores_path: Path | None,
    no_default_ignores: bool,
    show_ignored: bool,
    skip_liquibase: bool,
    strict_changelog: bool,
    baseline_path: Path | None,
    allow_unreachable: bool,
    sequential: bool,
    max_findings_per_kind: int,
    json_path: Path | None,
    junit_path: Path | None,
    html_path: Path | None,
    out_dir: Path | None,
    junit_max_cases: int,
    redact_literals: bool,
) -> None:
    """Compare the master against its targets and report any drift.

    Exits 1 when drift is found at or above the failure threshold, and 3 when a source could not
    be inspected — a partial comparison is never reported as a clean gate.
    """
    cli_options = ctx.obj or {}
    configs = load_config_files(list(config_paths))

    worst_exit = ExitCode.OK
    for config_path, raw_config in configs:
        config = _apply_overrides(raw_config, fail_on, ignore_column_order, exclude_schemas)
        rules = _build_ignores(
            config, config_path, ignores_path, no_default_ignores=no_default_ignores
        )
        runner.require_tunnel_support(config)
        credentials = config.resolve_credentials()
        configure(
            cli_options.get("verbose", 0),
            quiet=cli_options.get("quiet", False),
            dsns=credentials.values(),
        )

        for warning in config.warnings():
            click.secho(f"warning: {warning}", fg="yellow", err=True)

        report = runner.compare(
            config,
            credentials,
            ssh_passphrases=config.resolve_ssh_passphrases(),
            targets=target_labels or None,
            sequential=sequential,
            diff_options=DiffOptions(
                ignore_column_order=config.options.ignore_column_order,
                include_owners=config.options.include_owners,
                include_comments=config.options.include_comments,
                include_grants=config.options.include_grants,
                show_cosmetic=show_cosmetic,
            ),
            ignores=rules,
            changelog_options=ChangelogOptions(strict=strict_changelog),
            skip_liquibase=skip_liquibase,
            redact_literals=redact_literals,
        )

        if baseline_path is not None:
            report = _apply_baseline(report, baseline_path)

        ConsoleReporter(max_per_kind=max_findings_per_kind, show_ignored=show_ignored).render(
            report, sys.stdout
        )
        _write_files(
            report,
            json_path=json_path,
            junit_path=junit_path,
            html_path=html_path,
            out_dir=out_dir,
            junit_max_cases=junit_max_cases,
            many=len(configs) > 1,
        )
        worst_exit = max(worst_exit, _exit_code(report, allow_unreachable=allow_unreachable))

    ctx.exit(worst_exit)


@cli.command("inventory")
@click.option(
    "-c",
    "--config",
    "config_path",
    required=True,
    type=click.Path(path_type=Path),
    help="Config file naming the source.",
)
@click.option("--source", "label", required=True, help="Label of the source to capture.")
@click.option(
    "-o",
    "--out",
    "out_path",
    required=True,
    type=click.Path(path_type=Path),
    help="Where to write the inventory JSON.",
)
@click.option("--skip-liquibase", is_flag=True, help="Do not read DATABASECHANGELOG.")
@click.option(
    "--redact-literals/--no-redact-literals",
    default=True,
    help=(
        "Mask credential-shaped string literals in definition text (defaults, view bodies, "
        "check expressions, routine bodies) and in enum labels before they reach any output. "
        "On by default: a "
        "report is often uploaded as a CI artifact, and a hardcoded connection string in a "
        "function body would travel with it. Masks keep a short digest, so two different "
        "secrets still compare as different. Pass --no-redact-literals to inspect the real "
        "text locally; the captured inventory is then no longer safe to share. Redaction also "
        "changes baseline signatures, so a baseline approved in one mode will not match the "
        "other (accepted findings reappear as new; nothing is hidden)."
    ),
)
@click.pass_context
def inventory_command(
    ctx: click.Context,
    config_path: Path,
    label: str,
    out_path: Path,
    skip_liquibase: bool,
    redact_literals: bool,
) -> None:
    """Capture one database's schema to a JSON file.

    Lets two environments be captured separately and compared later, so no single process ever
    needs credentials for both at once. The file is also what the integration tests replay, and
    what makes an offline ``diff-inventories`` possible.
    """
    cli_options = ctx.obj or {}
    config = load_config([config_path])[0]
    source = config.master if label == config.master.label else config.target(label)
    runner.require_tunnel_support(config)
    credentials = config.resolve_credentials()
    configure(
        cli_options.get("verbose", 0),
        quiet=cli_options.get("quiet", False),
        dsns=credentials.values(),
    )

    result = runner.capture(
        source,
        credentials[label],
        ssh_passphrase=config.resolve_ssh_passphrases().get(label),
        exclude_schemas=config.exclude_schemas,
        options=runner.connection_options(config),
        skip_liquibase=skip_liquibase,
        redact_literals=redact_literals,
    )
    if not result.ok or result.inventory is None:
        raise ProbeError(result.error or f"{label}: capture failed")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(result.inventory.to_json() + "\n", encoding="utf-8")
    click.echo(
        f"captured {len(result.inventory)} object(s) from {label} "
        f"across {len(result.inventory.schemas)} schema(s) to {out_path}"
    )


@cli.command("diff-inventories")
@click.argument("master_path", type=click.Path(exists=True, path_type=Path))
@click.argument("target_path", type=click.Path(exists=True, path_type=Path))
@click.option("--name", default="inventories", show_default=True, help="Report title.")
@click.option(
    "--fail-on",
    type=click.Choice(["error", "warning", "any", "never"]),
    default="error",
    show_default=True,
)
@click.option("--ignore-column-order", is_flag=True)
@click.option("--show-cosmetic", is_flag=True)
@click.option(
    "--ignores",
    "ignores_path",
    type=click.Path(exists=True, path_type=Path),
    help="Ignore ruleset, layered over the bundled defaults.",
)
@click.option("--no-default-ignores", is_flag=True)
@click.option("--show-ignored", is_flag=True)
@click.option("--strict-changelog", is_flag=True)
@click.option("--json", "json_path", type=click.Path(path_type=Path))
@click.option("--junit", "junit_path", type=click.Path(path_type=Path))
@click.option("--html", "html_path", type=click.Path(path_type=Path))
@click.option("--max-findings-per-kind", type=int, default=DEFAULT_MAX_PER_KIND, show_default=True)
@click.pass_context
def diff_inventories_command(
    ctx: click.Context,
    master_path: Path,
    target_path: Path,
    name: str,
    fail_on: str,
    ignore_column_order: bool,
    show_cosmetic: bool,
    ignores_path: Path | None,
    no_default_ignores: bool,
    show_ignored: bool,
    strict_changelog: bool,
    json_path: Path | None,
    junit_path: Path | None,
    html_path: Path | None,
    max_findings_per_kind: int,
) -> None:
    """Compare two inventory files. Needs no database and no credentials.

    This is how a production capture and a QA capture get compared when nothing may hold
    credentials for both, and how a comparison is re-run later without touching either server.
    """
    master = _load_inventory(master_path)
    target = _load_inventory(target_path)

    diff = diff_inventories(
        master,
        target,
        options=DiffOptions(ignore_column_order=ignore_column_order, show_cosmetic=show_cosmetic),
    )
    report = ComparisonReport(
        name=name,
        master_label=master.source.label,
        targets=(diff,),
        generated_at=datetime.now(UTC).isoformat(),
        tool_version=__version__,
        fail_on=fail_on,
    )

    ConsoleReporter(max_per_kind=max_findings_per_kind, show_ignored=show_ignored).render(
        report, sys.stdout
    )
    _write_files(
        report, json_path=json_path, junit_path=junit_path, html_path=html_path, out_dir=None
    )
    ctx.exit(_exit_code(report, allow_unreachable=False))


@cli.command("render")
@click.option(
    "--from",
    "report_path",
    required=True,
    type=click.Path(exists=True, path_type=Path),
    help="A JSON report written by compare or diff-inventories.",
)
@click.option("--junit", "junit_path", type=click.Path(path_type=Path))
@click.option("--json", "json_path", type=click.Path(path_type=Path))
@click.option("--html", "html_path", type=click.Path(path_type=Path))
@click.option("--console/--no-console", default=True, show_default=True)
@click.option("--max-findings-per-kind", type=int, default=DEFAULT_MAX_PER_KIND, show_default=True)
@click.option("--junit-max-cases", type=int, default=DEFAULT_MAX_CASES, show_default=True)
@click.pass_context
def render_command(
    ctx: click.Context,
    report_path: Path,
    junit_path: Path | None,
    json_path: Path | None,
    html_path: Path | None,
    console: bool,
    max_findings_per_kind: int,
    junit_max_cases: int,
) -> None:
    """Re-render a saved JSON report in another format, offline.

    The exit code is reproduced too, so a pipeline can gate on a report produced by an earlier
    job without re-running the comparison.
    """
    try:
        report = load_report(report_path.read_text(encoding="utf-8"))
    except (ValueError, KeyError) as exc:
        raise ConfigError(f"{report_path}: not a readable report ({exc})") from None

    if console:
        ConsoleReporter(max_per_kind=max_findings_per_kind).render(report, sys.stdout)
    _write_files(
        report,
        json_path=json_path,
        junit_path=junit_path,
        html_path=html_path,
        out_dir=None,
        junit_max_cases=junit_max_cases,
    )
    ctx.exit(_exit_code(report, allow_unreachable=False))


@cli.command("validate-config")
@click.option(
    "-c",
    "--config",
    "config_paths",
    multiple=True,
    required=True,
    type=click.Path(path_type=Path),
    help="Config file, or a directory of them. Repeatable.",
)
@click.option(
    "--check-env",
    is_flag=True,
    help="Also check that every referenced environment variable is set.",
)
@click.pass_context
def validate_config_command(
    ctx: click.Context, config_paths: tuple[Path, ...], check_env: bool
) -> None:
    """Check configuration without connecting to anything.

    Useful in a pipeline stage that runs before any database is reachable, and as a quick check that
    a hand-edited file is still valid.
    """
    configs = load_config_files(list(config_paths))

    for path, config in configs:
        click.echo(f"{path}: {config.name}")
        click.echo(f"  master  {config.master.label}  (${config.master.dsn_env})")
        for target in config.targets:
            mapping = f"  schema_map {target.schema_map}" if target.schema_map else ""
            click.echo(f"  target  {target.label}  (${target.dsn_env}){mapping}")

        rules = _build_ignores(config, path, None, no_default_ignores=False)
        click.echo(f"  ignore rules in force: {len(rules)}")

        for warning in config.warnings():
            click.secho(f"  warning: {warning}", fg="yellow")

        if check_env:
            # Resolving raises with every missing variable listed at once.
            config.resolve_credentials()
            click.secho("  every credential is set", fg="green")

    click.secho(f"{len(configs)} config(s) valid", fg="green")
    ctx.exit(ExitCode.OK)


@cli.command("probe")
@click.option(
    "-c",
    "--config",
    "config_path",
    required=True,
    type=click.Path(path_type=Path),
    help="Config file.",
)
@click.pass_context
def probe_command(ctx: click.Context, config_path: Path) -> None:
    """Connect to every source and report what was found, without comparing anything.

    The first thing to run against a new environment. It answers the questions that otherwise turn
    into a confusing comparison: which server version, which schemas, and where the changelog
    lives.
    """
    cli_options = ctx.obj or {}
    config = load_config([config_path])[0]
    runner.require_tunnel_support(config)
    credentials = config.resolve_credentials()
    configure(
        cli_options.get("verbose", 0),
        quiet=cli_options.get("quiet", False),
        dsns=credentials.values(),
    )

    passphrases = config.resolve_ssh_passphrases()
    statuses = [
        runner.check_connection(
            source,
            credentials[source.label],
            ssh_passphrase=passphrases.get(source.label),
            options=runner.connection_options(config),
            exclude_schemas=config.exclude_schemas,
        )
        for source in config.sources
    ]
    failed = False
    for status in statuses:
        if not status.ok:
            failed = True
            click.secho(f"{status.label}: UNREACHABLE", fg="red")
            click.echo(f"  {status.error}")
            continue
        version = (status.server_version or "").split(" ")[0]
        click.secho(f"{status.label}: PostgreSQL {version}", fg="green")
        click.echo(f"  database  {status.database} as {status.user}")
        click.echo(f"  encoding  {status.encoding}  collation {status.collation}")
        click.echo(f"  schemas   {', '.join(status.schemas) or '(none)'}")
        _echo_changelog_status(status)

    ctx.exit(ExitCode.PROBE_ERROR if failed else ExitCode.OK)


def _echo_changelog_status(status: runner.ConnectionStatus) -> None:
    if status.changelog_candidates:
        click.secho(
            "  liquibase AMBIGUOUS: "
            + ", ".join(status.changelog_candidates)
            + " — set liquibase.schema in the config",
            fg="yellow",
        )
        return
    if status.changelog is None:
        click.echo("  liquibase no changelog table found")
        return
    tag = f", last tag {status.changelog_tag}" if status.changelog_tag else ""
    click.echo(f"  liquibase {status.changelog}: {status.changelog_count} changeset(s){tag}")
    if status.changelog_locked:
        click.secho("  liquibase lock is HELD — a deployment may be in progress", fg="yellow")


def _load_inventory(path: Path) -> Inventory:
    try:
        return Inventory.from_json(path.read_text(encoding="utf-8"))
    except (ValueError, KeyError) as exc:
        raise ConfigError(f"{path}: not a readable inventory ({exc})") from None


def _write_files(
    report: ComparisonReport,
    *,
    json_path: Path | None,
    junit_path: Path | None,
    out_dir: Path | None,
    html_path: Path | None = None,
    junit_max_cases: int = DEFAULT_MAX_CASES,
    many: bool = False,
) -> None:
    """Write whichever machine-readable reports were asked for.

    ``--out-dir`` is the convenient form for CI. With several configs in one run it gives each
    its own subdirectory, so two services cannot overwrite each other's report.
    """
    targets: list[tuple[Reporter, Path]] = []
    if out_dir is not None:
        directory = out_dir / report.name if many else out_dir
        targets.append((JsonReporter(), directory / "report.json"))
        targets.append((JUnitReporter(max_cases=junit_max_cases), directory / "junit.xml"))
        targets.append((HtmlReporter(), directory / "report.html"))
    if json_path is not None:
        targets.append((JsonReporter(), json_path))
    if junit_path is not None:
        targets.append((JUnitReporter(max_cases=junit_max_cases), junit_path))
    if html_path is not None:
        targets.append((HtmlReporter(), html_path))

    for reporter, path in targets:
        render_to_path(reporter, report, path)
        click.echo(f"wrote {reporter.name} report to {path}", err=True)


def _apply_baseline(report: ComparisonReport, path: Path) -> ComparisonReport:
    """Set aside every finding an approved report already contains."""
    try:
        baseline = load_report(path.read_text(encoding="utf-8"))
    except (ValueError, KeyError) as exc:
        raise ConfigError(f"{path}: not a readable baseline report ({exc})") from None

    result = apply_baseline(report, baseline)
    click.secho(
        f"baseline {path}: {result.accepted} accepted, {result.new} new"
        + (f", {result.resolved} no longer occur" if result.resolved else ""),
        fg="cyan",
        err=True,
    )
    return result.report


def _build_ignores(
    config: ComparerConfig,
    config_path: Path,
    ignores_path: Path | None,
    *,
    no_default_ignores: bool,
) -> IgnoreRuleSet:
    """The ruleset in force for one config.

    A ``--ignores`` file wins over the config's own ``ignores_file``: the flag is what the person
    running the command asked for now, and the file is what the repository asked for in general.
    """
    project = (
        load_ignores(ignores_path)
        if ignores_path is not None
        else resolve_ignores(config, config_path)
    )
    defaults = None if no_default_ignores else load_default_ignores()
    return IgnoreRuleSet.from_config(project, defaults=defaults)


def _apply_overrides(
    config: ComparerConfig,
    fail_on: str | None,
    ignore_column_order: bool | None,
    exclude_schemas: tuple[str, ...],
) -> ComparerConfig:
    """Let command-line flags win over the config file.

    A flag is more specific than a committed file: it is what the person running the command
    asked for right now.
    """
    updates: dict[str, object] = {}
    option_updates: dict[str, object] = {}

    if fail_on is not None:
        option_updates["fail_on"] = fail_on
    if ignore_column_order is not None:
        option_updates["ignore_column_order"] = ignore_column_order
    if exclude_schemas:
        updates["exclude_schemas"] = tuple(dict.fromkeys(config.exclude_schemas + exclude_schemas))

    if option_updates:
        updates["options"] = config.options.model_copy(update=option_updates)
    if not updates:
        return config
    return config.model_copy(update=updates)


def _exit_code(report: ComparisonReport, *, allow_unreachable: bool) -> ExitCode:
    """Translate a report into the documented exit status.

    A probe failure outranks drift: reporting 1 for an incomplete comparison would let a broken
    connection masquerade as a clean gate.

    ``--allow-unreachable`` waives that for the targets that *were* reachable. It does not waive
    it when none of them was: exiting 0 after comparing nothing would report "in sync" about a
    comparison that never happened, which is the one failure mode a gate must not have.
    """
    if report.probe_failed:
        comparable = [target for target in report.targets if not target.skipped]
        if not allow_unreachable or not comparable:
            return ExitCode.PROBE_ERROR
    return ExitCode.DRIFT if report.has_drift(gate(report.fail_on)) else ExitCode.OK


def main() -> int:
    """Console-script entry point.

    Translates the exception hierarchy into the documented exit codes. ``standalone_mode`` is off
    so click does not call ``sys.exit`` itself and bypass that translation — which means click
    *returns* the code a command passed to ``ctx.exit()``, and that return value is the exit
    status. Discarding it would make every command exit 0 while printing that it had found drift.
    """
    debug = "--debug" in sys.argv
    try:
        result = cli.main(args=sys.argv[1:], standalone_mode=False)
    except click.ClickException as exc:
        exc.show()
        return ExitCode.CONFIG_ERROR
    except click.Abort:
        return ExitCode.INTERRUPTED
    except KeyboardInterrupt:
        click.echo("Interrupted.", err=True)
        return ExitCode.INTERRUPTED
    except ComparerError as exc:
        if debug:
            raise
        click.secho(str(exc), fg="red", err=True)
        return exc.exit_code
    except SystemExit as exc:
        # Defensive: a library below us may still call sys.exit().
        return exc.code if isinstance(exc.code, int) else ExitCode.OK
    return result if isinstance(result, int) else ExitCode.OK


if __name__ == "__main__":
    sys.exit(main())
