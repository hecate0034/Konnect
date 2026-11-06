"""Serialize local edits and explicit session release across MCP worker processes."""
from contextlib import contextmanager


@contextmanager
def edit_lock(state):
    import msvcrt
    from .sessions import PadsError
    with (state / 'edits.lock').open('a+b') as handle:
        try:
            handle.seek(0)
            if not handle.read(1):
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise PadsError('edit_busy', 'Another edit operation is in progress') from exc
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
