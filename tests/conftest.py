"""Shared test fixtures for the expense tracker test suite."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from expense_tracker.db import ensure_schema, migrate_schema


# ---------------------------------------------------------------------------
# Synthetic UTF-16 CSV builder
# ---------------------------------------------------------------------------


def make_utf16_csv(
    path: Path,
    rows: list[tuple[str, str, str, str, str, str]],
    date_from: str = "01-01-2026",
    date_to: str = "31-01-2026",
    encoding: str = "utf-16-le",
) -> Path:
    """
    Create a synthetic UTF-16 CSV file.

    Each row is a tuple: (date_posted, date_value, description, amount, type, balance)
    Dates are dd-mm-YYYY, amounts use comma decimals (Portuguese format).
    """
    lines = [
        f"Data de:;{date_from}",
        f"Data até:;{date_to}",
        "Conta:;123456789",
        "",
        "Data lançamento;Data valor;Descrição;Montante;Tipo;Saldo",
    ]
    for row in rows:
        lines.append(";".join(row))
    lines.append("")  # trailing newline

    content = "\n".join(lines)
    path.write_bytes(content.encode(encoding))
    return path


# Reusable sample transaction rows
SAMPLE_ROWS = [
    ("15-01-2026", "15-01-2026", "COMPRA 1234 CONTINENTE PORTO",       "-45,50", "Compra", "1234,56"),
    ("14-01-2026", "14-01-2026", "COMPRA 5678 FARMACIA DA GARE 1000-001 LISBOA", "-12,80", "Compra", "1280,06"),
    ("13-01-2026", "13-01-2026", "DD VODAFONE PORTU 01234567890 PT12345678", "-35,99", "Débito", "1292,86"),
    ("12-01-2026", "12-01-2026", "TRF. P/O EXEMPLO SEGUROS SAUDE,SA", "-150,00", "Transf.", "1328,85"),
    ("10-01-2026", "10-01-2026", "TRANSFERENCIA - SALARIO",              "2500,00", "Crédito", "1478,85"),
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def utf16_csv(tmp_path: Path) -> Path:
    """A synthetic UTF-16 CSV file with 5 sample transactions."""
    return make_utf16_csv(tmp_path / "EXPORT_0_1012026.csv", SAMPLE_ROWS)


@pytest.fixture
def cards_csv(tmp_path: Path) -> Path:
    """A sample account-holders.csv with two cards."""
    p = tmp_path / "account-holders.csv"
    p.write_text("card_last4,name\n1234,Alice\n5678,Bob\n", encoding="utf-8")
    return p


@pytest.fixture
def rules_csv(tmp_path: Path) -> Path:
    """A sample rules.csv with a few categorization rules."""
    p = tmp_path / "rules.csv"
    p.write_text(
        "pattern,match_field,category,subcategory,payment_type\n"
        "CONTINENTE,description,Groceries,,card\n"
        "FARMACIA,description,Health,Pharmacy,card\n"
        "VODAFONE,description,Utilities,Phone,direct_debit\n"
        "SALARIO,description_raw,Income,Salary,\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture
def noise_words(tmp_path: Path) -> Path:
    """A sample noise-words.txt."""
    p = tmp_path / "noise-words.txt"
    p.write_text(
        "# Test noise words\n"
        "CONTACTLESS\n"
        "PT\n"
        "PORTO\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture
def cleaning_patterns(tmp_path: Path) -> Path:
    """A sample cleaning-patterns.csv."""
    p = tmp_path / "cleaning-patterns.csv"
    p.write_text(
        'type,pattern,description\n'
        'prefix,COMPRA\\s+\\d{4}\\s*,Card purchase prefix\n'
        'prefix,DD\\s+,Direct debit prefix\n'
        'noise,\\b\\d{4}-\\d{3}\\b,Portuguese postal codes\n'
        '"noise","\\b(?=[A-Z0-9]*\\d)[A-Z0-9]{8,12}\\b","Transaction ref codes"\n',
        encoding="utf-8",
    )
    return p


@pytest.fixture
def test_db(tmp_path: Path) -> Path:
    """An empty SQLite database with the correct schema."""
    db_path = tmp_path / "data" / "ledger.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    ensure_schema(conn)
    migrate_schema(conn)
    conn.close()
    return db_path


@pytest.fixture
def populated_db(test_db: Path, utf16_csv: Path, cards_csv: Path, noise_words: Path, cleaning_patterns: Path) -> Path:
    """A database populated with sample transactions from the UTF-16 CSV."""
    from expense_tracker.db import ingest
    from expense_tracker.parser import reset_cleaning_cache, _get_cleaning_patterns

    reset_cleaning_cache()
    _get_cleaning_patterns(noise_words, cleaning_patterns)

    ingest(test_db, [utf16_csv], cards_path=cards_csv)
    return test_db
