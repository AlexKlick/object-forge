from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .io_utils import ensure_dir


@dataclass
class CommandResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str


class RuntimeErrorWithContext(RuntimeError):
    pass


def run_command(
    command: Sequence[str],
    workdir: str | Path | None = None,
    log_path: str | Path | None = None,
    env: dict[str, str] | None = None,
) -> CommandResult:
    logging.info("Running command: %s", " ".join(command))
    completed = subprocess.run(
        list(command),
        cwd=str(workdir) if workdir else None,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    result = CommandResult(
        command=list(command),
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
    if log_path:
        log_file = Path(log_path)
        ensure_dir(log_file.parent)
        log_file.write_text(
            f"$ {' '.join(command)}\n\nSTDOUT:\n{result.stdout}\n\nSTDERR:\n{result.stderr}\n",
            encoding="utf-8",
        )
    if result.returncode != 0:
        raise RuntimeErrorWithContext(
            f"Command failed with code {result.returncode}: {' '.join(command)}\n{result.stderr}"
        )
    return result
