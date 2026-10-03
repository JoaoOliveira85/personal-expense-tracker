"""Tests for expense_tracker.email_fetch."""
from __future__ import annotations

import email
import json
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from expense_tracker.email_fetch import (
    load_email_config,
    create_email_config,
    _decode_header_value,
    _matches_bank_sender,
    _is_statement_attachment,
    _matches_subject,
    fetch_statements,
    DEFAULT_BANK_SENDERS,
)


# ---------------------------------------------------------------------------
# Config management
# ---------------------------------------------------------------------------


class TestEmailConfig:
    def test_create_and_load(self, tmp_path):
        config_path = tmp_path / "email-config.json"
        create_email_config(
            config_path,
            imap_host="imap.gmail.com",
            email_addr="test@gmail.com",
            password="app-password-123",
        )
        assert config_path.exists()
        config = load_email_config(config_path)
        assert config["imap_host"] == "imap.gmail.com"
        assert config["email"] == "test@gmail.com"
        assert config["password"] == "app-password-123"
        assert config["imap_port"] == 993
        assert config["folder"] == "INBOX"

    @pytest.mark.parametrize("pre_existing", [False, True])
    def test_config_is_readable_only_by_owner(self, tmp_path, pre_existing):
        """The file holds the mailbox password."""
        config_path = tmp_path / "email-config.json"
        if pre_existing:
            config_path.write_text("{}", encoding="utf-8")
            config_path.chmod(0o644)
        create_email_config(
            config_path,
            imap_host="imap.gmail.com",
            email_addr="test@gmail.com",
            password="app-password-123",
        )
        assert config_path.stat().st_mode & 0o777 == 0o600
        assert load_email_config(config_path)["password"] == "app-password-123"

    def test_load_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="Email config not found"):
            load_email_config(tmp_path / "nonexistent.json")

    def test_load_missing_required_key(self, tmp_path):
        config_path = tmp_path / "bad-config.json"
        config_path.write_text('{"imap_host": "test"}', encoding="utf-8")
        with pytest.raises(ValueError, match="Missing required key"):
            load_email_config(config_path)

    def test_custom_bank_senders(self, tmp_path):
        config_path = tmp_path / "email-config.json"
        create_email_config(
            config_path,
            imap_host="imap.gmail.com",
            email_addr="test@gmail.com",
            password="pass",
            bank_senders=["mybank.pt"],
        )
        config = load_email_config(config_path)
        assert config["bank_senders"] == ["mybank.pt"]

    def test_defaults_applied(self, tmp_path):
        config_path = tmp_path / "minimal.json"
        config_path.write_text(
            json.dumps({
                "imap_host": "imap.example.com",
                "email": "a@b.com",
                "password": "p",
            }),
            encoding="utf-8",
        )
        config = load_email_config(config_path)
        assert config["imap_port"] == 993
        assert config["folder"] == "INBOX"
        assert config["bank_senders"] == DEFAULT_BANK_SENDERS

    def test_creates_parent_directory(self, tmp_path):
        config_path = tmp_path / "deep" / "nested" / "config.json"
        create_email_config(
            config_path,
            imap_host="imap.gmail.com",
            email_addr="test@gmail.com",
            password="pass",
        )
        assert config_path.exists()


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


class TestDecodeHeaderValue:
    def test_plain_string(self):
        assert _decode_header_value("Hello World") == "Hello World"

    def test_encoded_header(self):
        from email.header import make_header, Header
        h = Header("Extrato Bancário", "utf-8")
        encoded = h.encode()
        assert "Bancário" in _decode_header_value(encoded)


class TestMatchesBankSender:
    def test_matches_utf16(self):
        assert _matches_bank_sender(
            "Bank A <noreply@bank-a.example>",
            ["bank-a.example"],
        )

    def test_no_match(self):
        assert not _matches_bank_sender(
            "spam@spam.com",
            ["bank-a.example"],
        )

    def test_case_insensitive(self):
        assert _matches_bank_sender(
            "NOREPLY@BANK-A.EXAMPLE",
            ["bank-a.example"],
        )


class TestIsStatementAttachment:
    def test_pdf(self):
        assert _is_statement_attachment("extrato_2026-01.pdf")

    def test_csv(self):
        assert _is_statement_attachment("EXPORT_0_1012026.csv")

    def test_pdf_uppercase(self):
        assert _is_statement_attachment("Statement.PDF")

    def test_non_matching(self):
        assert not _is_statement_attachment("logo.png")
        assert not _is_statement_attachment("readme.txt")


class TestMatchesSubject:
    def test_extrato(self):
        assert _matches_subject("Extrato de Conta - Janeiro 2026")

    def test_movimentos(self):
        assert _matches_subject("Movimentos da conta 123456")

    def test_statement(self):
        assert _matches_subject("Account Statement - January 2026")

    def test_no_match(self):
        assert not _matches_subject("Your order has shipped")

    def test_extra_patterns(self):
        assert _matches_subject(
            "Resumo mensal",
            extra_patterns=["resumo"],
        )


# ---------------------------------------------------------------------------
# Fetch logic (mocked IMAP)
# ---------------------------------------------------------------------------


def _make_email_message(
    from_addr: str,
    subject: str,
    attachments: list[tuple[str, bytes]] | None = None,
) -> bytes:
    """Create a synthetic email message as raw bytes."""
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["Subject"] = subject
    msg["To"] = "test@test.com"
    msg.set_content("This is a bank statement email.")

    if attachments:
        for filename, content in attachments:
            maintype = "application"
            subtype = "pdf" if filename.endswith(".pdf") else "octet-stream"
            msg.add_attachment(
                content,
                maintype=maintype,
                subtype=subtype,
                filename=filename,
            )

    return msg.as_bytes()


class TestFetchStatements:
    def _mock_imap(self, messages: list[bytes]):
        """Create a mock IMAP4_SSL connection with the given messages."""
        mock_conn = MagicMock()
        mock_conn.login.return_value = ("OK", [])
        mock_conn.select.return_value = ("OK", [b"1"])

        # Build message ID list
        msg_ids = b" ".join(str(i + 1).encode() for i in range(len(messages)))
        mock_conn.search.return_value = ("OK", [msg_ids])

        # Build fetch responses
        def fetch_side_effect(msg_id, fmt):
            idx = int(msg_id) - 1
            if idx < len(messages):
                return ("OK", [(b"1", messages[idx])])
            return ("OK", [])

        mock_conn.fetch.side_effect = fetch_side_effect
        mock_conn.logout.return_value = ("OK", [])
        return mock_conn

    def test_downloads_matching_attachment(self, tmp_path):
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()

        pdf_content = b"%PDF-1.4 fake content"
        msg = _make_email_message(
            "Bank A <noreply@bank-a.example>",
            "Extrato de Conta - Janeiro 2026",
            [("extrato_2026-01.pdf", pdf_content)],
        )

        mock_conn = self._mock_imap([msg])
        config = {
            "imap_host": "imap.test.com",
            "imap_port": 993,
            "email": "test@test.com",
            "password": "pass",
            "bank_senders": ["bank-a.example"],
            "folder": "INBOX",
        }

        with patch("expense_tracker.email_fetch.imaplib.IMAP4_SSL", return_value=mock_conn):
            downloaded = fetch_statements(config, raw_dir)

        assert len(downloaded) == 1
        assert downloaded[0].name == "extrato_2026-01.pdf"
        assert downloaded[0].read_bytes() == pdf_content

    def test_skips_non_bank_sender(self, tmp_path):
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()

        msg = _make_email_message(
            "spam@spam.com",
            "Extrato de Conta",
            [("statement.pdf", b"fake")],
        )

        mock_conn = self._mock_imap([msg])
        config = {
            "imap_host": "imap.test.com",
            "imap_port": 993,
            "email": "test@test.com",
            "password": "pass",
            "bank_senders": ["bank-a.example"],
            "folder": "INBOX",
        }

        with patch("expense_tracker.email_fetch.imaplib.IMAP4_SSL", return_value=mock_conn):
            downloaded = fetch_statements(config, raw_dir)

        assert len(downloaded) == 0

    def test_skips_non_matching_subject(self, tmp_path):
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()

        msg = _make_email_message(
            "Bank A <noreply@bank-a.example>",
            "Welcome to Bank A Online Banking",
            [("welcome.pdf", b"fake")],
        )

        mock_conn = self._mock_imap([msg])
        config = {
            "imap_host": "imap.test.com",
            "imap_port": 993,
            "email": "test@test.com",
            "password": "pass",
            "bank_senders": ["bank-a.example"],
            "folder": "INBOX",
        }

        with patch("expense_tracker.email_fetch.imaplib.IMAP4_SSL", return_value=mock_conn):
            downloaded = fetch_statements(config, raw_dir)

        assert len(downloaded) == 0

    def test_skips_already_downloaded(self, tmp_path):
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()
        # Pre-existing file
        (raw_dir / "extrato.pdf").write_bytes(b"old")

        msg = _make_email_message(
            "Bank A <noreply@bank-a.example>",
            "Extrato mensal",
            [("extrato.pdf", b"new")],
        )

        mock_conn = self._mock_imap([msg])
        config = {
            "imap_host": "imap.test.com",
            "imap_port": 993,
            "email": "test@test.com",
            "password": "pass",
            "bank_senders": ["bank-a.example"],
            "folder": "INBOX",
        }

        with patch("expense_tracker.email_fetch.imaplib.IMAP4_SSL", return_value=mock_conn):
            downloaded = fetch_statements(config, raw_dir)

        assert len(downloaded) == 0
        # Original file should be unchanged
        assert (raw_dir / "extrato.pdf").read_bytes() == b"old"

    def test_dry_run_does_not_save(self, tmp_path):
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()

        msg = _make_email_message(
            "Bank A <noreply@bank-a.example>",
            "Extrato de Conta",
            [("statement.pdf", b"fake")],
        )

        mock_conn = self._mock_imap([msg])
        config = {
            "imap_host": "imap.test.com",
            "imap_port": 993,
            "email": "test@test.com",
            "password": "pass",
            "bank_senders": ["bank-a.example"],
            "folder": "INBOX",
        }

        with patch("expense_tracker.email_fetch.imaplib.IMAP4_SSL", return_value=mock_conn):
            downloaded = fetch_statements(config, raw_dir, dry_run=True)

        assert len(downloaded) == 1
        # File should NOT exist on disk
        assert not (raw_dir / "statement.pdf").exists()

    def test_no_messages(self, tmp_path):
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()

        mock_conn = MagicMock()
        mock_conn.login.return_value = ("OK", [])
        mock_conn.select.return_value = ("OK", [b"0"])
        mock_conn.search.return_value = ("OK", [b""])
        mock_conn.logout.return_value = ("OK", [])

        config = {
            "imap_host": "imap.test.com",
            "imap_port": 993,
            "email": "test@test.com",
            "password": "pass",
            "bank_senders": ["bank-a.example"],
            "folder": "INBOX",
        }

        with patch("expense_tracker.email_fetch.imaplib.IMAP4_SSL", return_value=mock_conn):
            downloaded = fetch_statements(config, raw_dir)

        assert len(downloaded) == 0

    @pytest.mark.parametrize(
        "attachment_name",
        ["../data/rules.csv", "../../escaped.pdf", "sub/../../escaped.csv"],
    )
    def test_attachment_name_cannot_escape_output_dir(self, tmp_path, attachment_name):
        """The attachment name comes from the email: it must not pick the path."""
        raw_dir = tmp_path / "work" / "raw"
        raw_dir.mkdir(parents=True)

        msg = _make_email_message(
            "Bank A <noreply@bank-a.example>",
            "Extrato de Conta",
            [(attachment_name, b"payload")],
        )
        mock_conn = self._mock_imap([msg])
        config = {
            "imap_host": "imap.test.com",
            "imap_port": 993,
            "email": "test@test.com",
            "password": "pass",
            "bank_senders": ["bank-a.example"],
            "folder": "INBOX",
        }

        with patch("expense_tracker.email_fetch.imaplib.IMAP4_SSL", return_value=mock_conn):
            downloaded = fetch_statements(config, raw_dir)

        assert len(downloaded) == 1
        assert downloaded[0].parent == raw_dir
        assert downloaded[0].name == Path(attachment_name).name
        written = [p for p in tmp_path.rglob("*") if p.is_file()]
        assert written == [downloaded[0]]

    def test_absolute_attachment_name_stays_in_output_dir(self, tmp_path):
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()
        outside = tmp_path / "outside" / "statement.csv"

        msg = _make_email_message(
            "Bank A <noreply@bank-a.example>",
            "Extrato de Conta",
            [(str(outside), b"payload")],
        )
        mock_conn = self._mock_imap([msg])
        config = {
            "imap_host": "imap.test.com",
            "imap_port": 993,
            "email": "test@test.com",
            "password": "pass",
            "bank_senders": ["bank-a.example"],
            "folder": "INBOX",
        }

        with patch("expense_tracker.email_fetch.imaplib.IMAP4_SSL", return_value=mock_conn):
            downloaded = fetch_statements(config, raw_dir)

        assert downloaded == [raw_dir / "statement.csv"]
        assert not outside.exists()
