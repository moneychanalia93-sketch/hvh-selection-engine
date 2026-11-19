"""Allow ``python -m hvh <command>``."""

import sys

from hvh.cli import main

if __name__ == "__main__":
    sys.exit(main())
