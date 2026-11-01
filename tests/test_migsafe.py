"""Tests for migsafe rules."""

import pytest

from migsafe.parse import split_statements
from migsafe.rules import check_files, check_sql


def rules_for(sql: str) -> set[str]:
    return {f.rule for f in check_sql(sql).findings}


def test_parser_keeps_dollar_quoted_body_whole():
    sql = "CREATE FUNCTION f() RETURNS void AS $$ BEGIN PERFORM 1; END $$ LANGUAGE plpgsql;\nDROP TABLE q;"
    statements = split_statements(sql)
    assert len(statements) == 2
    assert "PERFORM 1" in statements[0].text
    assert statements[1].line == 2


def test_parser_ignores_semicolon_in_string_and_comment():
    sql = "INSERT INTO t VALUES ('a;b');\n-- drop; nothing\nDROP TABLE real;"
    statements = split_statements(sql)
    assert len(statements) == 2
    assert statements[1].text == "DROP TABLE real"


def test_drop_table_is_critical():
    findings = check_sql("SET lock_timeout = '3s'; DROP TABLE users;").findings
    assert findings[0].rule == "drop-table"
    assert findings[0].severity == "critical"


def test_unbounded_update_and_delete():
    assert "unbounded-write" in rules_for("SET lock_timeout='1s'; UPDATE users SET a = 1;")
    assert "unbounded-write" in rules_for("SET lock_timeout='1s'; DELETE FROM users;")


def test_batched_backfill_is_only_medium():
    findings = check_sql("SET lock_timeout='1s'; UPDATE users SET a = 1 WHERE id < 100;").findings
    assert [f.severity for f in findings if f.rule == "unbatched-backfill"] == ["medium"]


def test_index_concurrently_required_on_existing_table():
    sql = "SET lock_timeout='1s'; CREATE INDEX idx ON users (email);"
    assert "index-not-concurrent" in rules_for(sql)
    ok = "SET lock_timeout='1s'; CREATE INDEX CONCURRENTLY idx ON users (email);"
    assert "index-not-concurrent" not in rules_for(ok)


def test_index_on_new_table_is_fine():
    sql = "SET lock_timeout='1s'; CREATE TABLE orders (id int); CREATE INDEX idx ON orders (id);"
    assert "index-not-concurrent" not in rules_for(sql)


def test_add_not_null_column_without_default():
    sql = "SET lock_timeout='1s'; ALTER TABLE users ADD COLUMN age int NOT NULL;"
    assert "add-not-null-column" in rules_for(sql)


def test_volatile_default_detected():
    for default in ("now()", "random()", "gen_random_uuid()"):
        sql = f"SET lock_timeout='1s'; ALTER TABLE users ADD COLUMN c timestamptz DEFAULT {default};"
        assert "volatile-default" in rules_for(sql), default


def test_constraint_needs_not_valid():
    bad = "SET lock_timeout='1s'; ALTER TABLE orders ADD CONSTRAINT fk FOREIGN KEY (user_id) REFERENCES users (id);"
    good = "SET lock_timeout='1s'; ALTER TABLE orders ADD CONSTRAINT fk FOREIGN KEY (user_id) REFERENCES users (id) NOT VALID;"
    assert "constraint-without-not-valid" in rules_for(bad)
    assert "constraint-without-not-valid" not in rules_for(good)


def test_alter_type_and_set_not_null():
    assert "alter-column-type" in rules_for("SET lock_timeout='1s'; ALTER TABLE t ALTER COLUMN c TYPE bigint;")
    assert "set-not-null" in rules_for("SET lock_timeout='1s'; ALTER TABLE t ALTER COLUMN c SET NOT NULL;")


def test_missing_timeout_flagged_once():
    findings = check_sql("CREATE TABLE t (id int);").findings
    assert [f.rule for f in findings].count("no-timeout") == 1


def test_clean_migration_has_no_findings():
    sql = """SET lock_timeout = '3s';
SET statement_timeout = '30s';
CREATE TABLE audit (id bigserial primary key, note text);
CREATE INDEX CONCURRENTLY audit_note_idx ON audit (note);
"""
    report = check_sql(sql)
    assert report.findings == []
    assert report.worst == "none"


def test_report_counts_and_ordering():
    sql = "CREATE TABLE t (id int); DROP TABLE old; ALTER TABLE t ALTER COLUMN id TYPE bigint;"
    report = check_sql(sql)
    lines = [f.line for f in report.findings]
    assert lines == sorted(lines)
    assert report.counts()["critical"] == 1
    assert report.worst == "critical"


@pytest.mark.parametrize("begin", ["BEGIN", "BEGIN WORK", "BEGIN TRANSACTION", "START TRANSACTION", "start transaction"])
@pytest.mark.parametrize("index", ["CREATE INDEX CONCURRENTLY", "CREATE UNIQUE INDEX CONCURRENTLY"])
def test_concurrent_index_in_transaction_is_high_at_statement_line(begin, index):
    sql = f"SET lock_timeout='3s';\n{begin};\n{index} idx ON users (email);\nCOMMIT;"
    findings = check_sql(sql).findings
    assert len(findings) == 1
    finding = findings[0]
    assert finding.rule == "concurrent-index-in-transaction"
    assert finding.severity == "high"
    assert finding.line == 3
    assert finding.statement == f"{index} idx ON users (email)"
    assert "transaction" in finding.message.lower()
    assert "transaction" in finding.fix.lower()
    assert "outside" in finding.fix.lower()


@pytest.mark.parametrize("end", [
    "COMMIT", "COMMIT WORK", "COMMIT TRANSACTION", "ROLLBACK", "ROLLBACK WORK",
    "ROLLBACK TRANSACTION", "END", "ABORT", "COMMIT AND NO CHAIN", "ROLLBACK AND NO CHAIN",
])
def test_concurrent_index_after_transaction_end_is_not_flagged(end):
    sql = f"SET lock_timeout='3s';\nBEGIN;\nSELECT 1;\n{end};\nCREATE INDEX CONCURRENTLY idx ON users (email);"
    assert "concurrent-index-in-transaction" not in rules_for(sql)


def test_concurrent_index_without_transaction_is_not_flagged():
    sql = "SET lock_timeout='3s';\nCREATE INDEX CONCURRENTLY idx ON users (email);"
    assert check_sql(sql).findings == []


@pytest.mark.parametrize("prefix", [
    "CREATE FUNCTION f() RETURNS void AS $$ BEGIN PERFORM 1; END $$ LANGUAGE plpgsql;",
    "CREATE FUNCTION f() RETURNS void AS $body$ BEGIN PERFORM 1; END $body$ LANGUAGE plpgsql;",
    "-- BEGIN;\n/* START TRANSACTION; */",
    "SELECT 'BEGIN; START TRANSACTION;';",
])
def test_begin_in_body_comment_or_literal_does_not_open_transaction(prefix):
    sql = f"SET lock_timeout='3s';\n{prefix}\nCREATE INDEX CONCURRENTLY idx ON users (email);"
    assert "concurrent-index-in-transaction" not in rules_for(sql)


def test_concurrent_index_on_new_table_in_transaction_is_still_flagged():
    sql = """SET lock_timeout='3s';
BEGIN;
CREATE TABLE audit (id int);
CREATE INDEX CONCURRENTLY idx ON audit (id);
COMMIT;
"""
    findings = check_sql(sql).findings
    assert [(f.rule, f.severity, f.line) for f in findings] == [
        ("concurrent-index-in-transaction", "high", 4)
    ]


def test_transaction_state_is_not_shared_between_files():
    report = check_files({
        "first.sql": "SET lock_timeout='3s'; BEGIN;",
        "second.sql": "SET lock_timeout='3s'; CREATE INDEX CONCURRENTLY idx ON users (email);",
    })
    assert "concurrent-index-in-transaction" not in {f.rule for f in report.findings}


@pytest.mark.parametrize("rollback", [
    "ROLLBACK TO before_index", "ROLLBACK TO SAVEPOINT before_index",
    "ROLLBACK WORK TO SAVEPOINT before_index", "ROLLBACK TRANSACTION TO SAVEPOINT before_index",
])
def test_rollback_to_savepoint_keeps_transaction_active(rollback):
    sql = f"SET lock_timeout='3s';\nBEGIN;\nSAVEPOINT before_index;\n{rollback};\nCREATE INDEX CONCURRENTLY idx ON users (email);\nCOMMIT;"
    findings = check_sql(sql).findings
    assert [(f.rule, f.severity, f.line) for f in findings] == [
        ("concurrent-index-in-transaction", "high", 5)
    ]


@pytest.mark.parametrize("end", [
    "COMMIT AND CHAIN", "ROLLBACK AND CHAIN", "COMMIT WORK AND CHAIN", "ROLLBACK TRANSACTION AND CHAIN",
])
def test_chained_transaction_still_flags_concurrent_index(end):
    sql = f"SET lock_timeout='3s';\nBEGIN;\nSELECT 1;\n{end};\nCREATE INDEX CONCURRENTLY idx ON users (email);\nCOMMIT;"
    findings = check_sql(sql).findings
    assert [(f.rule, f.severity, f.line) for f in findings] == [
        ("concurrent-index-in-transaction", "high", 5)
    ]


@pytest.mark.parametrize("statement", [
    'CREATE INDEX "CONCURRENTLY" ON users (email)',
    "SELECT 'CREATE INDEX CONCURRENTLY idx ON users (email)'",
])
def test_concurrently_in_identifier_or_literal_is_not_index_option(statement):
    sql = f"SET lock_timeout='3s';\nBEGIN;\n{statement};\nCOMMIT;"
    assert "concurrent-index-in-transaction" not in rules_for(sql)


def test_each_concurrent_index_in_transaction_has_its_own_finding():
    sql = """SET lock_timeout='3s';
BEGIN;
CREATE INDEX CONCURRENTLY email_idx ON users (email);
CREATE INDEX CONCURRENTLY name_idx ON users (name);
COMMIT;
"""
    findings = check_sql(sql).findings
    assert [(f.rule, f.severity, f.line) for f in findings] == [
        ("concurrent-index-in-transaction", "high", 3),
        ("concurrent-index-in-transaction", "high", 4),
    ]


def test_clean_summary_names_the_installed_version():
    """Guards the drift where --version said 0.2.0 and the report said v0.1."""
    from migsafe import __version__
    from migsafe.rules import check_sql, to_text

    # a timeout must be set, or no-timeout fires and the clean branch is never reached
    text = to_text(check_sql("SET lock_timeout = '3s'; SET statement_timeout = '30s'; SELECT 1;"))
    assert "migsafe v%s" % __version__ in text
