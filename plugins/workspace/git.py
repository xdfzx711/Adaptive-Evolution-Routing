"""Small synchronous Git boundary; never runs a shell or project code."""

from pathlib import Path
import subprocess

from aer_core.errors import ExecutionError


def git(directory: Path, *args: str, timeout: float = 120.0, binary: bool = False):
    try:
        result = subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.quotepath=false",
            "-c", "core.hooksPath=", "-c", "commit.gpgsign=false", "-C", str(directory), *args],
            capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ExecutionError(f"Git operation failed: {type(error).__name__}", stage="initial_execution") from error
    if result.returncode:
        raise ExecutionError("Git operation failed: " + result.stderr.decode("utf-8", "replace")[-1500:], stage="initial_execution")
    return result.stdout if binary else result.stdout.decode("utf-8", "strict")
