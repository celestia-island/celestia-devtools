"""Allow ``python -m celestia_devtools``."""

from celestia_devtools.core.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
