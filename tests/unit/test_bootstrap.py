"""First-run .env setup.

The one rule that matters: a value someone already set is never overwritten. Getting
that wrong either destroys a working credential or generates a second one that nothing
reads, and both fail quietly.
"""

from __future__ import annotations

from pathlib import Path

from xauusd.config.bootstrap import ensure_env_file, parse_env


def write(root: Path, name: str, text: str) -> None:
    (root / name).write_text(text)


class TestParsing:
    def test_comments_and_blanks_are_ignored(self) -> None:
        parsed = parse_env("# a comment\n\nA=1\n  B = two  \nnot-a-pair\n")
        assert parsed == {"A": "1", "B": "two"}

    def test_a_value_containing_equals_is_kept_whole(self) -> None:
        """Connection strings and base64 secrets both contain '='."""
        parsed = parse_env("XAUUSD_DATABASE__URL=postgresql://u:p==@h:5432/db\n")
        assert parsed["XAUUSD_DATABASE__URL"] == "postgresql://u:p==@h:5432/db"


class TestGeneratesOnlyWhatIsMissing:
    def test_it_creates_env_from_the_example(self, tmp_path: Path) -> None:
        write(tmp_path, ".env.example", "XAUUSD_ENV=dev\n")
        ensure_env_file(tmp_path)
        assert (tmp_path / ".env").exists()
        assert parse_env((tmp_path / ".env").read_text())["XAUUSD_ENV"] == "dev"

    def test_it_fills_in_blank_secrets(self, tmp_path: Path) -> None:
        write(tmp_path, ".env.example", "POSTGRES_PASSWORD=\nXAUUSD_DASHBOARD__AUTH_TOKEN=\n")
        ensure_env_file(tmp_path)
        parsed = parse_env((tmp_path / ".env").read_text())
        assert len(parsed["POSTGRES_PASSWORD"]) >= 16
        assert len(parsed["XAUUSD_DASHBOARD__AUTH_TOKEN"]) >= 16

    def test_it_never_overwrites_an_existing_secret(self, tmp_path: Path) -> None:
        write(tmp_path, ".env.example", "POSTGRES_PASSWORD=\n")
        write(
            tmp_path,
            ".env",
            "POSTGRES_PASSWORD=the-one-the-database-actually-uses\n"
            "XAUUSD_DASHBOARD__AUTH_TOKEN=a-token-already-in-a-browser\n",
        )
        report = ensure_env_file(tmp_path)
        parsed = parse_env((tmp_path / ".env").read_text())
        assert parsed["POSTGRES_PASSWORD"] == "the-one-the-database-actually-uses"
        assert parsed["XAUUSD_DASHBOARD__AUTH_TOKEN"] == "a-token-already-in-a-browser"
        assert "left alone" in report["POSTGRES_PASSWORD"]

    def test_it_is_idempotent(self, tmp_path: Path) -> None:
        """Setup is safe to double-click twice, which people do."""
        write(tmp_path, ".env.example", "POSTGRES_PASSWORD=\nXAUUSD_DASHBOARD__AUTH_TOKEN=\n")
        ensure_env_file(tmp_path)
        first = (tmp_path / ".env").read_text()
        ensure_env_file(tmp_path)
        assert (tmp_path / ".env").read_text() == first

    def test_a_blank_entry_is_replaced_not_duplicated(self, tmp_path: Path) -> None:
        """An appended second line would be shadowed by the blank one on re-read."""
        write(tmp_path, ".env.example", "")
        write(tmp_path, ".env", "POSTGRES_PASSWORD=\nXAUUSD_ENV=dev\n")
        ensure_env_file(tmp_path)
        text = (tmp_path / ".env").read_text()
        assert text.count("POSTGRES_PASSWORD=") == 1
        assert parse_env(text)["POSTGRES_PASSWORD"] != ""

    def test_other_settings_survive(self, tmp_path: Path) -> None:
        write(tmp_path, ".env.example", "")
        write(tmp_path, ".env", "XAUUSD_BROKER__LOGIN=12345678\nXAUUSD_ENV=demo\n")
        ensure_env_file(tmp_path)
        parsed = parse_env((tmp_path / ".env").read_text())
        assert parsed["XAUUSD_BROKER__LOGIN"] == "12345678"
        assert parsed["XAUUSD_ENV"] == "demo"

    def test_it_copes_with_a_missing_example(self, tmp_path: Path) -> None:
        ensure_env_file(tmp_path)
        parsed = parse_env((tmp_path / ".env").read_text())
        assert parsed["POSTGRES_PASSWORD"]

    def test_the_file_ends_with_a_newline(self, tmp_path: Path) -> None:
        write(tmp_path, ".env.example", "POSTGRES_PASSWORD=\n")
        ensure_env_file(tmp_path)
        assert (tmp_path / ".env").read_text().endswith("\n")
