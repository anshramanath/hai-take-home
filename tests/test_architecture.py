"""Tier 2, T4 of FIXES (1).md: static checks proving architecture claims
the docs make that nothing in the type system enforces on its own.
"""

from __future__ import annotations

import re
from pathlib import Path

HARNESS_ROOT = Path(__file__).resolve().parent.parent / "harness"

# The fake company's own data (section 5): what detection and context must
# never write to, as distinct from harness bookkeeping (attention_items),
# which raising an attention item legitimately does write to.
WORLD_TABLES = (
    "erp_parts", "erp_suppliers", "erp_purchase_orders", "erp_production_orders",
    "erp_receipts", "erp_lots", "erp_lot_allocations", "mail_messages", "cal_events",
    "users", "notifications",
)


def _read(*parts: str) -> str:
    return (HARNESS_ROOT / Path(*parts)).read_text()


def test_explain_queries_only_audit_log():
    text = _read("audit", "explain.py")
    tables = {match.upper() for match in re.findall(r"\bSELECT\b.*?\bFROM\s+(\w+)", text, re.IGNORECASE)}
    assert tables == {"AUDIT_LOG"}, f"explain.py queries something other than audit_log: {tables}"


def test_policy_and_execution_never_reference_memory_facts():
    for pkg in ("policy", "execution"):
        for path in (HARNESS_ROOT / pkg).rglob("*.py"):
            text = path.read_text()
            assert "memory_facts" not in text, f"{path} references memory_facts"
            assert "facts_for_prompt" not in text, f"{path} references facts_for_prompt"


def test_approvals_does_not_import_context():
    text = _read("policy", "approvals.py")
    assert not re.search(r"^\s*(import|from)\s+harness\.context\b", text, re.MULTILINE), (
        "approvals.py imports harness.context; escalation's calendar read must stay a "
        "separate policy read, never the user-facing provider"
    )


def test_detection_and_context_never_write_to_a_world_table():
    """The detectors themselves (stockout.py, quality_hold.py) and every
    context provider are read-only over the fake company's own tables.
    detection/registry.py does INSERT into attention_items, which is
    harness bookkeeping, not world data, and is exactly what "raising an
    attention item" means (section 8) -- that one write is not a
    violation of this claim, so this check is scoped to the eleven
    world tables specifically, not "zero SQL writes of any kind."
    """

    write_pattern = re.compile(r"\b(INSERT|UPDATE|DELETE)\b[^;\"]*", re.IGNORECASE)
    for pkg in ("detection", "context"):
        for path in (HARNESS_ROOT / pkg).rglob("*.py"):
            text = path.read_text()
            for match in write_pattern.finditer(text):
                statement = match.group(0)
                hit = next((t for t in WORLD_TABLES if t in statement), None)
                assert hit is None, f"{path} writes to world table {hit!r}: {statement!r}"


def test_every_test_name_cited_in_the_docs_actually_exists():
    """T11 (Tier 2): README.md, MODEL.md, and DESIGN.md sometimes name a
    specific test as evidence for a claim. If that test gets renamed or
    removed later, the doc would be citing a test that no longer proves
    anything -- this catches that drift.

    Matches on individual test *function* names (many words joined by
    underscores, e.g. test_quality_hold_fires_for_l2093_4820), not on
    references to a test *file* (e.g. "tests/test_gate.py"), which this
    repo's docs also cite legitimately and which this check leaves alone.
    """

    docs_root = HARNESS_ROOT.parent
    tests_root = HARNESS_ROOT.parent / "tests"

    defined = set()
    for path in tests_root.glob("test_*.py"):
        defined.update(re.findall(r"^def (test_[a-zA-Z0-9_]+)", path.read_text(), re.MULTILINE))

    cited: set[str] = set()
    for name in ("README.md", "MODEL.md", "DESIGN.md"):
        text = (docs_root / name).read_text()
        for match in re.finditer(r"\btest_[a-zA-Z0-9_]+", text):
            token = match.group(0)
            # A file reference ("test_gate.py" or "tests/test_gate.py"):
            # a bare filename-shaped citation, immediately followed by
            # ".py", not a long, specific, underscore-joined test name.
            if text[match.end():match.end() + 3] == ".py":
                continue
            cited.add(token)

    missing = sorted(name for name in cited if name not in defined)
    assert not missing, f"docs cite test names that don't exist in tests/: {missing}"


def test_every_schema_table_is_referenced_outside_world():
    schema = _read("world", "schema.sql")
    tables = re.findall(r"CREATE TABLE (\w+)", schema)
    assert tables, "no tables found in schema.sql; the parse itself is broken"

    other_files = [p for p in HARNESS_ROOT.rglob("*.py") if "world" not in p.relative_to(HARNESS_ROOT).parts]
    combined = "\n".join(p.read_text() for p in other_files)

    for table in tables:
        assert re.search(rf"\b{table}\b", combined), (
            f"table {table!r} is never referenced anywhere outside harness/world/"
        )
