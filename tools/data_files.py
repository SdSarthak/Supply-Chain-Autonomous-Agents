"""Loading and writing the JSON master data every tool reads.

The four files under `data/` are the ground truth for the whole network — the
orchestrator reads `inventory.json` and `suppliers.json` before a single agent
starts. When one of them is missing or truncated the stock library raises
`FileNotFoundError` / `JSONDecodeError`, and `json.JSONDecodeError` does not
even name the file it choked on. This module turns both into one error that
says which file is wrong and how to regenerate it.
"""

import json
import os
import tempfile

REGENERATE_HINT = "regenerate it with: python data/generate_mock_data.py"


class DataFileError(RuntimeError):
    """A master-data file is missing, unreadable or not the expected shape."""


def load_json_records(path: str, label: str) -> list:
    """Read a master-data file that must contain a JSON array of objects."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError as e:
        raise DataFileError(
            f"{label} data file is missing: {path} — {REGENERATE_HINT}"
        ) from e
    except json.JSONDecodeError as e:
        raise DataFileError(
            f"{label} data file is not valid JSON: {path} "
            f"(line {e.lineno}, column {e.colno}: {e.msg}) — {REGENERATE_HINT}"
        ) from e
    except OSError as e:
        raise DataFileError(f"{label} data file could not be read: {path} — {e}") from e

    if not isinstance(data, list):
        raise DataFileError(
            f"{label} data file must contain a JSON array, "
            f"got {type(data).__name__}: {path} — {REGENERATE_HINT}"
        )
    return data


def atomic_write_json(path: str, payload) -> None:
    """Write JSON so readers never observe a partially written file.

    The temp file is created in the destination directory so `os.replace` is a
    same-filesystem rename, which is atomic on both POSIX and Windows.
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp-",
                                    suffix=os.path.basename(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        # Never leave the temp file behind on a failed or interrupted write.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
