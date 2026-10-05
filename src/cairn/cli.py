"""Punto de entrada del comando `cairn`."""

from __future__ import annotations

import argparse
import sys

from cairn import __version__, config
from cairn.store import CairnError, Store


def _open() -> Store:
    return Store(config.load())


def warn(msg: str) -> None:
    print(msg, file=sys.stderr)


# ──────────────────────────────────────────────────────────────── init / rebuild

def cmd_init(args) -> int:
    path = config.config_dir() / config.CONFIG_NAME
    if path.is_file():
        cfg = config.load()
        print(f"cairn ya está inicializado en este nodo: {cfg.node}")
    else:
        node = args.node or config.default_node_name()
        config.write(node, data_dir=args.data, projects=args.projects, author=args.author)
        cfg = config.load()
        print(f"Nodo inicializado: {cfg.node}")
    with Store(cfg) as store:
        n = store.projection.count_events()
    print(f"  config:    {cfg.path}")
    print(f"  datos:     {cfg.data_dir}")
    print(f"  proyectos: {cfg.projects}")
    print(f"  eventos:   {n}")
    return 0


def cmd_rebuild(args) -> int:
    with _open() as store:
        n = store.rebuild()
        docs = len(store.projection.doc_ids())
        for p in store.problems:
            warn(f"[aviso] {p}")
    print(f"Proyección rehecha desde el log: {n} evento(s), {docs} doc(s)")
    return 1 if store.problems else 0


# ──────────────────────────────────────────────────────────────── parser

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cairn", description="Base local-first para docs de trabajo y handoffs.")
    parser.add_argument("--version", action="version", version=f"cairn {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<comando>")

    p = sub.add_parser("init", help="inicializa el nodo: config, carpeta de datos y base vacía")
    p.add_argument("--node", help="nombre del nodo (default: el hostname)")
    p.add_argument("--data", help="carpeta de datos (default: ~/.local/share/cairn)")
    p.add_argument("--projects", help="raíz de los proyectos (default: ~/projects)")
    p.add_argument("--author", help=f"autor de los docs (default: {config.DEFAULT_AUTHOR})")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("rebuild", help="rehace la proyección SQLite desde el log de eventos")
    p.set_defaults(func=cmd_rebuild)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return args.func(args) or 0
    except (CairnError, config.ConfigError) as e:
        warn(str(e))
        return 1
    except BrokenPipeError:        # `cairn search x | head`
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
