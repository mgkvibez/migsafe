"""Rules for dangerous PostgreSQL migrations."""

from __future__ import annotations

from . import __version__

import re
from dataclasses import dataclass, field

from .parse import Statement, split_statements

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

VOLATILE_DEFAULT_RE = re.compile(
    r"DEFAULT\s+(NOW\(\)|CURRENT_TIMESTAMP|CLOCK_TIMESTAMP\(\)|RANDOM\(\)|"
    r"GEN_RANDOM_UUID\(\)|UUID_GENERATE_V4\(\)|NEXTVAL\()",
    re.IGNORECASE,
)
CREATE_TABLE_RE = re.compile(r"^CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.\"]+)", re.IGNORECASE)
CREATE_INDEX_RE = re.compile(
    r"^CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?[\w\"]*\s*"
    r"ON\s+(?:ONLY\s+)?([\w.\"]+)",
    re.IGNORECASE,
)
CONCURRENT_INDEX_RE = re.compile(r"^CREATE\s+(?:UNIQUE\s+)?INDEX\s+CONCURRENTLY\b", re.IGNORECASE)
TIMEOUT_RE = re.compile(r"(STATEMENT_TIMEOUT|LOCK_TIMEOUT)", re.IGNORECASE)


@dataclass
class Finding:
    """One dangerous operation."""

    rule: str
    severity: str
    line: int
    message: str
    fix: str
    statement: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "line": self.line,
            "message": self.message,
            "fix": self.fix,
            "statement": self.statement[:200],
        }


@dataclass
class Report:
    """All findings for one or more migration files."""

    findings: list[Finding] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    statements: int = 0

    @property
    def worst(self) -> str:
        if not self.findings:
            return "none"
        return min(self.findings, key=lambda f: SEVERITY_ORDER[f.severity]).severity

    def counts(self) -> dict[str, int]:
        out = {key: 0 for key in SEVERITY_ORDER}
        for finding in self.findings:
            out[finding.severity] += 1
        return out


def _new_tables(statements: list[Statement]) -> set[str]:
    """Tables created in this file; index builds on them cannot block existing readers."""
    tables: set[str] = set()
    for statement in statements:
        match = CREATE_TABLE_RE.search(statement.text)
        if match:
            tables.add(match.group(1).strip('"').lower())
    return tables


def check_statement(statement: Statement, new_tables: set[str]) -> list[Finding]:
    """Apply every rule to a single statement."""
    text = statement.text
    upper = statement.upper
    found: list[Finding] = []

    if re.match(r"^DROP\s+TABLE\b", upper):
        found.append(Finding(
            rule="drop-table", severity="critical", line=statement.line,
            message="DROP TABLE destroys data and cannot be undone by rollback after commit",
            fix="Rename the table instead, drop it in a later release once nothing references it",
            statement=text,
        ))

    if re.search(r"\bDROP\s+COLUMN\b", upper):
        found.append(Finding(
            rule="drop-column", severity="critical", line=statement.line,
            message="DROP COLUMN removes data immediately",
            fix="Stop reading the column first, then drop it in a separate release",
            statement=text,
        ))

    if re.match(r"^TRUNCATE\b", upper):
        found.append(Finding(
            rule="truncate", severity="critical", line=statement.line,
            message="TRUNCATE takes an ACCESS EXCLUSIVE lock and deletes every row",
            fix="Delete in batches with a WHERE clause, or run it in a maintenance window",
            statement=text,
        ))

    if re.match(r"^(UPDATE|DELETE)\b", upper):
        has_where = re.search(r"\bWHERE\b", upper) is not None
        if not has_where:
            found.append(Finding(
                rule="unbounded-write", severity="critical", line=statement.line,
                message=f"{upper.split()[0]} without WHERE touches every row and locks them",
                fix="Add a WHERE clause and batch the operation",
                statement=text,
            ))
        elif re.match(r"^UPDATE\b", upper) and not re.search(r"\bLIMIT\b", upper):
            found.append(Finding(
                rule="unbatched-backfill", severity="medium", line=statement.line,
                message="UPDATE with WHERE but no LIMIT rewrites every matching row in one transaction",
                fix="Backfill in batches (WHERE id BETWEEN ... AND ...) so locks stay short",
                statement=text,
            ))

    index_match = CREATE_INDEX_RE.search(text)
    if index_match and "CONCURRENTLY" not in upper:
        table = index_match.group(1).strip('"').lower()
        if table not in new_tables:
            found.append(Finding(
                rule="index-not-concurrent", severity="high", line=statement.line,
                message=f"CREATE INDEX on existing table '{table}' blocks writes for the whole build",
                fix="Use CREATE INDEX CONCURRENTLY (outside a transaction) on existing tables",
                statement=text,
            ))

    if "ADD COLUMN" in upper:
        if re.search(r"\bNOT\s+NULL\b", upper) and "DEFAULT" not in upper:
            found.append(Finding(
                rule="add-not-null-column", severity="high", line=statement.line,
                message="Adding a NOT NULL column without DEFAULT fails on a non-empty table",
                fix="Add the column nullable, backfill, then SET NOT NULL; or add it WITH a default",
                statement=text,
            ))
        volatile = VOLATILE_DEFAULT_RE.search(text)
        if volatile:
            found.append(Finding(
                rule="volatile-default", severity="high", line=statement.line,
                message=f"Volatile DEFAULT ({volatile.group(1)}) rewrites the table and takes a long lock",
                fix="Add the column without a default, backfill in batches, then set the default",
                statement=text,
            ))

    if re.search(r"ALTER\s+COLUMN\s+\S+\s+(SET\s+DATA\s+)?TYPE\b", upper):
        found.append(Finding(
            rule="alter-column-type", severity="high", line=statement.line,
            message="Changing a column type rewrites the table and blocks reads and writes",
            fix="Add a new column, dual-write, backfill, then swap and drop the old column",
            statement=text,
        ))

    if re.search(r"\bADD\s+CONSTRAINT\b", upper) and "NOT VALID" not in upper:
        if re.search(r"\b(FOREIGN\s+KEY|CHECK)\b", upper):
            found.append(Finding(
                rule="constraint-without-not-valid", severity="high", line=statement.line,
                message="Adding a FK or CHECK constraint validates every row under an exclusive lock",
                fix="Add it with NOT VALID, then VALIDATE CONSTRAINT in a separate migration",
                statement=text,
            ))

    if re.match(r"^ALTER\s+TABLE\b", upper) and text.count(",") > 1:
        found.append(Finding(
            rule="multiple-alter-ops", severity="medium", line=statement.line,
            message="Several ALTER operations in one statement hold their locks for the longest one",
            fix="Split into separate statements so each lock is short",
            statement=text,
        ))

    if re.search(r"\bRENAME\b", upper):
        found.append(Finding(
            rule="rename", severity="medium", line=statement.line,
            message="Renames break running application code that still uses the old name",
            fix="Add the new name alongside, migrate readers, then remove the old one",
            statement=text,
        ))

    if re.search(r"\bSET\s+NOT\s+NULL\b", upper):
        found.append(Finding(
            rule="set-not-null", severity="medium", line=statement.line,
            message="SET NOT NULL scans the whole table under an exclusive lock",
            fix="Add a CHECK (col IS NOT NULL) NOT VALID, validate it, then SET NOT NULL",
            statement=text,
        ))

    return found


def check_sql(sql: str, filename: str = "<sql>") -> Report:
    """Check one migration."""
    return check_files({filename: sql})


def check_files(files: dict[str, str]) -> Report:
    """Check several migrations; new-table knowledge is shared across the set."""
    report = Report(files=list(files))
    all_statements: dict[str, list[Statement]] = {}
    for name, sql in files.items():
        statements = split_statements(sql)
        all_statements[name] = statements
        report.statements += len(statements)

    new_tables: set[str] = set()
    for statements in all_statements.values():
        new_tables |= _new_tables(statements)

    for name, statements in all_statements.items():
        sql = files[name]
        in_transaction = False
        for statement in statements:
            report.findings.extend(check_statement(statement, new_tables))
            upper = statement.upper
            if re.match(r"^(BEGIN|START\s+TRANSACTION)\b", upper):
                in_transaction = True
            elif re.match(r"^(COMMIT|ROLLBACK|END|ABORT)\b", upper):
                # A savepoint rollback does not end the surrounding transaction.
                if not re.match(r"^ROLLBACK(?:\s+(?:WORK|TRANSACTION))?\s+TO\b", upper):
                    in_transaction = in_transaction and bool(re.search(r"\bAND\s+CHAIN$", upper))

            if in_transaction and CONCURRENT_INDEX_RE.match(statement.text):
                report.findings.append(Finding(
                    rule="concurrent-index-in-transaction", severity="high", line=statement.line,
                    message="CREATE INDEX CONCURRENTLY fails inside a transaction block, aborting the migration",
                    fix="Run this index statement alone, outside a transaction block",
                    statement=statement.text,
                ))
        if statements and not TIMEOUT_RE.search(sql):
            report.findings.append(Finding(
                rule="no-timeout", severity="medium", line=1,
                message="Migration sets no statement_timeout or lock_timeout",
                fix="Prefix the migration with SET lock_timeout = '3s'; SET statement_timeout = '30s'",
            ))

    report.findings.sort(key=lambda f: (SEVERITY_ORDER[f.severity], f.line))
    return report


def to_text(report: Report) -> str:
    """Human-readable report."""
    if not report.findings:
        return (
            f"No issues found by migsafe v{__version__} across {report.statements} statements "
            f"in {len(report.files)} file(s).\n"
            "That means no known dangerous shape matched; it is not a guarantee."
        )
    out = [f"migsafe: {len(report.findings)} finding(s), worst {report.worst}", ""]
    for finding in report.findings:
        out.append(f"[{finding.severity.upper()}] line {finding.line} ({finding.rule})")
        out.append(f"  what: {finding.message}")
        out.append(f"  fix:  {finding.fix}")
        out.append("")
    counts = report.counts()
    out.append("  ".join(f"{k}:{v}" for k, v in counts.items() if v))
    return "\n".join(out)
