"""Keep the first-PR instructions, downloaded workflow and generated starter in sync."""
from pathlib import Path
import re
import unittest

from setrya import __version__


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = "docs/examples/first-pr/setrya.yml"


def installation_block(path):
    blocks = re.findall(r"```sh\n(.*?)\n```", (ROOT / path).read_text(), re.S)
    return next(block for block in blocks if "git add " in block)


class FirstPrInstructions(unittest.TestCase):
    def test_two_committed_files_match_the_release_workflow(self):
        commands = installation_block("README.md")
        self.assertEqual(commands, installation_block("docs/github-action.md"))
        name = "setrya-case"
        starter = f"{name}/test_erasure.py"
        destination = ".github/workflows/setrya.yml"
        self.assertIn(f"setrya init --output {name}\n", commands)
        self.assertIn(f"setrya doctor --starter {starter} --mode deletion\n", commands)
        self.assertIn(f"git add {starter} {destination}\n", commands)
        self.assertIn(f"/v{__version__}/{TEMPLATE} -o {destination}\n", commands)
        workflow = (ROOT / TEMPLATE).read_text()
        for text in (commands, workflow):
            self.assertIn(f"python -m pip install setrya=={__version__}\n", text)
        self.assertEqual(re.findall(r"uses: davidahma/setrya@(\S+)", workflow), [f"v{__version__}"] * 2)
        self.assertEqual(re.findall(r"^\s+starter: (\S+)$", workflow, re.M), [starter])
        self.assertEqual(re.findall(r"^\s+mode: (\S+)$", workflow, re.M), ["deletion"])
