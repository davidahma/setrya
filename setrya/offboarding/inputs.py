"""Read bounded evidence and atomically publish valid gate decisions.

Adapted from Axiom v0.3.2 for Setrya's offboarding interface.
"""

import hashlib
import json
import os
from pathlib import Path
import stat
from tempfile import NamedTemporaryFile

MAX_JSON_BYTES = 1024 * 1024


class InvalidReport(ValueError):
    """Input did not match the public evidence format."""


def require(condition, message):
    if not condition:
        raise InvalidReport(message)


def keys_match(value, keys):
    return isinstance(value, dict) and set(value) == set(keys)


def regular_file_opener(path, flags):
    # A nonblocking open lets us inspect the actual descriptor even for a FIFO.
    fd = os.open(path, flags | getattr(os, "O_NONBLOCK", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise InvalidReport("Input must be a regular file.")
    except BaseException:
        os.close(fd)
        raise
    return fd


def unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidReport("Duplicate JSON field.")
        result[key] = value
    return result


def read_regular_bytes(path, label, limit=MAX_JSON_BYTES):
    try:
        with open(path, "rb", opener=regular_file_opener) as stream:
            raw = stream.read(limit + 1)
    except OSError:
        raise InvalidReport(f"Could not read {label} input.") from None
    if len(raw) > limit:
        raise InvalidReport(f"The {label} input exceeds the {limit // (1024 * 1024)} MiB limit.")
    return raw


def read_json(path, label):
    raw = read_regular_bytes(path, label)
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_fields)
    except (ValueError, RecursionError):
        raise InvalidReport(
            f"Invalid {label} JSON. Use UTF-8 and unique fields."
        ) from None
    return data, hashlib.sha256(raw).hexdigest()


def save(report, path):
    """Atomically replace a regular gate decision after all inputs were validated."""
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise InvalidReport("Gate output must be a regular file.")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as stream:
            temporary = Path(stream.name)
            json.dump(report, stream, indent=2)
            stream.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def publish_new(data, path, *, max_bytes=MAX_JSON_BYTES):
    raw = (json.dumps(data, indent=2) + "\n").encode("utf-8")
    return publish_new_bytes(raw, path, max_bytes=max_bytes)


def publish_new_bytes(raw, path, *, max_bytes=MAX_JSON_BYTES):
    """Publish exact, validated bytes without replacing existing evidence."""
    if len(raw) > max_bytes:
        raise InvalidReport(f"Output exceeds the {max_bytes // (1024 * 1024)} MiB replay limit.")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        # Link a complete file. An existing target, including a dangling symlink
        # or an input alias, makes this fail atomically without replacing it.
        os.link(temporary, path)
    except FileExistsError:
        raise InvalidReport(
            "Output already exists. Choose a new output path."
        ) from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return hashlib.sha256(raw).hexdigest()
