"""Allow ``python -m testenix`` to behave like the ``testenix`` console script."""

from testenix.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
