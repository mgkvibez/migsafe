# Changelog

## v0.1.2 - 2026-10-29

- Fix the reported version: `--version` said 0.1.0 while the package was
  0.1.1, because the module constant and the package metadata were bumped
  separately. The release gate now fails when they disagree.
## v0.1.1 - 2026-10-29

- First PyPI release as **migsafe-pg** (the bare name is taken by an unrelated
  project). The command, import package and repository stay `migsafe`.
- `migsafe check path` is now accepted as well as a bare path list.
- Release pipeline via PyPI trusted publishing, plus first-time contributor
  auto-approval.
## v0.1.0 - 2026-10-29

- PyPI distribution name is `migsafe-pg` (the bare name `migsafe` is
  taken by an unrelated project); command, import package and repo stay `migsafe`

- `migsafe check migrations/*.sql` flags the PostgreSQL migration that would lock
  or rewrite a table in production, before it merges.
- 14 rules across three severities. Critical: `drop-table`, `drop-column`,
  `truncate`, `unbounded-write`. High: `index-not-concurrent`,
  `add-not-null-column`, `volatile-default`, `alter-column-type`,
  `constraint-without-not-valid`. Medium: `unbatched-backfill`,
  `multiple-alter-ops`, `rename`, `set-not-null`, `no-timeout`.
- Statement splitter is dollar-quote, string and comment aware, so a function body
  or a semicolon inside a literal never fabricates a statement.
- An index on a table the same migration creates is not flagged, which is the
  mistake the naive version of this check makes.
- Exits 2 when any high or critical finding exists, so it drops into CI directly.
- Worked example in `examples/lock_users_table.sql`.
- 14 tests.
- Install: `pipx install git+https://github.com/mgkvibez/migsafe.git`.
