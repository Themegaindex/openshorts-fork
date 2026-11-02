import errno
import os

import pytest

import advisory_lock


@pytest.mark.parametrize("code,busy", [("EACCES", True), ("EAGAIN", True), ("EINVAL", False), ("ENOLCK", False)])
def test_try_lock_only_reports_busy_for_contention(monkeypatch, code, busy):
    def failing(*_args):
        raise OSError(getattr(errno, code), code)

    if os.name == "nt":
        monkeypatch.setattr(advisory_lock.msvcrt, "locking", failing)
    else:
        monkeypatch.setattr(advisory_lock.fcntl, "flock", failing)
    if busy:
        assert advisory_lock.try_lock(0) is False
    else:
        with pytest.raises(OSError):
            advisory_lock.try_lock(0)


def test_second_handle_is_busy_until_the_first_unlocks(tmp_path):
    path = str(tmp_path / "slot.lock")
    first = os.open(path, os.O_CREAT | os.O_RDWR)
    second = os.open(path, os.O_CREAT | os.O_RDWR)
    try:
        assert advisory_lock.try_lock(first) is True
        assert advisory_lock.try_lock(second) is False
        advisory_lock.unlock(first)
        assert advisory_lock.try_lock(second) is True
        advisory_lock.unlock(second)
    finally:
        os.close(first)
        os.close(second)
