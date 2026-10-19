"""`python -m cairn`: what the integration uses if the `cairn` command is not on the PATH."""

from cairn.cli import main

raise SystemExit(main())
