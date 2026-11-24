"""
A dead log pipe must not break requests (2026-11-24).

When the container's conmon died, every print() raised BrokenPipeError and
every BFF call answered 500. PipeSafeStream swallows that instead.
"""
from __future__ import annotations

import io
import os

import pytest

from service import PipeSafeStream


def _dead_pipe_writer():
    read_fd, write_fd = os.pipe()
    os.close(read_fd)  # nobody reads: the next write gets EPIPE
    return os.fdopen(write_fd, "w", buffering=1)


def test_raw_dead_pipe_raises_without_the_guard():
    stream = _dead_pipe_writer()
    with pytest.raises(BrokenPipeError):
        stream.write("hello\n")
        stream.flush()


def test_guard_swallows_broken_pipe_and_print_keeps_working():
    guarded = PipeSafeStream(_dead_pipe_writer())
    print("Registering BFF class: SessionService", file=guarded)
    print("and again", file=guarded, flush=True)
    assert guarded.broken
    assert guarded.write("x") == 1


def test_guard_passes_output_through_while_healthy():
    target = io.StringIO()
    guarded = PipeSafeStream(target)
    print("hello", file=guarded, flush=True)
    assert target.getvalue() == "hello\n"
    assert not guarded.broken
    assert guarded.isatty() is False  # other attributes delegate


def test_guard_survives_a_closed_stream():
    target = io.StringIO()
    guarded = PipeSafeStream(target)
    target.close()
    guarded.write("late line\n")
    guarded.flush()
    assert guarded.broken
