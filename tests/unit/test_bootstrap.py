"""First-run .env setup.

The one rule that matters: a value someone already set is never overwritten. Getting
that wrong either destroys a working credential or generates a second one that nothing
reads, and both fail quietly.
"""

from __future__ import annotations

from pathlib import Path

from xauusd.config.bootstrap import check_database, ensure_env_file, parse_env


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
        write(tmp_path, ".env.example", "POSTGRES_PASSWORD=\n")
        ensure_env_file(tmp_path)
        assert len(parse_env((tmp_path / ".env").read_text())["POSTGRES_PASSWORD"]) >= 16

    def test_no_dashboard_token_is_generated_for_a_loopback_install(self, tmp_path: Path) -> None:
        """The dashboard binds to loopback, where a token buys nothing. Generating one
        meant the first thing an operator saw was a password prompt for a secret nobody
        had told them existed, guarding a page only they could reach."""
        write(tmp_path, ".env.example", "XAUUSD_DASHBOARD__AUTH_TOKEN=\n")
        ensure_env_file(tmp_path)
        assert not parse_env((tmp_path / ".env").read_text()).get("XAUUSD_DASHBOARD__AUTH_TOKEN")

    def test_a_token_the_operator_set_is_still_respected(self, tmp_path: Path) -> None:
        """Someone who wants remote access sets one; nothing here may discard it."""
        write(tmp_path, ".env.example", "")
        write(tmp_path, ".env", "XAUUSD_DASHBOARD__AUTH_TOKEN=" + "k" * 40 + "\n")
        ensure_env_file(tmp_path)
        assert (
            parse_env((tmp_path / ".env").read_text())["XAUUSD_DASHBOARD__AUTH_TOKEN"] == "k" * 40
        )

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
        write(tmp_path, ".env.example", "POSTGRES_PASSWORD=\n")
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


class TestTheDatabaseUrlFollowsWhatIsActuallyRunning:
    """Whether PostgreSQL exists decides what belongs in the file.

    Writing a Postgres URL on a machine with no Postgres is worse than writing none:
    the config files already fall back to a local SQLite file that needs nothing
    installed, and an unreachable URL overrides that and fails at first connection —
    which is what made setup unfinishable for anyone who skipped Docker.
    """

    def test_no_url_is_written_when_postgres_is_absent(self, tmp_path: Path) -> None:
        write(tmp_path, ".env.example", "POSTGRES_PASSWORD=\n")
        ensure_env_file(tmp_path)
        assert "XAUUSD_DATABASE__URL" not in parse_env((tmp_path / ".env").read_text())

    def test_the_url_is_written_when_postgres_is_running(self, tmp_path: Path) -> None:
        write(tmp_path, ".env.example", "POSTGRES_PASSWORD=\n")
        ensure_env_file(tmp_path, postgres=True)
        parsed = parse_env((tmp_path / ".env").read_text())
        assert parsed["XAUUSD_DATABASE__URL"].startswith("postgresql+psycopg://")
        assert parsed["POSTGRES_PASSWORD"] in parsed["XAUUSD_DATABASE__URL"]

    def test_a_placeholder_password_is_replaced(self, tmp_path: Path) -> None:
        write(tmp_path, ".env.example", "")
        write(
            tmp_path,
            ".env",
            "POSTGRES_PASSWORD=\n"
            "XAUUSD_DATABASE__URL=postgresql+psycopg://xauusd:CHANGEME@localhost:5432/xauusd\n",
        )
        ensure_env_file(tmp_path, postgres=True)
        parsed = parse_env((tmp_path / ".env").read_text())
        assert "CHANGEME" not in parsed["XAUUSD_DATABASE__URL"]
        assert parsed["POSTGRES_PASSWORD"] in parsed["XAUUSD_DATABASE__URL"]

    def test_an_edited_url_is_left_alone(self, tmp_path: Path) -> None:
        """Their own host, their own password, a managed database — none of it ours."""
        url = "postgresql+psycopg://ops:their-own-secret@db.internal:5432/xauusd"
        write(tmp_path, ".env.example", "")
        write(tmp_path, ".env", f"POSTGRES_PASSWORD=generated\nXAUUSD_DATABASE__URL={url}\n")
        ensure_env_file(tmp_path, postgres=True)
        assert parse_env((tmp_path / ".env").read_text())["XAUUSD_DATABASE__URL"] == url

    def test_a_sqlite_url_is_left_alone(self, tmp_path: Path) -> None:
        write(tmp_path, ".env.example", "")
        write(tmp_path, ".env", "POSTGRES_PASSWORD=\nXAUUSD_DATABASE__URL=sqlite:///data/x.db\n")
        ensure_env_file(tmp_path, postgres=True)
        assert parse_env((tmp_path / ".env").read_text())["XAUUSD_DATABASE__URL"] == (
            "sqlite:///data/x.db"
        )


class TestTheDatabaseIsCheckedBeforeItIsUsed:
    """An unreachable database surfaced as a SQLAlchemy traceback at the schema step.

    The specific trap: earlier setup versions wrote a PostgreSQL URL into .env
    unconditionally. It sat harmless while nothing read .env — and came alive the moment
    that was fixed, pointing at a database the machine never had.
    """

    def test_a_fresh_install_with_no_data_directory_works(
        self, tmp_path: Path, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        """`data/` holds only generated files, so it is not in the repository and a
        fresh install does not have one. SQLite cannot create a file in a directory
        that does not exist, and the resulting OperationalError reads like the
        database is unreachable rather than like a missing folder.

        The check must build its engine the same way the application does — via
        make_engine, which creates the directory — rather than reimplementing it.
        """
        monkeypatch.chdir(tmp_path)
        (tmp_path / "config").mkdir()
        (tmp_path / ".env").write_text("XAUUSD_DATABASE__URL=sqlite:///data/fresh.db\n")
        assert not (tmp_path / "data").exists()

        ok, detail = check_database(tmp_path)
        assert ok, detail
        assert (tmp_path / "data" / "fresh.db").exists()

    def test_a_reachable_database_passes(self, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.chdir(tmp_path)
        (tmp_path / "config").mkdir()
        (tmp_path / ".env").write_text(f"XAUUSD_DATABASE__URL=sqlite:///{tmp_path}/ok.db\n")
        ok, detail = check_database(tmp_path)
        assert ok, detail

    def test_a_stale_generated_url_is_commented_out(self, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.chdir(tmp_path)
        (tmp_path / "config").mkdir()
        url = "postgresql+psycopg://xauusd:generated-pw@localhost:5432/xauusd"
        (tmp_path / ".env").write_text(f"XAUUSD_DATABASE__URL={url}\n")

        ok, detail = check_database(tmp_path)
        assert ok, "a URL we generated ourselves should be repaired, not fatal"
        assert "commented out" in detail

        text = (tmp_path / ".env").read_text()
        assert f"# XAUUSD_DATABASE__URL={url}" in text, "the old value must stay visible"
        assert parse_env(text).get("XAUUSD_DATABASE__URL") is None

    def test_a_hand_written_url_is_never_touched(self, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """A different host means someone made a decision, and it outranks our guess."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "config").mkdir()
        url = "postgresql+psycopg://ops:secret@db.internal:5432/trading"
        (tmp_path / ".env").write_text(f"XAUUSD_DATABASE__URL={url}\n")

        ok, detail = check_database(tmp_path)
        assert not ok, "we must not silently discard an operator's own database"
        assert "left alone" in detail
        assert parse_env((tmp_path / ".env").read_text())["XAUUSD_DATABASE__URL"] == url
