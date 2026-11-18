"""Start the local pgvector HTTP app or its independent worker."""

import argparse
from pathlib import Path

from examples.support_search.__main__ import serve
from . import MODES, SCENARIOS
from .app import App, work


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("serve", "worker"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("run_id")
    parser.add_argument("mode", choices=MODES)
    parser.add_argument("scenario", choices=SCENARIOS)
    args = parser.parse_args()
    if args.operation == "worker":
        work(args.run_id, args.mode)
        return 0
    return serve(App(args.directory, args.run_id, args.mode, args.scenario), args.directory, args.run_id)


if __name__ == "__main__":
    raise SystemExit(main())
