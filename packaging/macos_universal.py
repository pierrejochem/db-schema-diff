#!/usr/bin/env python3
"""Merge two single-architecture macOS app bundles into one universal bundle.

Nuitka compiles for the architecture it runs on, and its Python extension modules are fetched as
per-architecture wheels, so a universal build is two native builds -- one on an Intel runner, one
on Apple silicon -- joined afterwards. This does the joining: every Mach-O file that exists in both
bundles is combined with `lipo -create`, and every other file is taken as it is. The bundles are
laid out identically because they were built from the same sources by the same Nuitka.

    python3 packaging/macos_universal.py build-x64/app.app build-arm64/app.app out/app.app

The result carries no valid signature -- `lipo` invalidates the ones inside -- so the caller
signs it again, ad hoc at the least: an arm64 binary with no signature will not run at all.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

#: The first four bytes of a thin Mach-O file, in either byte order and both word sizes. A fat
#: file (0xcafebabe) is not listed: if one turns up in a bundle built on one architecture it is
#: already universal and is taken as it is.
THIN_MAGIC = {
    bytes.fromhex("feedface"),
    bytes.fromhex("feedfacf"),
    bytes.fromhex("cefaedfe"),
    bytes.fromhex("cffaedfe"),
}

Lipo = Callable[[Path, Path, Path], None]


def is_thin_macho(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(4) in THIN_MAGIC
    except OSError:
        return False


def run_lipo(first: Path, second: Path, out: Path) -> None:
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["lipo", "-create", str(first), str(second), "-output", str(out)],  # noqa: S607
        check=True,
    )


def merge(first: Path, second: Path, out: Path, lipo: Lipo = run_lipo) -> dict[str, int]:
    """Write the merge of two bundles to ``out``. Returns how many files went each way."""
    if out.exists():
        raise FileExistsError(out)
    counts = {"merged": 0, "copied": 0, "only_in_second": 0, "links": 0}
    for source in sorted(first.rglob("*")):
        relative = source.relative_to(first)
        target = out / relative
        if source.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(source.readlink())
            counts["links"] += 1
        elif source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            other = second / relative
            if is_thin_macho(source) and other.is_file() and is_thin_macho(other):
                lipo(source, other, target)
                shutil.copymode(source, target)
                counts["merged"] += 1
            else:
                shutil.copy2(source, target)
                counts["copied"] += 1
    for extra in sorted(second.rglob("*")):
        relative = extra.relative_to(second)
        if not (first / relative).exists() and not (first / relative).is_symlink():
            target = out / relative
            if extra.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif extra.is_symlink():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(extra.readlink())
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(extra, target)
                counts["only_in_second"] += 1
    return counts


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    first, second, out = (Path(a) for a in argv)
    for bundle in (first, second):
        if not bundle.is_dir():
            print(f"not a directory: {bundle}", file=sys.stderr)
            return 2
    counts = merge(first, second, out)
    print(
        f"merged {counts['merged']} Mach-O files, copied {counts['copied']}, "
        f"{counts['only_in_second']} only in the second bundle, {counts['links']} symlinks"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
