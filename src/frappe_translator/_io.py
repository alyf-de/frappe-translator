"""Internal I/O utilities."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


def atomic_json_write(path: Path, data: object, **kwargs: Any) -> None:
    """Write JSON atomically: write to temp file, then rename.

    This prevents data corruption if the process is killed mid-write.
    On POSIX systems, os.replace is atomic within the same filesystem.
    """
    kwargs.setdefault("ensure_ascii", False)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, **kwargs)
        os.replace(tmp, str(path))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
