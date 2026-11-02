"""Non-blocking OS advisory locks on a one-byte region (Windows and POSIX).

Shared by the learned-ETA stats file and the GPU transcription slot. The OS
drops the lock when its holder dies, so no stale-lock heuristics are needed.
"""
import errno
import os

if os.name == "nt":
    import msvcrt
else:
    import fcntl

# Errors that mean "another holder has it". Anything else (a file system
# without byte-range locks, EINVAL/ENOLCK) is not contention.
BUSY_ERRNOS = frozenset({
    errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK,
    getattr(errno, "EDEADLK", -1), getattr(errno, "EDEADLOCK", -1),
})


def try_lock(descriptor):
    """True when locked, False when another holder has it.

    Other lock errors raise OSError; each caller decides what that means."""
    try:
        if os.name == "nt":
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in BUSY_ERRNOS:
            return False
        raise


def unlock(descriptor):
    """Release a lock taken with try_lock; errors are ignored."""
    try:
        if os.name == "nt":
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    except OSError:
        pass
