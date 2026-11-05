"""Human-readable console output plus structured JSON-lines log files.

All messages pass through ``safety.redact`` before being written anywhere so
tokens or credentials that appear in command output never reach disk.
"""

from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from safety import redact


class AgentLogger:
    def __init__(self, log_file: Path | None = None, verbose: bool = False, stream=None):
        self.log_file = log_file
        self.verbose = verbose
        self.stream = stream or sys.stdout
        self._lock = threading.Lock()
        self.context: dict[str, Any] = {}
        if log_file:
            log_file.parent.mkdir(parents=True, exist_ok=True)

    def bind(self, **context: Any) -> None:
        self.context.update({k: v for k, v in context.items() if v is not None})

    def unbind(self, *keys: str) -> None:
        for key in keys:
            self.context.pop(key, None)

    def _emit(self, level: str, message: str, data: dict[str, Any] | None) -> None:
        message = redact(message)
        now = datetime.now(timezone.utc)
        with self._lock:
            if level != "DEBUG" or self.verbose:
                local = now.astimezone().strftime("%H:%M:%S")
                prefix = "" if level == "INFO" else f"{level}: "
                print(f"[{local}] {prefix}{message}", file=self.stream, flush=True)
            if self.log_file:
                record = {"ts": now.isoformat(), "level": level, "message": message, **self.context}
                if data:
                    record["data"] = json.loads(redact(json.dumps(data, default=str)))
                with self.log_file.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, default=str) + "\n")

    def debug(self, message: str, **data: Any) -> None:
        self._emit("DEBUG", message, data)

    def info(self, message: str, **data: Any) -> None:
        self._emit("INFO", message, data)

    def warning(self, message: str, **data: Any) -> None:
        self._emit("WARNING", message, data)

    def error(self, message: str, **data: Any) -> None:
        self._emit("ERROR", message, data)

    def raw(self, text: str) -> None:
        """Print a block (reports, tables) without a timestamp prefix."""
        with self._lock:
            print(redact(text), file=self.stream, flush=True)
