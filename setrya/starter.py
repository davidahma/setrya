"""Install an editable pytest test into an application checkout."""

from importlib.resources import files
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from .drill import InputError


def write_endpoint_test(target: Path) -> None:
    if target.suffix != ".py":
        raise InputError("--output needs a Python test filename such as tests/test_setrya_endpoint.py")
    template = files("setrya.examples").joinpath("endpoint_starter.py").read_bytes()
    temporary = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=target.parent, prefix=".setrya-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(template)
        # Link the complete file only if no other process has claimed the name.
        os.link(temporary, target)
    except FileExistsError:
        raise InputError(f"{target} already exists. Choose a new file with --output.") from None
    except OSError:
        raise InputError(f"Could not create {target}. Check the output directory and its permissions.") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
