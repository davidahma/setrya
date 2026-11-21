"""Install an editable three-hook erasure drill next to a LangGraph project."""

from importlib.resources import files
from pathlib import Path


INVENTORY = '''version = 1

[[systems]]
name = "checkpoints"
owner = "app-team"
role = "checkpoint"
approved_labels = ["restricted"]
retention_days = 0
adapter = "checkpoints"

[[systems]]
name = "memory"
owner = "app-team"
role = "memory"
approved_labels = ["restricted"]
retention_days = 0
adapter = "memory"
'''


def init_erasure(destination: Path, *, shared=False):
    destination = Path(destination)
    resources = files("setrya.erasure")
    if shared:
        destination.mkdir(parents=True, exist_ok=False)
        (destination / "test_erasure.py").write_bytes(resources.joinpath("shared_starter.py").read_bytes())
        (destination / "answer_cache.py").write_bytes(resources.joinpath("starter_cache.py").read_bytes())
        return destination
    template = resources.joinpath("starter_template.py").read_bytes()
    destination.mkdir(parents=True, exist_ok=False)
    for filename, body in (("test_erasure.py", template), ("inventory.toml", INVENTORY.encode())):
        with (destination / filename).open("xb") as output:
            output.write(body)
    return destination
