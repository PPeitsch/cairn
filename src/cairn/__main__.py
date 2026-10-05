"""`python -m cairn`: lo que usa la integración si el comando `cairn` no está en el PATH."""

from cairn.cli import main

raise SystemExit(main())
