# cairn

A local-first store for the working docs an AI coding agent and you write along the way —
diagnoses, plans, references — and for the handoff notes one conversation leaves for the next.
Docs live in a signed event log on your machine, the agent reads and writes them through MCP, and
if you work on more than one machine, they sync over plain ssh.

> Status: in daily use by its author since October 2026, on two nodes (a desktop and a VPS).
> Single-user by design. The Claude Code integration is built in; the MCP server works with any
> MCP client. Not done yet: an HTTPS transport for nodes that cannot open an outbound ssh
> connection.

## Why

Agents are good at investigating a bug or designing a feature, and bad at remembering it. What
was tried, which hypothesis was dropped and why, what the next session should pick up: that
lives in the conversation and is gone when it ends, or ends up as loose `.md` files that nobody
closes, nobody finds and that quietly go stale.

cairn gives that material a lifecycle. A diagnosis is opened when the investigation starts and
closed when it is fixed; a closed doc says *what happened*, and whatever will be looked up again
is promoted to a reference that says *how something is*. A handoff is a one-shot note that the
next conversation is told about at startup, and that is archived once taken. `cairn weekly`
turns the closed docs into a digest of what got done.

### What it is not

- **Not an issue tracker.** There are no priorities, assignees or dependency graphs. If that is
  what you want for an agent, look at [Beads](https://github.com/steveyegge/beads), which
  tracks issues and their dependencies inside the repo. cairn keeps the narrative around the
  work: the why, the path the diagnosis took, the decisions.
- **Not a knowledge graph or a notes app.** [Basic Memory](https://github.com/basicmachines-co/basic-memory)
  builds a linked knowledge base out of Markdown files you can also browse in Obsidian. cairn's
  docs are few, typed and short-lived: they are opened and closed, and links between them are
  only the ones the lifecycle creates (a reference and the doc it came from, a handoff and its
  plan).
- **Not collaborative.** The nodes are your own machines and trust each other; there are no
  users or permissions, and no web UI.
- **Not tied to a repo.** Docs are stored by node, not inside each project: one queue of open
  work across every repo under your projects folder, each doc stamped with the repo it belongs
  to.

## Quick start (one machine)

A single node is a complete installation: sync is optional and can be added later.

```bash
uv tool install git+https://github.com/PPeitsch/cairn    # or: pipx install git+…
cairn init                                  # config, data folder and node key
cairn integrate claude-code                 # MCP server + the SessionStart hook for handoffs
```

From then on, the agent opens and fills docs with the `cairn_*` tools, following the
instructions the MCP server carries. The same operations, by hand:

```bash
cd ~/projects/api
cairn new api timeout_on_login bug          # 2026-10-19_api_BUG_timeout_on_login, repo: api
cairn edit 2026-10-19_api_BUG_timeout_on_login                # in $EDITOR
cairn close 2026-10-19_api_BUG_timeout_on_login               # stops if `- [ ]` items remain
cairn promote 2026-10-19_api_BUG_timeout_on_login api auth_flow   # sections → reference api_auth_flow

cairn new api db_migration plan             # a feature plan
cairn handoff new api finish_migration --plan 2026-10-19_api_db_migration
#   next conversation in ~/projects/api: "there is a pending handoff, take it?"

cairn list --by-repo                        # the open work, grouped by repo
cairn search --fts "connection pool"        # by relevance, with the matching snippet
cairn show api_auth_flow --section Context  # read a part of a long doc
cairn weekly                                # what got closed in the last 7 days
```

## The taxonomy

The doc types, their sections and the naming are what cairn *is*, more than its storage:

| Type | What goes in it | Lifecycle | Name |
|---|---|---|---|
| `bug`, `infra` | a diagnosis: TL;DR, Context / topology, Symptoms observed, Discoveries (in order), Fixes applied, Pending, Useful commands | `new` → `close` ↔ `reopen` | `<date>_<area>_BUG_<topic>`, `<date>_<area>_<topic>` |
| `plan` | a feature plan: TL;DR, Context / problem, Scope, Design / proposal, Decisions, Pending | `new` → `close` ↔ `reopen` | `<date>_<area>_<topic>` |
| `ref` | reference material in the present tense: a review, an API map, architecture notes | none: it is updated | `<area>_<topic>` |
| `handoff` | a note for the next conversation: what to read first, work order, what to ask | `handoff new` → `take` / `drop` | `<date>_<area>_HANDOFF_<topic>` |

Status lives in the front-matter (`type`, `repo`, `status`, `date`, `closed`, …) and only the
operations change it: a doc with unchecked `- [ ]` items does not close without `--force`, a
discarded one keeps its reason (`close --discard "why"`), and editing a reference stamps
`updated:` so a stale one shows its age. Work that spans several repos goes under
`repo: cross-repo`, which is also what a doc created outside any repo gets.

None of the text is hard-wired. `cairn defaults <folder>` exports the built-in templates (one per
type) and the MCP instructions (when and how the agent should document); edit them — rewrite the
sections, translate them — and point `[docs] templates` and `[mcp] instructions` at them. The
data vocabulary (field names, statuses) is the one thing that stays fixed: it is the format the
nodes understand each other in.

## More than one machine

Every machine where work happens can be a **node** with its own copy of the database. Nodes
exchange events over ssh, so the one requirement is a machine the others can reach: a VPS, a
Raspberry Pi or a NAS that is always on works as the meeting point, and laptops that come and go
sync against it. Every write triggers a background sync, the handoffs hook pulls before
announcing, and a systemd timer covers the gaps. Details in [Sync between nodes](#sync-between-nodes).

## How it works

- **Event log**: the source of truth is the operations (`create`, `close`, `promote`, …), not
  the state. Each event carries its hash, the hash of the previous event of the same doc, and
  the signature (ed25519) of the node that wrote it. History, diffs and verification come from
  there.
- **One log per node**: each node writes only to its own files, so syncing means exchanging the
  events the other side is missing, with no merging.
- **SQLite + FTS5** as a projection of the log: fast queries and search, rebuildable at any time.
- **Sync over ssh**: `cairn sync` talks to `cairn sync-peer --stdio` on the other side, with a key
  restricted to that single command.
- **MCP stdio** against the local database, and a Claude Code integration generated by the
  product itself.

## Paths

| What | Where |
|---|---|
| Configuration | `~/.config/cairn/config.toml` |
| Data (logs, database, node key `node_key`) | `~/.local/share/cairn/` |

Data never goes in this repo: each node has its own and sync keeps them equal.

## Configuration

Everything that is a preference lives in `config.toml` (values are written as text). None of it
changes the event format, so two nodes can have different configs.

| Table | Key | Default | What |
|---|---|---|---|
| `[docs]` | `projects` | `~/projects` | projects root (each doc's `repo:` comes from here) |
| | `author` | `git config user.name`, or the user | author of new docs |
| | `templates` | (the built-in ones) | folder with `bug.md`, `infra.md`, `plan.md`, `ref.md`, `ref_header.md`, `handoff.md`; `{title}` is replaced |
| | `handoff_stale_days` | `14` | after how many days a pending handoff is flagged as old |
| | `file_dirs` | (none) | subfolders of `<projects>/_wip`, `_done` and `_ref` with files that are not docs (e.g. `scripts, validations`); see below |
| | `prune`, `max_depth` | `node_modules, .git, …`, `4` | what not to walk in those folders, and when looking for wip-doc folders inside repos |
| `[sync]` | `auto` | `true` | background sync and sync in the hooks |
| | `timeout`, `hook_timeout` | `60`, `15` | seconds per remote (by hand / in the handoffs hook) |
| | `debounce` | `5` | seconds the sync triggered by a write waits, so a burst travels together |
| | `interval` | `15` | minutes between syncs of the `integrate systemd` timer |
| `[mcp]` | `instructions` | (the built-in ones) | file with the MCP server instructions |
| | `show_max_chars` | `8000` | `cairn_show` returns a longer doc as an outline (TL;DR and sections with their lines) |
| | `search_max_docs`, `search_max_lines` | `15`, `3` | cap on what `cairn_search` returns with a regex |
| `[claude_code]` | `mcp_name` | `cairn` | name `integrate` registers the MCP server under |
| | `start_timeout`, `end_timeout` | `30`, `10` | timeout of the `SessionStart` and `SessionEnd` hooks |

`cairn defaults <folder>` exports the built-in templates and instructions as a starting point (to
write them in another language, for instance), and shows what to put in the config. A config that
does not load is not saved: `cairn` goes back to the previous one.

## Installation

```bash
uv tool install git+https://github.com/PPeitsch/cairn    # or: pipx install git+…
cairn --version
```

With `"cairn[serve] @ git+https://github.com/PPeitsch/cairn"` instead, `cairn serve` also renders
docs as markdown, highlights code and draws mermaid diagrams (it adds `markdown-it-py` and
`pygments`); without it, it shows them as text.

## Usage

```bash
cairn init                                  # config, data and empty database (node = hostname)

cairn new bcra rate_limit_429 bug           # a diagnosis; also infra (default), plan, ref
cairn edit <doc>                            # in $EDITOR; or --stdin / --file / --set k=v / --unset k
cairn close <doc> [--force]                 # stops if unchecked `- [ ]` items remain
cairn close <doc> --discard "reason"        # unfinished: status `discarded`, reason in `note:`
cairn reopen <doc> [reason]
cairn promote <doc> <area> <topic> [--section S]...   # sections to a reference, linked
cairn link <doc> <origin|references|plan> <other> [--remove]   # <doc> gets <other> under <kind>:

cairn list [wip|done|ref|all] [--by-repo]   # by repo: with counts and date of the last change
cairn search <regex> [scope]                # or --fts <query> by relevance
cairn weekly [days] [summary|full]
cairn check                                 # log, projection and docs in order (exits 1 if not)

cairn handoff new <area> <topic> [--to R]... [--plan <doc>]...
cairn handoff take [<note>] | drop <note> [reason] | list [--all] | hook

cairn show <doc>[@rev] [--section S | --lines A-B]    # rev: hash prefix or ~N
cairn log [<doc>] [-n N]
cairn diff <doc> [rev_a] [rev_b]            # default: the last change
cairn rebuild                               # rebuilds the SQLite database from the log
cairn resolve <doc> [--keep main|<hash>]    # divergent branches: see them and pick one

cairn serve [--port N] [--open]             # read-only web view on 127.0.0.1 (default 8737)
```

### Browsing in the browser

`cairn serve` opens a local web view of the docs:

- **Home**: open work grouped by repo (with checklist progress and the docs that went quiet),
  pending handoffs, what closed this week and the latest activity.
- **Lists** of open, closed and reference docs, filtered by scope, type and repo.
- **A doc**: its status and facts, the doc itself, an outline that follows the scroll, its links
  both ways, its checklist progress, and the `cairn show` / `cairn edit` commands to copy. The
  names of other docs become links. If it has links, a diagram of its group (a plan with its
  handoffs and the reference that came out of it), in date order with a lane per type.
- **History** of a doc as a timeline with the diff of each revision; **Activity** for every
  change on the node, by day; **Weekly**; full-text **search** (`/` focuses it).

With the `serve` extra, docs are rendered as markdown, code blocks are highlighted and
`mermaid` blocks are drawn as diagrams; without it (or with "text"), a doc is shown as the text
it is stored as. Raw HTML in a doc is shown as text, never run. Mermaid is loaded from a CDN
only on pages that have a diagram; without network the diagram's source is shown instead.
Light and dark follow the system, or the toggle.

It only reads, listens only on `127.0.0.1` and opens the database on each request, so whatever
a sync brings is there on reload (the page offers it when it notices). To browse another node,
tunnel: `ssh -L 8737:localhost:8737 <node>` and `cairn serve` there.

### Sync between nodes

Each node has an ed25519 key (`<data>/node_key`, OpenSSH format) that is generated on its own:
it signs every event the node writes and is also its ssh key for syncing. An event without a
valid signature from an authorized node does not enter the database, however it arrives.

Adding a node (e.g. the laptop against the VPS, with a dedicated `cairn` user on the VPS):

```bash
laptop$ cairn key                                   # copy the line; note down the fingerprint
vps$    cairn authorize laptop "ssh-ed25519 AAAA… cairn:laptop"
vps$    cairn key                                   # copy the line
laptop$ cairn authorize vps "ssh-ed25519 AAAA… cairn:vps" --no-ssh
laptop$ cairn remote add vps ssh://cairn@vps.example
laptop$ cairn sync
```

`authorize` leaves an entry in the VPS's `~/.ssh/authorized_keys` restricted to a single command
(`command="cairn sync-peer --stdio --as laptop",restrict`): that key gives no shell or
forwarding, and it cannot pose as another node. `--no-ssh` only trusts the node's events (for one
that does not connect to this node, whose events arrive through another). Every node has to
authorize all the others, so a compromised node cannot inject events in another node's name.

**The key fingerprint.** `cairn key` (and `init`, and `key --rotate`) also shows the node key's
fingerprint, `SHA256:…`, the same one `ssh-keygen -lf` prints; `cairn key --fingerprint` shows
only that. It is worth noting it down **outside the node** (in the password manager, e.g.): if
the node loses its data, it is what lets the restore check the old keys the peer sends. It is not
secret. `cairn peers` shows the fingerprints of the authorized nodes and of their previous keys.

```bash
cairn sync [remote]... [--quiet] [--timeout S] [--background]
cairn remote add|remove|list
cairn peers                                 # the authorized ones, with or without ssh, revoked
cairn revoke <node>                         # removes access and rejects its new events
```

With remotes configured, sync runs on its own: the handoffs hook pulls what is new before
announcing, and **every write** (through the MCP or the CLI: create, edit, close, a handoff…)
launches a background sync that waits `[sync] debounce` seconds, so a burst of edits travels
together; when the session ends it also pushes. One sync at a time on each node, and the output
goes to `<data>/sync.log`. `[sync] auto = "false"` in the config turns it off.

As a backup for those triggers (a push that failed because the remote was down, a hook that did
not run because the window was closed, the machine being off in between), a systemd user timer
syncs every `[sync] interval` minutes:

```
cairn integrate systemd [--dry-run]     # writes cairn-sync.{service,timer} and enables it
cairn integrate systemd --remove        # disables it and deletes the units
systemctl --user list-timers cairn-sync.timer
```

**Key change.** `cairn key --rotate` generates a new key; the previous one stays in the config
(`[node.old]`) and keeps verifying what it already signed. On every node that trusts this one,
`cairn authorize <node> "<new key>" --replace`: the old one moves to `[peers.<node>.old]` and only
verifies events from before the change. Reauthorizing a revoked node with another key keeps the
old one for what was signed up to the day of the revocation.

**Restoring a node that lost its data.** It is reinitialized with the same name (new key), that
key is accepted on a peer, and the node asks for its own log, with the fingerprint of the lost
key:

```bash
laptop$ cairn init --node laptop && cairn key       # copy the line
vps$    cairn authorize laptop "ssh-ed25519 AAAA… cairn:laptop" --replace
laptop$ cairn authorize vps "ssh-ed25519 AAAA… cairn:vps" --no-ssh   # and the rest of the nodes
laptop$ cairn remote add vps ssh://cairn@vps.example
laptop$ cairn sync --restore vps --old-key SHA256:…  # the noted fingerprint
```

The restore only runs on a node with no events of its own, and only if the current key the peer
has on record for it is its own. The old history is signed with the lost key, and the peer is
the one sending that key; if it were believed blindly, a compromised peer could send a made-up
one and, signed with it, fake history in the node's name. That is why every previous key it sends
has to match a fingerprint passed with `--old-key` (one per key, if there were several); if any
does not match, or the peer lacks one of the fingerprints passed, the restore stops without saving
anything and says which ones the peer sent. Without noted fingerprints, `--trust-peer-keys`
accepts the peer's keys unchecked. If the private key was recovered from a backup, there is no
old key to check and `--old-key` is not needed.

### Agents (MCP)

```bash
cairn integrate claude-code [--dry-run]     # registers the MCP server (user scope) and the SessionStart hook
cairn integrate claude-code --remove        # undoes it
cairn integrate claude-code --command "ssh vps cairn"   # use another node's database over ssh
```

`integrate` registers the MCP server with `claude mcp add --scope user` and adds only cairn's
entries to `settings.json` (it does not touch the other hooks). It is idempotent and honors
`CLAUDE_CONFIG_DIR`.

`cairn mcp` is the stdio server the agent launches: it exposes the docs' lifecycle
(`cairn_new`, `cairn_edit`, `cairn_close`, `cairn_reopen`, `cairn_promote`, `cairn_link`), the
queries (`cairn_list`, `cairn_search`, `cairn_show`, `cairn_weekly`, `cairn_check`, `cairn_log`,
`cairn_diff`) and the handoffs (`cairn_handoff_new|list|take|drop`). Its instructions carry the
conventions for when and how to document.

A doc is referred to by its name (`2026-10-04_cairn_arquitectura`, with or without `.md`) or by a
prefix of its id. There are no folders: whether a doc is open, closed or a reference is given by
its type and status, and links (`origin`, `references`, `plan`) are stored by id.
It is shown as `_wip/<name>.md`, `_done/<name>.md` or `_ref/<name>.md` (open, closed,
reference), and links between docs are rendered as relative paths from there.

### Files that are not docs

Scripts, generated reports and CSV files do not go into cairn. If they live under the projects
root, in `_wip/`, `_done/` and `_ref/`, `[docs] file_dirs` names their subfolders (e.g.
`file_dirs = "scripts, validations"`), and then `search` also searches the `.md` files in them
and `check` warns about what is out of place: a stray `.md` at the top (a doc that did not go
through cairn), a file outside the subfolders, or a subfolder that is not one of them. Without
`file_dirs` (the default), cairn does not look at those folders.

### Migrating from wip-doc

wip-doc kept the docs as `.md` files in `<root>/{_wip,_done,_ref}` and announced handoffs from
its own `SessionStart` hook. Each step of the move is its own command:

```bash
cairn migrate wip-doc import ~/projects [--dry-run]   # once: the docs in _wip/_done/_ref
cairn migrate wip-doc check [<root>]                  # what is left: files already imported, docs
                                                      # not imported, _wip/ folders inside repos
cairn migrate wip-doc unhook [--dry-run]              # removes wip-doc's hook from Claude Code
cairn migrate wip-doc export <dest> [scope] [--force] # cairn's docs back as that tree
```

Importing twice duplicates nothing, and `export` of what was imported reproduces the files,
with wip-doc's Spanish front-matter in cairn's vocabulary.

## The Spanish vocabulary of older logs

cairn started out in Spanish, and logs written back then carry Spanish field names and statuses
(`tipo`, `estado: en_progreso`, `nota`, …). They are read as aliases of the English ones, forever:
the log is signed and never rewritten. Everything new is written in English, and hand input with
the old names (`edit --set estado=…`) is saved in English too.

## Development

```bash
uv run pytest
uvx ruff check src tests
```

Tests run against an isolated node in a temp dir: `CAIRN_CONFIG_DIR`, `CAIRN_DATA_DIR` and
`CAIRN_PROJECTS` change the paths, and `CAIRN_NOW` pins the clock. For sync,
`CAIRN_AUTHORIZED_KEYS` changes the `authorized_keys` file, `CAIRN_SSH` the ssh command (like
`GIT_SSH`), `CAIRN_NO_AUTOSYNC` turns the triggers off and `CAIRN_SYSTEMD_DIR` changes where
`integrate systemd` writes the units.

## License

[MIT](LICENSE).
