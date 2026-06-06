"""Subprocess driver for P3 bot evaluation.

Spawns a bot command as a subprocess, drives the streaming protocol
(INIT → READY → BAR* → ORDER* → END), and returns the list of `want` ints.

Design notes
============
* **No deadlock** — a background reader thread drains stdout continuously and
  pushes complete lines into a ``queue.Queue``.  The main loop never blocks on
  a write while the child is blocked on a write back, so the pipe buffers
  cannot fill up.
* **No leaked child** — a ``try/finally`` block always terminates then kills
  the child process and joins the reader thread.
* **Cross-platform** — no ``signal.alarm`` (not thread-safe; unavailable on
  Windows); no ``select``/``poll`` (platform-specific behaviour in text mode).
  The queue + timeout pattern works identically on macOS and Linux.
* **Bounded** — total bytes read are capped at ``max_bytes`` (default 1 MB).
  Any bot that spews garbage cannot allocate unbounded memory in the driver.
"""
from __future__ import annotations

import json
import queue
import subprocess
import tempfile
import threading
import time
from typing import Any

from tyani_tolkai import bot_protocol as bp


class BotProtocolError(Exception):
    """Raised when the bot violates the P3 streaming protocol or times out."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _kill_quietly(proc: subprocess.Popen) -> None:
    """Watchdog callback: kill the child, swallowing any error."""
    try:
        proc.kill()
    except Exception:  # noqa: BLE001
        pass


def _reader_thread(
    proc: subprocess.Popen,
    line_q: "queue.Queue[str | None]",
    byte_q: "queue.Queue[bool]",
    max_bytes: int,
) -> None:
    """Background thread: read stdout lines, push to queue.

    Pushes ``None`` as a sentinel when the stream is exhausted or an error
    occurs.  Pushes ``True`` to *byte_q* if the byte cap is exceeded.
    """
    total = 0
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            total += len(raw.encode("utf-8", errors="replace"))
            if total > max_bytes:
                byte_q.put(True)
                return
            line_q.put(raw)
    except Exception:  # noqa: BLE001
        pass
    finally:
        line_q.put(None)  # sentinel


def _read_line(
    line_q: "queue.Queue[str | None]",
    byte_q: "queue.Queue[bool]",
    per_read_timeout: float,
    deadline: float,
) -> str:
    """Pop one line from the queue, respecting per-read and global timeouts.

    Returns the stripped line string.
    Raises ``BotProtocolError`` on timeout, byte cap exceeded, or EOF.
    """
    # Check byte cap first (non-blocking)
    try:
        byte_q.get_nowait()
        raise BotProtocolError("evaluation failed")
    except queue.Empty:
        pass

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise BotProtocolError("evaluation failed")

    wait = min(per_read_timeout, remaining)
    try:
        line = line_q.get(timeout=wait)
    except queue.Empty:
        raise BotProtocolError("evaluation failed")

    if line is None:
        # EOF or reader error
        raise BotProtocolError("evaluation failed")

    return line.rstrip("\n\r")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def drive_bot(
    cmd: list[str],
    bars: list[tuple[float, float, float, float, float]],
    *,
    params: dict[str, Any],
    per_read_timeout: float,
    total_timeout: float,
    max_bytes: int = 1_000_000,
) -> list[int]:
    """Spawn *cmd* as a subprocess and drive the P3 streaming protocol.

    Parameters
    ----------
    cmd:
        Command + arguments to launch the bot.
    bars:
        Sequence of ``(o, h, l, c, v)`` tuples.
    params:
        Arbitrary parameters forwarded in the ``init`` message.
    per_read_timeout:
        Maximum seconds to wait for a single response line.
    total_timeout:
        Wall-clock budget for the entire conversation.
    max_bytes:
        Maximum total bytes accepted from the bot's stdout (default 1 MB).

    Returns
    -------
    list[int]
        ``want`` values (one per bar), each in ``{-1, 0, 1}``.

    Raises
    ------
    BotProtocolError
        On any protocol violation, timeout, byte cap, or unexpected EOF.
    """
    deadline = time.monotonic() + total_timeout

    line_q: "queue.Queue[str | None]" = queue.Queue()
    byte_q: "queue.Queue[bool]" = queue.Queue()

    orders: list[int] = []

    # The TemporaryDirectory encloses the whole try/finally so it is only
    # removed AFTER the child process is gone.
    with tempfile.TemporaryDirectory() as cwd:
        proc: subprocess.Popen | None = None
        reader: threading.Thread | None = None
        watchdog: threading.Timer | None = None
        try:
            # Popen is INSIDE the try so a bad command (FileNotFoundError,
            # PermissionError, ...) is masked as a generic BotProtocolError
            # and never leaks our path/argv.
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                close_fds=True,
                cwd=cwd,
            )

            # Backstop for write-stall: if the bot stops reading stdin, our
            # stdin.write() can block past the pipe buffer, bypassing the
            # read-deadline. The watchdog kills the child at the global
            # deadline regardless of whether we are blocked in write() or
            # read(); a blocked write() then raises BrokenPipeError ->
            # caught below -> BotProtocolError.
            watchdog = threading.Timer(total_timeout, _kill_quietly, args=(proc,))
            watchdog.daemon = True
            watchdog.start()

            reader = threading.Thread(
                target=_reader_thread,
                args=(proc, line_q, byte_q, max_bytes),
                daemon=True,
            )
            reader.start()

            assert proc.stdin is not None

            # ── INIT ──────────────────────────────────────────────────────
            if time.monotonic() >= deadline:
                raise BotProtocolError("evaluation failed")
            init_msg = bp.encode(
                {"type": bp.INIT, "params": params, "schema_version": "1"}
            )
            proc.stdin.write(init_msg)
            proc.stdin.flush()

            raw = _read_line(line_q, byte_q, per_read_timeout, deadline)
            try:
                msg = bp.decode(raw)
            except (ValueError, json.JSONDecodeError):
                raise BotProtocolError("evaluation failed")
            if msg.get("type") != bp.READY:
                raise BotProtocolError("evaluation failed")

            # ── BAR loop ──────────────────────────────────────────────────
            for i, (o, h, l, c, v) in enumerate(bars):
                if time.monotonic() >= deadline:
                    raise BotProtocolError("evaluation failed")
                bar_msg = bp.encode(
                    {"type": bp.BAR, "n": i, "o": o, "h": h, "l": l, "c": c, "v": v}
                )
                proc.stdin.write(bar_msg)
                proc.stdin.flush()

                raw = _read_line(line_q, byte_q, per_read_timeout, deadline)
                try:
                    msg = bp.decode(raw)
                except (ValueError, json.JSONDecodeError):
                    raise BotProtocolError("evaluation failed")

                if msg.get("type") != bp.ORDER:
                    raise BotProtocolError("evaluation failed")
                if msg.get("n") != i:
                    raise BotProtocolError("evaluation failed")
                want = msg.get("want")
                if want not in (-1, 0, 1):
                    raise BotProtocolError("evaluation failed")

                orders.append(want)

            # ── END ───────────────────────────────────────────────────────
            if time.monotonic() >= deadline:
                raise BotProtocolError("evaluation failed")
            proc.stdin.write(bp.encode({"type": bp.END}))
            proc.stdin.flush()
            proc.stdin.close()

            return orders

        except BotProtocolError:
            raise

        except Exception:
            raise BotProtocolError("evaluation failed")

        finally:
            if watchdog is not None:
                watchdog.cancel()

            if proc is not None:
                # Close our pipe ends first so the child sees EOF / broken pipe.
                for stream in (proc.stdin, proc.stdout):
                    try:
                        if stream:
                            stream.close()
                    except Exception:  # noqa: BLE001
                        pass
                # Always terminate the child — never leave it running.
                try:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3.0)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        try:
                            proc.wait(timeout=2.0)
                        except subprocess.TimeoutExpired:
                            pass
                except Exception:  # noqa: BLE001
                    pass

            # Join the reader thread so the thread is fully cleaned up.
            if reader is not None:
                reader.join(timeout=5.0)
