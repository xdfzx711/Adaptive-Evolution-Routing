"""Drain CLI events while bounding shutdown after an authoritative turn end."""

from dataclasses import dataclass
import json
from queue import Empty, Queue
from threading import Thread
from time import monotonic


@dataclass(frozen=True)
class CliOutput:
    stdout: str
    stderr: str
    timed_out: bool
    terminal_shutdown: bool


def capture_cli(process, public_input, timeout, stop, shutdown_grace=2.0):
    stdout, stderr, events = [], [], Queue()

    def drain(stream, target, name):
        try:
            for line in stream:
                target.append(line)
                if name == "stdout":
                    events.put(line)
        finally:
            stream.close()
            events.put(("eof", name))

    threads = [Thread(target=drain, args=(process.stdout, stdout, "stdout"), daemon=True),
               Thread(target=drain, args=(process.stderr, stderr, "stderr"), daemon=True)]

    def feed():
        try:
            process.stdin.write(public_input)
        except (BrokenPipeError, OSError):
            pass  # Startup failure can close stdin before consuming a task.
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass

    threads.append(Thread(target=feed, daemon=True))
    for thread in threads:
        thread.start()
    deadline, terminal_at, exit_at, finished = monotonic() + timeout, None, None, set()
    timed_out, terminal_shutdown = False, False
    try:
        while True:
            now = monotonic()
            if process.poll() is not None:
                exit_at = exit_at or now
                if len(finished) == 2:
                    break
            if now >= deadline or (terminal_at is not None and now >= terminal_at + shutdown_grace):
                timed_out = terminal_at is None
                if process.poll() is None:
                    terminal_shutdown = terminal_at is not None
                    stop(process)
                break
            if exit_at is not None and now >= exit_at + shutdown_grace:
                break
            try:
                event = events.get(timeout=min(0.05, max(0.001, deadline - now)))
            except Empty:
                continue
            if isinstance(event, tuple):
                finished.add(event[1])
                continue
            try:
                parsed = json.loads(event)
            except (ValueError, TypeError):
                continue
            if isinstance(parsed, dict) and parsed.get("type") in {"turn.completed", "turn.failed"}:
                terminal_at = terminal_at or monotonic()
        process.wait(timeout=10)
    finally:
        if process.poll() is None:
            stop(process)
            process.wait(timeout=10)
        for thread in threads:
            thread.join(timeout=1)
    return CliOutput("".join(stdout), "".join(stderr), timed_out, terminal_shutdown)
