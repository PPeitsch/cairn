# cairn

Base local-first para los docs de trabajo (diagnósticos, planes, referencias) y los handoffs entre
conversaciones. Cada lugar donde se trabaja es un **nodo** con su propia base; los nodos se
sincronizan por ssh, al estilo git, y los agentes acceden por MCP stdio.

> Estado: en construcción. Todavía no hace nada útil.

## Cómo funciona

- **Log de eventos**: la fuente de verdad son las operaciones (`create`, `close`, `promote`, …),
  no el estado. Cada evento lleva su hash, el del evento anterior del mismo doc y la firma
  (ed25519) del nodo que lo escribió. De ahí salen el historial, los diffs y la verificación.
- **Un log por nodo**: cada nodo escribe sólo en sus propios archivos, así que sincronizar es
  intercambiar los eventos que le faltan al otro, sin merge.
- **SQLite + FTS5** como proyección del log: consultas y búsqueda rápidas, reconstruible en
  cualquier momento.
- **Sync por ssh**: `cairn sync` habla con `cairn sync-peer --stdio` del otro lado, con una clave
  restringida a ese único comando.
- **MCP stdio** contra la base local, e integración con Claude Code generada por el propio
  producto.

## Rutas

| Qué | Dónde |
|---|---|
| Configuración | `~/.config/cairn/config.toml` |
| Datos (logs, base, clave del nodo) | `~/.local/share/cairn/` |

Los datos nunca van en este repo: cada nodo tiene los suyos y el sync los iguala.

## Instalación

```bash
uv tool install git+<url-del-repo>    # o: pipx install git+<url-del-repo>
cairn --version
```

## Licencia

[MIT](LICENSE).
