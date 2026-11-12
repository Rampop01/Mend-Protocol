"""Runs the Mend server. Equivalent to `python -m mend serve`."""
from mend.cli import main

if __name__ == "__main__":
    main(["serve"])
