"""Install an editable v3 subject-erasure drill next to the application."""
from importlib.resources import files
from pathlib import Path


def init_erasure(destination: Path, *, shared=True):
    """Create the current starter in a fresh directory."""
    if not shared:
        raise ValueError("The v2 starter was retired. Use the v3 starter for new runs.")
    destination = Path(destination)
    resources = files("setrya.erasure")
    destination.mkdir(parents=True, exist_ok=False)
    for source, name in (("shared_starter.py", "test_erasure.py"), ("starter_cache.py", "answer_cache.py")):
        (destination / name).write_bytes(resources.joinpath(source).read_bytes())
    return destination
