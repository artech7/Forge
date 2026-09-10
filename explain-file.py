#!/usr/bin/env python3
"""Explain why Forge did or didn't pick up a particular file.

    python3 explain-file.py "/path/to/the/file.mp4"

Walks the same decisions the scanner makes, in order, and prints what each
one concluded. Reads the live database, so it can be run while Forge is up.

The same walkthrough is also available inside Forge itself (Nodes &
Libraries -> "Explain a file") for when running this script directly
against the server isn't an option.
"""
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE / "server"))

import db          # noqa: E402

# Lets the checks point at a scratch database instead of the live one.
if os.environ.get("FORGE_DB"):
    db.DB_PATH = pathlib.Path(os.environ["FORGE_DB"])
import explain     # noqa: E402


def probe(path):
    # Imported lazily: everything up to this point in the walkthrough can
    # answer without FastAPI and friends ever needing to load.
    import app
    return app.probe(path)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    target = pathlib.Path(sys.argv[1]).expanduser().resolve()
    for line in explain.walk(target, probe):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
