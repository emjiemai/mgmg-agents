"""The second lock on the analyst's SQL (2026-11-09): one read-only SELECT over the asker's own analyst.* views.

The database side (``db.fetch_analyst``): the query runs as a NOLOGIN role that may SELECT the analyst.* views and
nothing else, in a READ ONLY transaction with a short timeout and a server-side row limit, as a prepared statement
(the server refuses a second one). The role is switched with SET LOCAL ROLE on the app's own login, so SQL that
reached set_config('role') or RESET ROLE could switch back — this module's function allow-list is what stops that
(a separate LOGIN role for query_db would close it; it needs a password secret in Render, owner's call). This module
refuses everything else BEFORE it reaches the database, with a reason the model can fix in one retry:

  * exactly one statement, a SELECT (WITH … SELECT, UNION of SELECTs) — no writes, no DDL, no INTO, no FOR UPDATE,
    no COPY / SET / EXPLAIN / transaction control, no data-modifying or recursive CTE;
  * no comments, dollar quotes or backslashes (where a parser and the server could disagree);
  * every table is one of the asker's views (``analyst.x`` or bare ``x``) or a CTE of the same query;
  * every function is on the allow-list below — pg_sleep, dblink, lo_*, current_setting, pg_read_file … are not;
  * no system columns (ctid, xmin …), nothing named pg_*;
  * at most ``MAX_SQL_CHARS`` characters.

Parsed with sqlglot (pure Python, so it runs on the owner's Windows PC too), never with regular expressions alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

MAX_SQL_CHARS = 4000

# sqlglot's classes for the functions an analysis needs (aggregates, maths, text, dates, windows, casts).
ALLOWED_FUNCS = frozenset({
    "Sum", "Count", "Avg", "Min", "Max", "LogicalOr", "LogicalAnd", "Stddev", "Variance", "PercentileCont",
    "PercentileDisc", "Median", "GroupConcat", "ArrayAgg",
    "Abs", "Round", "Ceil", "Floor", "Sign", "Coalesce", "Nullif", "Greatest", "Least", "Case", "If",
    "Lower", "Upper", "Initcap", "Length", "StrPosition", "Trim", "SplitPart", "Concat", "ConcatWs", "Left", "Right",
    "Substring", "Cast", "TryCast", "Array",
    "CurrentDate", "CurrentTimestamp", "Extract", "TimestampTrunc", "DateTrunc", "TimeToStr", "StrToDate",
    "RowNumber", "Lag", "Lead", "FirstValue", "LastValue",
})
# Functions sqlglot doesn't know by class (exp.Anonymous): by name. name_key / similarity are analyst.*.
ALLOWED_ANON = frozenset({
    "name_key", "similarity", "word_similarity", "make_date", "replace", "rank", "dense_rank", "ntile",
    "percent_rank", "date_part", "to_char",
})
QUALIFIED_ANON = frozenset({"name_key", "similarity", "word_similarity"})  # may be written analyst.name_key(…)
SYSTEM_COLUMNS = frozenset({"ctid", "xmin", "xmax", "cmin", "cmax", "tableoid", "oid"})
FORBIDDEN_NODES = tuple(getattr(exp, n) for n in (
    "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "Command", "Into", "Lock", "Copy",
    "Transaction", "Commit", "Rollback", "Set", "Pragma", "Use", "Grant", "Describe", "Kill", "Cache", "Uncache",
    "LoadData", "TruncateTable", "Analyze",
) if hasattr(exp, n))


class SqlRejected(ValueError):
    """The query isn't allowed; the message says why, in words the model can act on."""


@dataclass
class Checked:
    sql: str                                  # what runs: the query as written, one trailing ';' dropped
    views: list[str] = field(default_factory=list)  # the analyst views it reads, for the audit row (no SQL there)


def validate(sql: str, allowed: set[str] | frozenset[str]) -> Checked:
    """The query if it may run for an asker allowed ``allowed`` views; ``SqlRejected`` with the reason otherwise."""
    text = (sql or "").strip()
    while text.endswith(";"):
        text = text[:-1].rstrip()
    if not text:
        raise SqlRejected("empty query — send one SELECT")
    if len(text) > MAX_SQL_CHARS:
        raise SqlRejected(f"query longer than {MAX_SQL_CHARS} characters — aggregate in SQL, ask for fewer columns")
    for token, what in (("--", "comments"), ("/*", "comments"), ("$$", "dollar quoting"), ("\\", "backslashes")):
        if token in text:
            raise SqlRejected(f"{what} are not allowed — send the bare SELECT")
    try:
        trees = [t for t in sqlglot.parse(text, read="postgres") if t is not None]
    except ParseError as exc:
        raise SqlRejected(f"could not parse the query ({str(exc).splitlines()[0][:160]})") from None
    if len(trees) != 1:
        raise SqlRejected("exactly one statement, please — a single SELECT")
    tree = trees[0]
    if not isinstance(tree, (exp.Select, exp.SetOperation)):
        raise SqlRejected("only SELECT is allowed (read-only)")
    for with_ in tree.find_all(exp.With):
        if with_.args.get("recursive"):
            # 09.11 review: a recursive CTE can grow one value past the database's memory inside the timeout.
            raise SqlRejected("WITH RECURSIVE is not allowed — the views need no recursion")
    for node in tree.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise SqlRejected(f"{type(node).__name__.upper()} is not allowed — only reading with SELECT")

    ctes = {(c.alias_or_name or "").lower() for c in tree.find_all(exp.CTE)}
    allowed_lower = {v.lower() for v in allowed}
    if ctes & allowed_lower:
        raise SqlRejected("a WITH name may not be the same as a view's name")
    views: list[str] = []
    for table in tree.find_all(exp.Table):
        try:
            name, db, catalog = (table.name or "").lower(), (table.db or "").lower(), (table.catalog or "")
        except AttributeError:
            raise SqlRejected("table functions are not allowed — read the views") from None
        if not name or catalog:
            raise SqlRejected("table functions and three-part names are not allowed — read the views")
        if not db and name in ctes:
            continue
        if db not in ("", "analyst") or name not in allowed_lower:
            shown = f"{db}.{name}" if db else name
            raise SqlRejected(f"«{shown}» is not one of your views — use only: {', '.join(sorted(allowed_lower))}")
        if name not in views:
            views.append(name)

    for func in tree.find_all(exp.Func):
        if isinstance(func, exp.Anonymous):
            name = (func.name or "").lower()
            if name not in ALLOWED_ANON:
                raise SqlRejected(f"function {name}() is not allowed")
            parent = func.parent
            if isinstance(parent, exp.Dot):
                owner = (parent.this.name if isinstance(parent.this, exp.Expression) else str(parent.this)).lower()
                if owner != "analyst" or name not in QUALIFIED_ANON:
                    raise SqlRejected(f"{owner}.{name}() is not allowed — functions are plain or analyst.*")
        elif type(func).__name__ not in ALLOWED_FUNCS:
            raise SqlRejected(f"function {func.sql_name().lower()}() is not allowed")

    for column in tree.find_all(exp.Column):
        if (column.name or "").lower() in SYSTEM_COLUMNS:
            raise SqlRejected(f"system column {column.name} is not allowed")
    for ident in tree.find_all(exp.Identifier):
        if str(ident.this or "").lower().startswith("pg_"):
            raise SqlRejected("pg_* names are not allowed")
    return Checked(sql=text, views=views)
