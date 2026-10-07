# cairn

Base local-first para los docs de trabajo (diagnósticos, planes, referencias) y los handoffs entre
conversaciones. Cada lugar donde se trabaja es un **nodo** con su propia base; los nodos se
sincronizan por ssh, al estilo git, y los agentes acceden por MCP stdio.

> Estado: en construcción. Fases 1 (núcleo local: log, proyección, CLI e importador), 2 (MCP
> stdio e integración con Claude Code) y 3 (sync por ssh con eventos firmados) listas. Falta
> migrar desde wip-doc y, si hace falta, un transporte HTTPS para nodos cloud sin ssh saliente.

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
| Datos (logs, base, clave del nodo `node_key`) | `~/.local/share/cairn/` |

Los datos nunca van en este repo: cada nodo tiene los suyos y el sync los iguala.

## Configuración

Todo lo que es preferencia vive en `config.toml` (los valores van como texto). Nada de esto
cambia el formato de los eventos, así que dos nodos pueden tener configs distintas.

| Tabla | Clave | Default | Qué |
|---|---|---|---|
| `[docs]` | `projects` | `~/projects` | raíz de los proyectos (el `repo:` de cada doc sale de acá) |
| | `author` | `git config user.name`, o el usuario | autor de los docs nuevos |
| | `templates` | (las de fábrica) | carpeta con `bug.md`, `infra.md`, `plan.md`, `ref.md`, `ref_header.md`, `handoff.md`; `{titulo}` se reemplaza |
| | `handoff_stale_days` | `14` | desde cuántos días un handoff pendiente se marca como viejo |
| | `file_dirs` | `scripts, validations` | subcarpetas de archivos que no son docs |
| | `prune`, `max_depth` | `node_modules, .git, …`, `4` | qué no recorrer al buscar carpetas de docs adentro de repos |
| `[sync]` | `auto` | `true` | sync en segundo plano y en los hooks |
| | `timeout`, `hook_timeout` | `60`, `15` | segundos por remoto (a mano / en el hook de handoffs) |
| `[mcp]` | `instructions` | (las de fábrica) | archivo con las instrucciones del servidor MCP |
| `[claude_code]` | `mcp_name` | `cairn` | nombre con el que `integrate` registra el MCP |
| | `start_timeout`, `end_timeout` | `30`, `10` | timeout de los hooks `SessionStart` y `SessionEnd` |

`cairn defaults <carpeta>` exporta las plantillas y las instrucciones de fábrica para partir de
ellas, y muestra qué poner en la config. Una config que no carga no se guarda: `cairn` vuelve a la
anterior.

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

cairn list [wip|done|ref|all] [--por-repo]  # por repo: con conteos y fecha del último cambio
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
cairn resolve <doc> [--keep main|<hash>]    # ramas divergentes: verlas y elegir cuál queda
```

### Sync entre nodos

Cada nodo tiene una clave ed25519 (`<datos>/node_key`, formato OpenSSH) que se genera sola:
firma cada evento que escribe y es también su clave ssh para sincronizar. Un evento sin firma
válida de un nodo autorizado no entra a la base, llegue como llegue.

Sumar un nodo (p. ej. la laptop contra el VPS, con un usuario dedicado `cairn` en el VPS):

```bash
laptop$ cairn key                                   # copiar la línea; anotar la huella
vps$    cairn authorize laptop "ssh-ed25519 AAAA… cairn:laptop"
vps$    cairn key                                   # copiar la línea
laptop$ cairn authorize vps "ssh-ed25519 AAAA… cairn:vps" --no-ssh
laptop$ cairn remote add vps ssh://cairn@vps.example
laptop$ cairn sync
```

`authorize` deja en `~/.ssh/authorized_keys` del VPS una entrada restringida a un único comando
(`command="cairn sync-peer --stdio --as laptop",restrict`): esa clave no da shell ni forwarding,
y no puede hacerse pasar por otro nodo. `--no-ssh` sólo confía en los eventos del nodo (para uno
que no se conecta a este, cuyos eventos llegan a través de otro). Cada nodo tiene que autorizar
a todos los demás, así un nodo comprometido no puede inyectar eventos a nombre de otro.

**La huella de la clave.** `cairn key` (y `init`, y `key --rotate`) muestra también la huella de
la clave del nodo, `SHA256:…`, la misma que da `ssh-keygen -lf`; `cairn key --fingerprint`, sólo
esa. Conviene anotarla **fuera del nodo** (en el gestor de contraseñas, p. ej.): si el nodo pierde
sus datos, es lo que permite controlar en el restore las claves viejas que mande el par. No es
secreta. `cairn peers` muestra las huellas de los nodos autorizados y de sus claves anteriores.

```bash
cairn sync [remoto]... [--quiet] [--timeout S] [--background]
cairn remote add|remove|list
cairn peers                                 # los autorizados, con o sin ssh, revocados
cairn revoke <nodo>                         # saca el acceso y rechaza sus eventos nuevos
```

Con remotos configurados, el sync corre solo: el hook de handoffs trae lo nuevo antes de
avisar, al cerrar la sesión se empuja en segundo plano, y lo mismo después de crear o cerrar un
doc y de crear o tomar un handoff (salida en `<datos>/sync.log`). `[sync] auto = "false"` en la
config lo apaga.

**Cambio de clave.** `cairn key --rotate` genera una clave nueva; la anterior queda en la config
(`[node.old]`) y sigue verificando lo que ya firmó. En cada nodo que confía en este,
`cairn authorize <nodo> "<clave nueva>" --replace`: la vieja pasa a `[peers.<nodo>.old]` y sólo
verifica eventos de antes del cambio. Reautorizar un nodo revocado con otra clave conserva la
vieja para lo firmado hasta el día de la revocación.

**Restore de un nodo que perdió sus datos.** Se reinicializa con el mismo nombre (clave nueva),
se acepta esa clave en un par y se le pide el log propio, con la huella de la clave perdida:

```bash
laptop$ cairn init --node laptop && cairn key       # copiar la línea
vps$    cairn authorize laptop "ssh-ed25519 AAAA… cairn:laptop" --replace
laptop$ cairn authorize vps "ssh-ed25519 AAAA… cairn:vps" --no-ssh   # y el resto de los nodos
laptop$ cairn remote add vps ssh://cairn@vps.example
laptop$ cairn sync --restore vps --old-key SHA256:…  # la huella anotada
```

El restore sólo corre en un nodo sin eventos propios, y sólo si la clave vigente que el par
tiene registrada para él es la suya. El historial viejo está firmado con la clave perdida, y esa
clave la manda el par; si se la creyera a ciegas, un par comprometido podría mandar una inventada
y, firmada con ella, historia falsa a nombre del nodo. Por eso cada clave anterior que mande
tiene que coincidir con una huella pasada con `--old-key` (una por clave, si hubo varias); si
alguna no coincide, o el par no tiene alguna de las huellas pasadas, el restore se corta sin
guardar nada y dice cuáles mandó el par. Sin las huellas anotadas, `--trust-peer-keys` acepta las
del par sin control. Si la clave privada se recuperó de un backup, no hay clave vieja que
controlar y `--old-key` no hace falta.

### Agentes (MCP)

```bash
cairn integrate claude-code [--dry-run]     # registra el MCP (scope user) y el hook SessionStart
cairn integrate claude-code --remove        # lo deshace
cairn integrate claude-code --command "ssh vps cairn"   # usar la base de otro nodo por ssh
cairn integrate claude-code --replace-wip-doc           # al migrar: saca también el hook de wip-doc
```

`integrate` registra el MCP con `claude mcp add --scope user` y agrega a `settings.json` sólo las
entradas de cairn (no toca los demás hooks). Es idempotente y respeta `CLAUDE_CONFIG_DIR`.

`cairn mcp` es el servidor stdio que lanza el agente: expone el ciclo de vida de los docs
(`cairn_new`, `cairn_edit`, `cairn_close`, `cairn_reopen`, `cairn_promote`), las consultas
(`cairn_list`, `cairn_search`, `cairn_show`, `cairn_weekly`, `cairn_check`, `cairn_log`,
`cairn_diff`) y los handoffs (`cairn_handoff_new|list|take|drop`). Sus instrucciones llevan las
convenciones de cuándo y cómo documentar.

`check` y `search` miran también la raíz de proyectos: `check` avisa de un `.md` suelto en
`_wip/`, `_done/` o `_ref/` (un doc que no pasó por cairn, o un resto de wip-doc ya importado),
de archivos fuera de `scripts/` y `validations/` y de carpetas `_wip/` adentro de un repo; `search`
busca además en los `.md` de `scripts/` y `validations/`, que siguen siendo archivos.

Un doc se nombra por su nombre (`2026-10-04_cairn_arquitectura`, con o sin `.md`) o por un
prefijo de su id. No hay carpetas: si un doc está abierto, cerrado o es referencia lo dicen su
tipo y su estado, y los vínculos (`origen`, `referencias`, `plan`) se guardan por id.

## Desarrollo

```bash
uv run pytest
uvx ruff check src tests
```

Los tests corren contra un nodo aislado en un temporal: `CAIRN_CONFIG_DIR`, `CAIRN_DATA_DIR` y
`CAIRN_PROJECTS` cambian las rutas, y `CAIRN_NOW` fija el reloj. Para el sync,
`CAIRN_AUTHORIZED_KEYS` cambia el `authorized_keys`, `CAIRN_SSH` el comando ssh (como `GIT_SSH`)
y `CAIRN_NO_AUTOSYNC` apaga los disparadores.

## Licencia

[MIT](LICENSE).
