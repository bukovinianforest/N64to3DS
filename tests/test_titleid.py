"""Title-id registry: stable ids, collision probing, and locking without fcntl (Windows)."""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.n64to3ds import titleid  # noqa: E402
from tools.n64to3ds.errors import RomError  # noqa: E402

SHA_A = "aa" * 32
SHA_B = "bb" * 32


@pytest.fixture
def data_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return tmp_path


def _load_without_fcntl(monkeypatch, locking):
    """Import a fresh copy of titleid as Windows CPython sees it: no fcntl, msvcrt present."""

    msvcrt = types.ModuleType("msvcrt")
    msvcrt.LK_UNLCK, msvcrt.LK_LOCK = 0, 1
    msvcrt.locking = locking
    monkeypatch.setitem(sys.modules, "fcntl", None)
    monkeypatch.setitem(sys.modules, "msvcrt", msvcrt)
    spec = importlib.util.spec_from_file_location("tools.n64to3ds._titleid_without_fcntl", titleid.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.fcntl is None
    return module


def test_registry_is_stable_and_probes_past_collisions(data_home):
    full, unique, warnings = titleid.choose_title_id(SHA_A, "a.z64")
    assert warnings == []
    assert unique == titleid.derive_unique_id(SHA_A)
    assert titleid.choose_title_id(SHA_A, "a.z64")[:2] == (full, unique)

    with pytest.raises(RomError):
        titleid.choose_title_id(SHA_B, "b.z64", explicit=titleid.format_title_id(full))

    # Claim B's derived id for A; B must then move to the next free id.
    b_unique = titleid.derive_unique_id(SHA_B)
    titleid.choose_title_id(SHA_A, "a.z64", explicit=f"{b_unique:05X}")
    expected = (b_unique + 1) & titleid.UNIQUE_MASK or 1
    assert titleid.choose_title_id(SHA_B, "b.z64")[1] == expected


def test_registry_locks_with_msvcrt_when_fcntl_is_missing(data_home, monkeypatch):
    calls = []
    module = _load_without_fcntl(
        monkeypatch, lambda fd, mode, nbytes: calls.append((mode, nbytes, os.lseek(fd, 0, os.SEEK_CUR)))
    )
    lock = module.registry_path().with_suffix(".lock")
    lock.parent.mkdir(parents=True)
    lock.write_text("x")  # "a+" opens at EOF; the lock must still cover byte 0.

    full, unique, warnings = module.choose_title_id(SHA_A, "a.z64")

    assert warnings == []
    assert unique == titleid.derive_unique_id(SHA_A)
    assert calls == [(1, 1, 0), (0, 1, 0)]
    assert format(full, "016X") in module.registry_path().read_text(encoding="utf-8")


def test_registry_falls_back_when_msvcrt_lock_times_out(data_home, monkeypatch):
    def locking(fd, mode, nbytes):
        raise PermissionError(36, "Resource deadlock avoided")

    module = _load_without_fcntl(monkeypatch, locking)

    _, unique, warnings = module.choose_title_id(SHA_A, "a.z64")

    assert unique == titleid.derive_unique_id(SHA_A)
    assert len(warnings) == 1 and "registry unavailable" in warnings[0]
    assert not module.registry_path().exists()
