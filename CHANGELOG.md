# Changelog

## v0.1.0 - 2026-10-29

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
