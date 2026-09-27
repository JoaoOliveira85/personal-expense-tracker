"""
Portuguese starter rules — a curated knowledge base of common Portuguese
merchants and service providers, organized by category.

These rules can be imported into the user's rules.csv to bootstrap
categorization for the most recognizable transaction patterns.
"""
from __future__ import annotations

import csv
from pathlib import Path

from .rules import RULES_HEADER, load_rules

# ---------------------------------------------------------------------------
# Curated Portuguese merchant/service patterns
# ---------------------------------------------------------------------------
# Each tuple: (pattern, match_field, category, subcategory, payment_type)
#
# Guidelines:
# - Patterns are case-insensitive and must start at a word boundary; a
#   trailing space ("BP ") also requires a word boundary at the end
# - The longest matching pattern wins, so order within the list does not
#   matter; add a more specific pattern to override a broad one
# - payment_type is left empty unless strongly implied (e.g. DD = direct_debit)
# - match_field is "description" (matches both raw and cleaned) unless the
#   pattern only makes sense against the raw bank text
# ---------------------------------------------------------------------------

STARTER_RULES: list[tuple[str, str, str, str, str]] = [
    # ══════════════════════════════════════════════════════════════════════
    # GROCERIES & FOOD SHOPPING
    # ══════════════════════════════════════════════════════════════════════
    
    # ── Supermarkets ──────────────────────────────────────────────────────
    ("CONTINENTE", "description", "Groceries", "Supermarket", ""),
    ("PINGO DOCE", "description", "Groceries", "Supermarket", ""),
    ("LIDL", "description", "Groceries", "Supermarket", ""),
    ("ALDI", "description", "Groceries", "Supermarket", ""),
    ("MINIPRECO", "description", "Groceries", "Supermarket", ""),
    ("MINIPREÇO", "description", "Groceries", "Supermarket", ""),
    ("MERCADONA", "description", "Groceries", "Supermarket", ""),
    ("INTERMARCHE", "description", "Groceries", "Supermarket", ""),
    ("AUCHAN", "description", "Groceries", "Supermarket", ""),
    ("E.LECLERC", "description", "Groceries", "Supermarket", ""),
    ("JUMBO", "description", "Groceries", "Supermarket", ""),
    ("COVIRAN", "description", "Groceries", "Supermarket", ""),
    ("FROIZ", "description", "Groceries", "Supermarket", ""),
    ("SUPERMERCADO FROIZ", "description", "Groceries", "Supermarket", ""),
    ("APOLONIA", "description", "Groceries", "Supermarket", ""),
    ("SPAR", "description", "Groceries", "Supermarket", ""),
    ("PRIMAPRIX", "description", "Groceries", "Supermarket", ""),
    
    # ── Specialty Food Shops ──────────────────────────────────────────────
    ("TALHO", "description", "Groceries", "Butcher", ""),
    ("PEIXARIA", "description", "Groceries", "Fish Market", ""),
    ("BACALHAU", "description", "Groceries", "Fish Market", ""),
    ("FRUTARIA", "description", "Groceries", "Produce", ""),
    ("MERCEARIA", "description", "Groceries", "Grocery Store", ""),
    ("CELEIRO", "description", "Groceries", "Health Food", ""),
    ("ERVANARIO", "description", "Groceries", "Health Food", ""),
    
    # ── Bakeries ──────────────────────────────────────────────────────────
    ("PADARIA", "description", "Groceries", "Bakery", ""),
    ("PASTELARIA", "description", "Groceries", "Bakery", ""),
    ("CONFEITARIA", "description", "Groceries", "Bakery", ""),
    ("PAO ", "description", "Groceries", "Bakery", ""),

    # ══════════════════════════════════════════════════════════════════════
    # UTILITIES & BILLS
    # ══════════════════════════════════════════════════════════════════════
    
    # ── Electricity ───────────────────────────────────────────────────────
    ("EDP", "description", "Utilities", "Electricity", ""),
    ("ENDESA", "description", "Utilities", "Electricity", ""),
    ("IBERDROLA", "description", "Utilities", "Electricity", ""),
    ("GOLDENERGY", "description", "Utilities", "Electricity", ""),
    ("SU ELETRICIDAD", "description", "Utilities", "Electricity", "direct_debit"),
    ("ELETRICIDADE", "description", "Utilities", "Electricity", ""),
    
    # ── Gas ───────────────────────────────────────────────────────────────
    ("GALP ENERGIA", "description", "Utilities", "Gas", ""),
    
    # ── Water ─────────────────────────────────────────────────────────────
    ("AGUAS DE PORTUGAL", "description", "Utilities", "Water", ""),
    ("AGUAS DO PORTO", "description", "Utilities", "Water", ""),
    ("AGUAS PORTO", "description", "Utilities", "Water", "direct_debit"),
    ("AGUAS E ENERGI", "description", "Utilities", "Water", "direct_debit"),
    ("EPAL", "description", "Utilities", "Water", ""),
    ("SMAS", "description", "Utilities", "Water", ""),
    ("INDAQUA", "description", "Utilities", "Water", ""),

    # ── Telecoms ──────────────────────────────────────────────────────────
    ("MEO", "description", "Utilities", "Telecoms", ""),
    ("NOS COMUNICACOES", "description", "Utilities", "Telecoms", ""),
    ("VODAFONE", "description", "Utilities", "Telecoms", "direct_debit"),
    ("NOWO", "description", "Utilities", "Telecoms", ""),
    ("DIGI PORTUGAL", "description", "Utilities", "Telecoms", ""),

    # ══════════════════════════════════════════════════════════════════════
    # TRANSPORT
    # ══════════════════════════════════════════════════════════════════════
    
    # ── Fuel ──────────────────────────────────────────────────────────────
    ("GALP", "description", "Transport", "Fuel", ""),
    ("REPSOL", "description", "Transport", "Fuel", ""),
    ("CEPSA", "description", "Transport", "Fuel", ""),
    ("PRIO ENERGY", "description", "Transport", "Fuel", ""),
    ("BP ", "description", "Transport", "Fuel", ""),
    
    # ── Tolls ─────────────────────────────────────────────────────────────
    ("VIA VERDE", "description", "Transport", "Tolls", ""),
    ("BRISA", "description", "Transport", "Tolls", ""),
    
    # ── Public Transport ──────────────────────────────────────────────────
    ("CP COMBOIOS", "description", "Transport", "Train", ""),
    ("CP PORTO", "description", "Transport", "Train", ""),
    ("FERTAGUS", "description", "Transport", "Train", ""),
    ("METRO DO PORTO", "description", "Transport", "Metro", ""),
    ("METRO LISBOA", "description", "Transport", "Metro", ""),
    ("METROPOLITANO", "description", "Transport", "Metro", ""),
    ("STCP", "description", "Transport", "Bus", ""),
    ("CARRIS", "description", "Transport", "Bus", ""),
    ("ANDANTE", "description", "Transport", "Metro", ""),
    ("APP ANDA", "description", "Transport", "Metro", ""),
    ("TMP ", "description", "Transport", "Parking", ""),
    ("TRAN.URB", "description", "Transport", "Public Transport", ""),
    
    # ── Ride-hailing & Scooters ───────────────────────────────────────────
    ("UBER", "description", "Transport", "Ride-hailing", ""),
    ("BOLT", "description", "Transport", "Ride-hailing", ""),
    ("FREE NOW", "description", "Transport", "Ride-hailing", ""),
    ("LIME", "description", "Transport", "Scooter", ""),
    ("BIRD", "description", "Transport", "Scooter", ""),

    # ══════════════════════════════════════════════════════════════════════
    # HEALTH & WELLNESS
    # ══════════════════════════════════════════════════════════════════════
    
    # ── Pharmacies ────────────────────────────────────────────────────────
    ("FARMACIA", "description", "Health", "Pharmacy", ""),
    ("FARM.", "description", "Health", "Pharmacy", ""),
    ("PARAFARMACIA", "description", "Health", "Pharmacy", ""),
    ("WELLS", "description", "Health", "Pharmacy", ""),
    
    # ── Health Insurance ──────────────────────────────────────────────────
    ("MEDIS", "description", "Health", "Insurance", ""),
    ("MEDICARE", "description", "Health", "Insurance", ""),
    
    # ── Hospitals & Clinics ───────────────────────────────────────────────
    ("HOSPITAL", "description", "Health", "Hospital", ""),
    ("CLINICA", "description", "Health", "Clinic", ""),
    ("CENTRO HOSPITALAR", "description", "Health", "Hospital", ""),
    ("CUF", "description", "Health", "Hospital", ""),
    ("JOSE MELLO SAUDE", "description", "Health", "Hospital", ""),
    ("LUZ SAUDE", "description", "Health", "Hospital", ""),
    ("LUSIADAS", "description", "Health", "Hospital", ""),
    ("SNS", "description", "Health", "", ""),
    
    # ── Optical ───────────────────────────────────────────────────────────
    ("OPTIC", "description", "Health", "Optical", ""),
    ("OCULISTA", "description", "Health", "Optical", ""),
    
    # ── Mental Health ─────────────────────────────────────────────────────
    ("PSICOLOG", "description", "Health", "Mental Health", ""),

    # ══════════════════════════════════════════════════════════════════════
    # INSURANCE
    # ══════════════════════════════════════════════════════════════════════
    ("ALLIANZ", "description", "Insurance", "", ""),
    ("TRANQUILIDADE", "description", "Insurance", "", ""),
    ("AGEAS", "description", "Insurance", "", ""),
    ("GENERALI", "description", "Insurance", "", ""),
    ("MAPFRE", "description", "Insurance", "", ""),
    ("ZURICH", "description", "Insurance", "", ""),
    ("OCIDENTAL SEGUROS", "description", "Insurance", "", ""),
    ("OK TELESEGUROS", "description", "Insurance", "", ""),
    ("LOGO SEGUROS", "description", "Insurance", "", ""),
    ("UNIVERSO", "description", "Insurance", "Credit Card", "direct_debit"),

    # ══════════════════════════════════════════════════════════════════════
    # EATING OUT & RESTAURANTS
    # ══════════════════════════════════════════════════════════════════════
    
    # ── Fast Food ─────────────────────────────────────────────────────────
    ("MCDONALD", "description", "Eating Out", "Fast Food", ""),
    ("BURGER KING", "description", "Eating Out", "Fast Food", ""),
    ("KFC", "description", "Eating Out", "Fast Food", ""),
    ("PIZZA HUT", "description", "Eating Out", "Fast Food", ""),
    ("DOMINOS", "description", "Eating Out", "Fast Food", ""),
    ("TELEPIZZA", "description", "Eating Out", "Fast Food", ""),
    ("SUBWAY", "description", "Eating Out", "Fast Food", ""),
    ("PANS E COMPANY", "description", "Eating Out", "Fast Food", ""),
    
    # ── Restaurants ───────────────────────────────────────────────────────
    ("RESTAURANTE", "description", "Eating Out", "Restaurant", ""),
    ("REST.", "description", "Eating Out", "Restaurant", ""),
    
    # ── Coffee Shops ──────────────────────────────────────────────────────
    ("STARBUCKS", "description", "Eating Out", "Coffee", ""),
    ("JERONYMO", "description", "Eating Out", "Coffee", ""),
    ("CAFE ", "description", "Eating Out", "Coffee", ""),
    ("CAFETARIA", "description", "Eating Out", "Coffee", ""),
    
    # ── Food Delivery ─────────────────────────────────────────────────────
    ("UBER EATS", "description", "Eating Out", "Delivery", ""),
    ("GLOVO", "description", "Eating Out", "Delivery", ""),
    ("BOLT FOOD", "description", "Eating Out", "Delivery", ""),

    # ══════════════════════════════════════════════════════════════════════
    # SUBSCRIPTIONS & DIGITAL SERVICES
    # ══════════════════════════════════════════════════════════════════════
    
    # ── Streaming ─────────────────────────────────────────────────────────
    ("NETFLIX", "description", "Subscriptions", "Streaming", ""),
    ("SPOTIFY", "description", "Subscriptions", "Streaming", ""),
    ("HBO MAX", "description", "Subscriptions", "Streaming", ""),
    ("DISNEY PLUS", "description", "Subscriptions", "Streaming", ""),
    ("DISNEY+", "description", "Subscriptions", "Streaming", ""),
    ("AMAZON PRIME", "description", "Subscriptions", "Streaming", ""),
    ("YOUTUBE", "description", "Subscriptions", "Streaming", ""),
    
    # ── Cloud & Software ──────────────────────────────────────────────────
    ("APPLE.COM", "description", "Subscriptions", "", ""),
    ("GOOGLE STORAGE", "description", "Subscriptions", "Cloud", ""),
    ("GOOGLE ONE", "description", "Subscriptions", "Cloud", ""),
    ("ICLOUD", "description", "Subscriptions", "Cloud", ""),
    ("MICROSOFT 365", "description", "Subscriptions", "Software", ""),
    ("PROTON", "description", "Subscriptions", "Software", ""),
    
    # ── Other Digital ─────────────────────────────────────────────────────
    ("PATREON", "description", "Subscriptions", "Membership", ""),

    # ══════════════════════════════════════════════════════════════════════
    # SHOPPING
    # ══════════════════════════════════════════════════════════════════════
    
    # ── Clothing ──────────────────────────────────────────────────────────
    ("PRIMARK", "description", "Shopping", "Clothing", ""),
    ("ZARA", "description", "Shopping", "Clothing", ""),
    ("H&M", "description", "Shopping", "Clothing", ""),
    ("PULL&BEAR", "description", "Shopping", "Clothing", ""),
    ("BERSHKA", "description", "Shopping", "Clothing", ""),
    ("SPRINGFIELD", "description", "Shopping", "Clothing", ""),
    ("LANIDOR", "description", "Shopping", "Clothing", ""),
    ("BENETTON", "description", "Shopping", "Clothing", ""),
    ("WOMEN SECRET", "description", "Shopping", "Clothing", ""),
    ("LINGERIE", "description", "Shopping", "Clothing", ""),
    ("TEXTIL", "description", "Shopping", "Clothing", ""),
    
    # ── Electronics ───────────────────────────────────────────────────────
    ("WORTEN", "description", "Shopping", "Electronics", ""),
    ("FNAC", "description", "Shopping", "Electronics", ""),
    ("MEDIA MARKT", "description", "Shopping", "Electronics", ""),
    ("RADIO POPULAR", "description", "Shopping", "Electronics", ""),
    
    # ── Home & DIY ────────────────────────────────────────────────────────
    ("IKEA", "description", "Shopping", "Home", ""),
    ("LEROY MERLIN", "description", "Shopping", "Home", ""),
    ("AKI", "description", "Shopping", "Home", ""),
    ("CASA", "description", "Shopping", "Home", ""),
    
    # ── Sports ────────────────────────────────────────────────────────────
    ("SPORT ZONE", "description", "Shopping", "Sports", ""),
    ("DECATHLON", "description", "Shopping", "Sports", ""),
    
    # ── Online Shopping ───────────────────────────────────────────────────
    ("AMAZON", "description", "Shopping", "Online", ""),
    ("ALIEXPRESS", "description", "Shopping", "Online", ""),
    ("VINTED", "description", "Shopping", "Second-hand", ""),
    ("HIPAY", "description", "Shopping", "Online", ""),
    
    # ── Variety Stores ────────────────────────────────────────────────────
    ("TIGER", "description", "Shopping", "Variety Store", ""),
    
    # ── Books ─────────────────────────────────────────────────────────────
    ("BERTRAND", "description", "Shopping", "Books", ""),
    ("LIVRARIA", "description", "Shopping", "Books", ""),
    ("PORTO EDITORA", "description", "Shopping", "Books", ""),
    ("BOOK.IT", "description", "Shopping", "Books", ""),

    # ══════════════════════════════════════════════════════════════════════
    # ENTERTAINMENT & LEISURE
    # ══════════════════════════════════════════════════════════════════════
    ("CINEMA", "description", "Entertainment", "Cinema", ""),
    ("NOS CINEMAS", "description", "Entertainment", "Cinema", ""),
    ("TEATRO", "description", "Entertainment", "Theatre", ""),
    ("MUSEU", "description", "Entertainment", "Museum", ""),
    ("BILHET", "description", "Entertainment", "Tickets", ""),
    ("SEE TICKETS", "description", "Entertainment", "Tickets", ""),

    # ══════════════════════════════════════════════════════════════════════
    # GOVERNMENT & TAXES
    # ══════════════════════════════════════════════════════════════════════
    ("AUTORID TRIBUTARIA", "description", "Taxes", "", ""),
    ("AUTORIDADE TRIBUTAR", "description", "Taxes", "", ""),
    ("AT - AUTORIDADE", "description", "Taxes", "", ""),
    ("REEMBOLSOS IRS", "description", "Income", "Tax Refund", ""),
    ("SEG SOCIAL", "description", "Taxes", "Social Security", ""),
    ("SEGURANCA SOCIAL", "description", "Taxes", "Social Security", ""),
    ("IMPOSTO", "description_raw", "Taxes", "", ""),
    ("IMI ", "description", "Taxes", "Property Tax", ""),
    ("PAG.IGCP", "description_raw", "Taxes", "Government", ""),

    # ══════════════════════════════════════════════════════════════════════
    # BANK & FINANCIAL
    # ══════════════════════════════════════════════════════════════════════
    ("COMISSAO", "description_raw", "Bank Fees", "", "fee"),
    ("COMISSAO LEVANTAMENTO", "description_raw", "Bank Fees", "ATM Fee", "fee"),
    ("COM.MAN.CONTA", "description_raw", "Bank Fees", "Maintenance", "fee"),
    ("CUSTO MANUTENCAO", "description_raw", "Bank Fees", "Maintenance", "fee"),
    ("CUSTO DE SERVICO", "description_raw", "Bank Fees", "Service Fee", "fee"),
    ("ANUIDADE CARTAO", "description_raw", "Bank Fees", "Card Fee", "fee"),
    ("LEV ATM", "description_raw", "Cash", "ATM Withdrawal", ""),
    ("LEVANTAMENTO", "description_raw", "Cash", "ATM Withdrawal", ""),
    ("PAGAMENTO CREDITO", "description_raw", "Debt", "Credit Card Payment", ""),
    (">PAGAMENTO CARTAO", "description_raw", "Debt", "Credit Card Payment", ""),
    ("VIS PAGAMENTO CARTAO", "description_raw", "Debt", "Credit Card Payment", ""),
    ("PRESTACAO", "description_raw", "Debt", "Loan Payment", ""),

    # ══════════════════════════════════════════════════════════════════════
    # INCOME
    # ══════════════════════════════════════════════════════════════════════
    ("TRANSFERENCIA - VENCIMENTO", "description_raw", "Income", "Salary", ""),
    ("VENCIMENTO", "description", "Income", "Salary", ""),

    # ══════════════════════════════════════════════════════════════════════
    # CHILDCARE & EDUCATION
    # ══════════════════════════════════════════════════════════════════════
    ("CRECHE", "description", "Childcare", "", ""),
    ("COLEGIO", "description", "Education", "School", ""),
    ("UNIVERSIDADE", "description", "Education", "University", ""),
    ("FORMACAO", "description", "Education", "Training", ""),
    ("CURSO", "description", "Education", "Course", ""),

    # ══════════════════════════════════════════════════════════════════════
    # PERSONAL CARE
    # ══════════════════════════════════════════════════════════════════════
    ("CABELEIREIRO", "description", "Personal Care", "Hair", ""),
    ("BARBEIRO", "description", "Personal Care", "Hair", ""),
    ("ESTETICA", "description", "Personal Care", "Beauty", ""),
    ("SPA", "description", "Personal Care", "Wellness", ""),

    # ══════════════════════════════════════════════════════════════════════
    # PETS
    # ══════════════════════════════════════════════════════════════════════
    ("VETERINAR", "description", "Pets", "Vet", ""),
    ("PET ", "description", "Pets", "", ""),
    ("PETSHOP", "description", "Pets", "", ""),

    # ══════════════════════════════════════════════════════════════════════
    # HOUSING
    # ══════════════════════════════════════════════════════════════════════
    ("RENDA", "description", "Housing", "Rent", ""),
    ("ALUGUER", "description", "Housing", "Rent", ""),
    ("CONDOMINIO", "description", "Housing", "Condo Fee", ""),
]


# ---------------------------------------------------------------------------
# Import logic
# ---------------------------------------------------------------------------


def get_starter_rules() -> list[dict]:
    """Return the starter rules as a list of dicts (same format as load_rules)."""
    return [
        {
            "pattern": pattern,
            "match_field": match_field,
            "category": category,
            "subcategory": subcategory,
            "payment_type": payment_type,
        }
        for pattern, match_field, category, subcategory, payment_type in STARTER_RULES
    ]


def import_starter_rules(rules_path: Path, *, dry_run: bool = False) -> tuple[int, int]:
    """
    Import starter rules into the user's rules.csv, skipping any
    whose pattern already exists (case-insensitive).

    Returns (added, skipped) counts.
    """
    # Load existing rules to detect duplicates (strip to match CSV round-trip)
    existing = load_rules(rules_path) if rules_path.exists() else []
    existing_patterns = {r["pattern"].strip().upper() for r in existing}

    to_add = []
    skipped = 0
    for rule in get_starter_rules():
        if rule["pattern"].strip().upper() in existing_patterns:
            skipped += 1
        else:
            to_add.append(rule)

    if dry_run or not to_add:
        return len(to_add), skipped

    # Append new rules to the CSV
    is_new = not rules_path.exists() or rules_path.stat().st_size == 0
    with rules_path.open("a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(RULES_HEADER)
        for rule in to_add:
            writer.writerow([
                rule["pattern"],
                rule["match_field"],
                rule["category"],
                rule["subcategory"],
                rule["payment_type"],
            ])

    return len(to_add), skipped
