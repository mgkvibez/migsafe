# migsafe

**This migration would have locked your `users` table. migsafe tells you before you merge it.** Static analysis for PostgreSQL migrations: locks, rewrites, destructive steps, missing timeouts.

[![ci](https://github.com/mgkvibez/migsafe/actions/workflows/ci.yml/badge.svg)](https://github.com/mgkvibez/migsafe/actions/workflows/ci.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![PyPI](https://img.shields.io/pypi/v/migsafe-pg.svg)](https://pypi.org/project/migsafe-pg/)
[![postgres](https://img.shields.io/badge/postgresql-12%2B-336791.svg)](https://www.postgresql.org/)
[![stars](https://img.shields.io/github/stars/mgkvibez/migsafe?style=flat&color=yellow)](https://github.com/mgkvibez/migsafe/stargazers)

![migsafe demo](docs/demo/migsafe.svg)

## Install

```bash
pipx install git+https://github.com/mgkvibez/migsafe.git   # works today
pipx install migsafe-pg                                    # once the PyPI release is live
```

The distribution is published to PyPI as `migsafe-pg` because the short name is
already taken by an unrelated project. The command, the import package and the
repo are all still `migsafe`. Zero runtime dependencies, Python 3.9+.
bash
pipx install git+https://github.com/mgkvibez/migsafe.git
```

## Three ways to use it

```bash
# 1. Check one migration
migsafe check migrations/2026_10_29_add_user_flags.sql

# 2. Check the whole directory (fails the build on high and above)
migsafe check migrations/ --fail-on high

# 3. Machine-readable, for a bot or an agent skill
migsafe check migrations/ --json
```

Exit code is `2` when a finding at or above `--fail-on` exists, so `migsafe check migrations/ && psql ...` is safe by default.

## Why it exists

`ALTER TABLE users ADD COLUMN last_seen timestamptz DEFAULT now()` is four words of difference from a
statement that rewrites the whole table under an `ACCESS EXCLUSIVE` lock. It passes review because
it looks like the fifty migrations before it. Then it runs at 14:00 and takes the site down for
forty seconds, and everyone learns the same lesson the hard way.

The linter encodes that lesson: what takes a lock, what rewrites rows, what cannot be undone, and
what to write instead.

## How it works

Splits the file into statements — dollar-quoted blocks, string literals and comments are handled, so a
`;` inside a function body does not confuse it — then applies rules:

| Severity | Rule | Why it matters |
|---|---|---|
| critical | `drop-table`, `drop-column`, `truncate` | data loss that a rollback does not undo after commit |
| critical | `unbounded-write` | `UPDATE`/`DELETE` with no `WHERE` touches every row |
| high | `volatile-default` | `DEFAULT now()` on `ADD COLUMN` rewrites the table |
| high | `add-not-null-column` | `NOT NULL` with no default fails on non-empty tables |
| high | `index-not-concurrent` | `CREATE INDEX` blocks writes for the whole build |
| high | `alter-column-type` | type changes rewrite the table and drop dependent indexes |
| high | `constraint-without-not-valid` | adding a FK or CHECK validates every row under a lock |
| medium | `unbatched-backfill`, `multiple-alter-ops`, `rename`, `set-not-null`, `no-timeout` | slower failure modes and lock pile-ups |

It knows what the same file creates: an index on a table you are creating in this migration is not
flagged, because there is nothing to lock yet.

Every finding carries the statement line and a fix you can paste.

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md). Tests:

```bash
python -m pytest -q
```

## License

MIT. See [LICENSE](LICENSE).
