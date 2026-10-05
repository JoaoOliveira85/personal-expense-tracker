"""
Email statement fetcher — download bank statement attachments via IMAP.

Connects to an email account, searches for messages from the bank,
and saves PDF/CSV attachments to the raw/ directory for ingestion.

Uses only Python standard library (imaplib, email).
"""
from __future__ import annotations

import email
import hashlib
import imaplib
import json
import os
import re
from datetime import datetime, timedelta
from email.header import decode_header
from pathlib import Path
from typing import Optional

from .constants import DEFAULT_RAW

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_EMAIL_CONFIG = Path("data/email-config.json")

# No bank is built in: the sender patterns (case-insensitive substrings of the
# From address) come from "bank_senders" in the email config.
DEFAULT_BANK_SENDERS: list[str] = []

# Attachment patterns to download
ATTACHMENT_PATTERNS = [
    re.compile(r"\.pdf$", re.IGNORECASE),
    re.compile(r"\.csv$", re.IGNORECASE),
]

# Subject patterns that indicate bank statements
SUBJECT_PATTERNS = [
    re.compile(r"extrato", re.IGNORECASE),
    re.compile(r"movimentos", re.IGNORECASE),
    re.compile(r"statement", re.IGNORECASE),
    re.compile(r"comprovativo", re.IGNORECASE),
]


# ---------------------------------------------------------------------------
# Config management
# ---------------------------------------------------------------------------


def load_email_config(config_path: Path = DEFAULT_EMAIL_CONFIG) -> dict:
    """
    Load email configuration from a JSON file.

    Expected format:
    {
        "imap_host": "imap.gmail.com",
        "imap_port": 993,
        "email": "user@gmail.com",
        "password": "app-specific-password",
        "bank_senders": ["mybank.example"],
        "folder": "INBOX"
    }
    """
    if not config_path.exists():
        raise FileNotFoundError(
            f"Email config not found at {config_path}.\n"
            f"Create it with: python bank_ingest.py fetch --setup"
        )
    with config_path.open("r", encoding="utf-8") as f:
        config = json.load(f)

    required = ["imap_host", "email", "password"]
    for key in required:
        if key not in config:
            raise ValueError(f"Missing required key '{key}' in {config_path}")

    # Defaults
    config.setdefault("imap_port", 993)
    config.setdefault("bank_senders", DEFAULT_BANK_SENDERS)
    config.setdefault("folder", "INBOX")
    config.setdefault("subject_patterns", [])

    return config


def create_email_config(
    config_path: Path,
    imap_host: str,
    email_addr: str,
    password: str,
    imap_port: int = 993,
    bank_senders: list[str] | None = None,
    folder: str = "INBOX",
) -> None:
    """Create a new email configuration file."""
    config = {
        "imap_host": imap_host,
        "imap_port": imap_port,
        "email": email_addr,
        "password": password,
        "bank_senders": bank_senders or DEFAULT_BANK_SENDERS,
        "folder": folder,
    }
    config_path.parent.mkdir(parents=True, exist_ok=True)
    # The file holds the mailbox password: owner-only, also when it exists
    fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


# ---------------------------------------------------------------------------
# IMAP helpers
# ---------------------------------------------------------------------------


def _decode_header_value(value: str) -> str:
    """Decode an email header value that may be encoded."""
    parts = decode_header(value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return " ".join(decoded)


def _matches_bank_sender(from_addr: str, bank_senders: list[str]) -> bool:
    """Check if the sender address matches any known bank sender pattern."""
    from_lower = from_addr.lower()
    return any(pattern.lower() in from_lower for pattern in bank_senders)


def _is_statement_attachment(filename: str) -> bool:
    """Check if a filename looks like a bank statement attachment."""
    return any(pattern.search(filename) for pattern in ATTACHMENT_PATTERNS)


def _matches_subject(subject: str, extra_patterns: list[str] | None = None) -> bool:
    """Check if the email subject matches known bank statement patterns."""
    patterns = list(SUBJECT_PATTERNS)
    if extra_patterns:
        patterns.extend(re.compile(p, re.IGNORECASE) for p in extra_patterns)
    return any(p.search(subject) for p in patterns)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _unused_path(output_dir: Path, filename: str, taken: set[str]) -> Path:
    """output_dir/filename, or "name (2).ext", "name (3).ext"... if taken."""
    candidate = Path(filename)
    n = 1
    while candidate.name in taken:
        n += 1
        candidate = Path(f"{Path(filename).stem} ({n}){Path(filename).suffix}")
    return output_dir / candidate.name


# ---------------------------------------------------------------------------
# Core fetch logic
# ---------------------------------------------------------------------------


def fetch_statements(
    config: dict,
    output_dir: Path = DEFAULT_RAW,
    days_back: int = 60,
    dry_run: bool = False,
) -> list[Path]:
    """
    Connect to IMAP, search for bank statement emails, and download
    PDF/CSV attachments to output_dir. An attachment whose bytes are already
    in output_dir (under any name) is skipped; one whose name is taken by
    other content is saved as "name (2).ext".

    Args:
        config: Email configuration dict (from load_email_config).
        output_dir: Directory to save downloaded attachments.
        days_back: How far back to search for emails (default: 60 days).
        dry_run: If True, list what would be downloaded without saving.

    Returns:
        List of paths to downloaded files.
    """
    imap_host = config["imap_host"]
    imap_port = config["imap_port"]
    email_addr = config["email"]
    password = config["password"]
    bank_senders = config.get("bank_senders", DEFAULT_BANK_SENDERS)
    if not bank_senders:
        raise ValueError(
            "No bank sender patterns configured: set 'bank_senders' in the "
            'email config (e.g. ["mybank.example"]).'
        )
    folder = config.get("folder", "INBOX")
    extra_subject_patterns = config.get("subject_patterns", [])

    # Connect and login
    conn = imaplib.IMAP4_SSL(imap_host, imap_port)
    try:
        conn.login(email_addr, password)
        conn.select(folder, readonly=True)

        # Search for recent emails
        since_date = (datetime.now() - timedelta(days=days_back)).strftime("%d-%b-%Y")
        status, msg_ids = conn.search(None, f'(SINCE "{since_date}")')

        if status != "OK" or not msg_ids[0]:
            return []

        downloaded: list[Path] = []
        existing_files = {p.name for p in output_dir.iterdir()} if output_dir.is_dir() else set()
        # "Already downloaded" is decided by content: a bank may give every
        # statement the same attachment name, and auto renames what it imports.
        existing_digests = {
            _digest(p.read_bytes()) for p in output_dir.iterdir() if p.is_file()
        } if output_dir.is_dir() else set()

        for msg_id in msg_ids[0].split():
            status, msg_data = conn.fetch(msg_id, "(RFC822)")
            if status != "OK":
                continue

            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)

            # Check sender
            from_addr = _decode_header_value(msg.get("From", ""))
            if not _matches_bank_sender(from_addr, bank_senders):
                continue

            # Check subject
            subject = _decode_header_value(msg.get("Subject", ""))
            if not _matches_subject(subject, extra_subject_patterns):
                continue

            # Extract attachments
            for part in msg.walk():
                if part.get_content_maintype() == "multipart":
                    continue
                filename = part.get_filename()
                if not filename:
                    continue
                # The name is chosen by whoever sent the email: keep only the
                # last component so it can never point outside output_dir.
                filename = Path(_decode_header_value(filename)).name
                if not _is_statement_attachment(filename):
                    continue

                payload = part.get_payload(decode=True)
                if not payload:
                    continue

                # Skip already-downloaded files
                digest = _digest(payload)
                if digest in existing_digests:
                    continue
                existing_digests.add(digest)

                # Never overwrite: other content under a taken name gets its own
                file_path = _unused_path(output_dir, filename, existing_files)
                existing_files.add(file_path.name)

                if dry_run:
                    print(f"  [dry-run] Would download: {file_path.name}")
                    print(f"    From: {from_addr}")
                    print(f"    Subject: {subject}")
                    downloaded.append(file_path)
                    continue

                # Save attachment
                output_dir.mkdir(parents=True, exist_ok=True)
                file_path.write_bytes(payload)
                downloaded.append(file_path)

        return downloaded

    finally:
        try:
            conn.logout()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# High-level API (used by CLI)
# ---------------------------------------------------------------------------


def fetch_and_report(
    config_path: Path = DEFAULT_EMAIL_CONFIG,
    output_dir: Path = DEFAULT_RAW,
    days_back: int = 60,
    dry_run: bool = False,
) -> list[Path]:
    """
    Load config, fetch statements, and return the list of downloaded files.
    This is the main entry point for the CLI 'fetch' subcommand.
    """
    config = load_email_config(config_path)
    return fetch_statements(config, output_dir, days_back, dry_run)
