"""The joining of two macOS bundles into a universal one.

`lipo` itself is a macOS tool and is replaced by a recorder here; what is held is the decision about
which files get combined, which get copied, and that nothing is lost on the way.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "macos_universal", ROOT / "packaging/macos_universal.py"
)
assert spec is not None and spec.loader is not None
macos_universal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(macos_universal)

MACHO_ARM = bytes.fromhex("cffaedfe") + b"arm64 code"
MACHO_X64 = bytes.fromhex("cffaedfe") + b"x86_64 code"


def bundle(root: Path, binary: bytes, *, extra: dict[str, bytes] | None = None) -> Path:
    files = {
        "Contents/Info.plist": b"<plist/>",
        "Contents/MacOS/app": binary,
        "Contents/MacOS/libfoo.dylib": binary,
        "Contents/Resources/data.txt": b"same",
        **(extra or {}),
    }
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (root / "Contents/MacOS/app").chmod(0o755)
    return root


def fake_lipo(first: Path, second: Path, out: Path) -> None:
    out.write_bytes(b"FAT:" + first.read_bytes() + b"|" + second.read_bytes())


def test_a_mach_o_file_is_recognised_by_its_magic_number(tmp_path):
    assert macos_universal.is_thin_macho(bundle(tmp_path / "a", MACHO_ARM) / "Contents/MacOS/app")
    assert not macos_universal.is_thin_macho(tmp_path / "a/Contents/Info.plist")
    assert not macos_universal.is_thin_macho(tmp_path / "missing")


def test_binaries_in_both_bundles_are_combined_and_everything_else_is_copied(tmp_path):
    first = bundle(tmp_path / "arm64", MACHO_ARM)
    second = bundle(tmp_path / "x64", MACHO_X64)
    out = tmp_path / "out"
    counts = macos_universal.merge(first, second, out, lipo=fake_lipo)
    assert counts["merged"] == 2
    assert (out / "Contents/MacOS/app").read_bytes() == b"FAT:" + MACHO_ARM + b"|" + MACHO_X64
    assert (out / "Contents/Resources/data.txt").read_bytes() == b"same"
    assert (out / "Contents/Info.plist").read_bytes() == b"<plist/>"


def test_the_executable_bit_survives_the_merge(tmp_path):
    first = bundle(tmp_path / "arm64", MACHO_ARM)
    second = bundle(tmp_path / "x64", MACHO_X64)
    out = tmp_path / "out"
    macos_universal.merge(first, second, out, lipo=fake_lipo)
    assert os.access(out / "Contents/MacOS/app", os.X_OK)


def test_a_binary_present_in_only_one_bundle_is_kept_not_dropped(tmp_path):
    first = bundle(tmp_path / "arm64", MACHO_ARM, extra={"Contents/MacOS/only-arm.so": MACHO_ARM})
    second = bundle(tmp_path / "x64", MACHO_X64, extra={"Contents/MacOS/only-x64.so": MACHO_X64})
    out = tmp_path / "out"
    counts = macos_universal.merge(first, second, out, lipo=fake_lipo)
    assert (out / "Contents/MacOS/only-arm.so").read_bytes() == MACHO_ARM
    assert (out / "Contents/MacOS/only-x64.so").read_bytes() == MACHO_X64
    assert counts["only_in_second"] == 1


def test_symlinks_are_recreated_not_followed(tmp_path):
    first = bundle(tmp_path / "arm64", MACHO_ARM)
    second = bundle(tmp_path / "x64", MACHO_X64)
    (first / "Contents/MacOS/current").symlink_to("app")
    out = tmp_path / "out"
    macos_universal.merge(first, second, out, lipo=fake_lipo)
    assert (out / "Contents/MacOS/current").is_symlink()
    assert (out / "Contents/MacOS/current").readlink() == Path("app")


def test_an_existing_output_is_refused(tmp_path):
    first = bundle(tmp_path / "arm64", MACHO_ARM)
    second = bundle(tmp_path / "x64", MACHO_X64)
    (tmp_path / "out").mkdir()
    with pytest.raises(FileExistsError):
        macos_universal.merge(first, second, tmp_path / "out", lipo=fake_lipo)
