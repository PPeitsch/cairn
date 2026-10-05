# cairn

Base local-first para los docs de trabajo (diagnósticos, planes, referencias) y los handoffs entre
conversaciones. Cada lugar donde se trabaja es un **nodo** con su propia base; los nodos se
sincronizan por ssh, al estilo git, y los agentes acceden por MCP stdio.

> Estado: en construcción. Fase 1 (núcleo local: log, proyección, CLI e importador) y fase 2
> (MCP stdio e integración con Claude Code) listas; falta el sync por ssh (fase 3).

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

## Uso

```bash
cairn init                                  # config, datos y base vacía (nodo = hostname)
cairn import ~/projects                     # una vez: los docs de _wip/_done/_ref (--dry-run)

cairn new bcra rate_limit_429 bug           # diagnóstico; también infra (default), plan, ref
cairn edit <doc>                            # en $EDITOR; o --stdin / --file / --set k=v / --unset k
cairn close <doc> [--force]                 # corta si quedan `- [ ]` sin tildar
cairn reopen <doc> [motivo]
cairn promote <doc> <area> <tema> [--seccion S]...   # secciones a una referencia, vinculadas

cairn list [wip|done|ref|all]
cairn search <regex> [scope]                # o --fts <consulta> por relevancia
cairn weekly [dias] [resumen|full]
cairn check                                 # log, proyección y docs en orden (sale 1 si no)

cairn handoff new <area> <tema> [--para R]... [--plan <doc>]...
cairn handoff take [<nota>] | drop <nota> [motivo] | list [--all] | hook

cairn show <doc>[@rev]                      # rev: prefijo del hash o ~N
cairn log [<doc>] [-n N]
cairn diff <doc> [rev_a] [rev_b]            # default: el último cambio
cairn export <destino> [scope] [--force]    # los docs como árbol _wip/_done/_ref
cairn rebuild                               # rehace la base SQLite desde el log
```

### Agentes (MCP)

```bash
cairn integrate claude-code [--dry-run]     # registra el MCP (scope user) y el hook SessionStart
cairn integrate claude-code --remove        # lo deshace
cairn integrate claude-code --command "ssh vps cairn"   # usar la base de otro nodo por ssh
```

`integrate` registra el MCP con `claude mcp add --scope user` y agrega a `settings.json` sólo las
entradas de cairn (no toca los demás hooks). Es idempotente y respeta `CLAUDE_CONFIG_DIR`.

`cairn mcp` es el servidor stdio que lanza el agente: expone el ciclo de vida de los docs
(`cairn_new`, `cairn_edit`, `cairn_close`, `cairn_reopen`, `cairn_promote`), las consultas
(`cairn_list`, `cairn_search`, `cairn_show`, `cairn_weekly`, `cairn_check`, `cairn_log`,
`cairn_diff`) y los handoffs (`cairn_handoff_new|list|take|drop`). Sus instrucciones llevan las
convenciones de cuándo y cómo documentar.

Un doc se nombra por su nombre (`2026-10-04_cairn_arquitectura`, con o sin `.md`) o por un
prefijo de su id. No hay carpetas: si un doc está abierto, cerrado o es referencia lo dicen su
tipo y su estado, y los vínculos (`origen`, `referencias`, `plan`) se guardan por id.

## Desarrollo

```bash
uv run pytest
uvx ruff check src tests
```

Los tests corren contra un nodo aislado en un temporal: `CAIRN_CONFIG_DIR`, `CAIRN_DATA_DIR` y
`CAIRN_PROJECTS` cambian las rutas, y `CAIRN_NOW` fija el reloj.

## Licencia

[MIT](LICENSE).
