"""Punto de entrada del comando `cairn`."""

import argparse

from cairn import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cairn", description=__doc__)
    parser.add_argument("--version", action="version", version=f"cairn {__version__}")
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
