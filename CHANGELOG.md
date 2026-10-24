# Changelog

All notable changes to cairn are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html); before 1.0, a breaking change bumps the
minor version. See [Versions and compatibility](README.md#versions-and-compatibility) for what
"breaking" means here and how to upgrade a node.

Versions 0.1.0 to 0.4.0 were reconstructed from the git history when versioning started (until
then every build reported 0.0.1); their tags point at the merge that closed each milestone.

## [Unreleased]

### Added
- `cairn rename <doc> <new-name>` (and `cairn_rename`): a new name for a doc, with the mentions of
  the old one replaced in the body and front-matter of every doc. Links go by id and keep
  working, and the old name still finds the doc.
- `cairn delete <doc> [reason]` (and `cairn_delete`, marked destructive): the doc leaves `list`,
  `search`, `show`, the web view and the handoffs, and its name is free again. Its events stay
  in the log, so `cairn restore <doc>` (and `cairn_restore`) brings it back; `cairn restore` with
  no doc lists the deleted ones. `check` tells a link to a deleted doc apart from a broken one.

### Upgrading
- `rename`, `delete` and `restore` are new event operations: a node on an older version keeps
  them out of its projection (and `check` reports them) until it is upgraded. Upgrade every node.

## [0.5.0] - 2026-10-22

### Added
- `cairn serve`: a local, read-only web view of the docs on 127.0.0.1, with markdown rendering
  (the `serve` extra), a home with a tab per repo and docs as cards, a filter bar, a doc side
  panel with timeline and activity, mermaid diagrams, and the doc's group of links as a diagram.
- `cairn link`, to link two docs by hand.
- A changelog, release tags and GitHub Releases. CI fails a pull request that changes `src/`
  without an entry under `[Unreleased]` (the `no-changelog` label skips the check), and a test
  keeps `__version__` equal to the latest released version here.
- The sync greeting carries the cairn version. Nodes on different versions keep syncing (the
  protocol decides that), and `cairn sync` shows a notice naming the node left behind, even with
  `--quiet`.

### Fixed
- The first open after an upgrade rebuilds the projection. Events an older version could not read
  used to stay out of it after the upgrade, until a manual `cairn rebuild`. An older version still
  running (an MCP server started before the upgrade) leaves the projection alone.
- A doc created outside any repo gets `repo: cross-repo` explicitly.

### Upgrading
- From any earlier version, `cairn --version` reported 0.0.1. Reinstall from the tag (see the
  README) and restart the open Claude Code sessions; the projection rebuilds itself.

## [0.4.0] - 2026-10-19

### Changed
- **BREAKING**: the data vocabulary is canonical in English. Front-matter fields, statuses and
  link kinds are written in English (`type`, `status`, `note`, `in_progress`, `discarded`, ...).
  Older logs keep their Spanish names and are read as aliases, forever; hand input with the old
  names is saved in English. A node older than 0.4.0 does not understand what a newer one writes:
  upgrade every node.
- **BREAKING**: CLI flags and positionals renamed without aliases (`--discard`, `--section`,
  `--lines`, `--by-repo`, `--to`, `weekly summary|full`), and so were the MCP tool parameters
  (`section`, `lines`, `full`, `by_repo`, `topic`, `type`, `discard`, `reason`, ...).
- **BREAKING**: the wip-doc tree moved out of the core: `import`, `export`, the wip-doc part of
  `check` and `integrate --replace-wip-doc` are now `cairn migrate wip-doc
  {import,export,check,unhook}`. `[docs] file_dirs` is empty by default.
- Messages, help, MCP texts and built-in templates are in English. Templates take `{title}`
  (and still `{titulo}`).
- The projection schema is version 2; a projection with another schema is dropped and rebuilt.

## [0.3.0] - 2026-10-15

### Added
- Every write (CLI or MCP, edits included) triggers a background sync, coalesced by a debounce
  (`[sync] debounce`, 5 s) and one run at a time per node.
- `cairn integrate systemd`: a user timer that syncs every `[sync] interval` minutes (15).
- `close --descartar "reason"` leaves a doc closed as discarded without finishing it.

## [0.2.0] - 2026-10-07

### Added
- `cairn_show` returns a long doc as an outline and reads it by section or by line range; the CLI
  `show` gains the same options.
- FTS search shows the matching fragment, the total and whether the results were cut; regex
  search is capped.
- `list` grouped by repo, with counts and the last-modified date (the MCP default).
- `restore --old-key` checks a restoring node's old keys against fingerprints noted outside it;
  keys show SHA256 fingerprints.

## [0.1.0] - 2026-10-05

### Added
- Append-only event log per node and segment, with a canonical format and content hash; events
  are signed with the node's ed25519 key.
- SQLite + FTS5 projection rebuilt from the log; `init` and `rebuild`.
- Doc lifecycle, promote, search, check and handoffs as events, with per-type templates and the
  working repo resolved from the cwd.
- Revision history (`log`, `show doc@rev`, `diff`), `edit` with state and link guards, and import
  from / export to the wip-doc tree.
- `cairn mcp`, a stdio MCP server, and `cairn integrate claude-code` for the MCP and the
  SessionStart hook.
- Sync over ssh (`authorize`, `revoke`, remotes), divergent branches settled with `resolve`, key
  rotation and restore of a node from a peer.
- Every user preference comes from the config.

[Unreleased]: https://github.com/PPeitsch/cairn/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/PPeitsch/cairn/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/PPeitsch/cairn/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/PPeitsch/cairn/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/PPeitsch/cairn/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/PPeitsch/cairn/releases/tag/v0.1.0
