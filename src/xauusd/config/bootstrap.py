"""First-run environment file setup.

This exists in Python rather than in Setup.bat because the batch equivalent — parsing
`.env` with `findstr` to work out whether a key already has a value — is both unreadable
and untestable, and getting it wrong means either overwriting a working credential or
generating a second one that never gets used.

The rule throughout: **never overwrite a value that is already set.** A key with a value
is a decision someone made; a key that is absent or blank is one nobody has made yet.
"""

from __future__ import annotations

import secrets
import shutil
from pathlib import Path

# Keys that must have a value, and how many bytes of entropy to generate for each.
GENERATED_SECRETS = {
    "XAUUSD_DASHBOARD__AUTH_TOKEN": 32,
    "POSTGRES_PASSWORD": 24,
}


def parse_env(text: str) -> dict[str, str]:
    """Read KEY=VALUE lines, ignoring comments and blanks.

    Deliberately simple: this reads files this project writes, not arbitrary shell.
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def ensure_env_file(root: Path | str = ".") -> dict[str, str]:
    """Create `.env` if absent and fill in any missing generated secret.

    Returns a report of what happened, so the caller can tell the operator rather than
    silently doing something to a credentials file.
    """
    root = Path(root)
    env_path = root / ".env"
    example = root / ".env.example"
    report: dict[str, str] = {}

    if not env_path.exists():
        if example.exists():
            shutil.copyfile(example, env_path)
            report[".env"] = "created from .env.example"
        else:
            env_path.write_text("")
            report[".env"] = "created empty (.env.example is missing)"
    else:
        report[".env"] = "already present"

    text = env_path.read_text()
    existing = parse_env(text)

    additions: list[str] = []
    for key, nbytes in GENERATED_SECRETS.items():
        if existing.get(key):
            report[key] = "already set — left alone"
            continue
        value = secrets.token_urlsafe(nbytes)
        if key in existing:
            # Present but blank: replace that line rather than appending a duplicate,
            # because a later blank would otherwise win on re-read.
            lines = text.splitlines()
            text = "\n".join(
                f"{key}={value}" if ln.strip().startswith(f"{key}=") else ln for ln in lines
            )
            report[key] = "generated (filled in a blank entry)"
        else:
            additions.append(f"{key}={value}")
            report[key] = "generated (added)"

    if additions:
        if text and not text.endswith("\n"):
            text += "\n"
        text += "\n".join(additions) + "\n"

    # The generated Postgres password has to appear in TWO places: the variable compose
    # reads, and the connection string the engine reads. Leaving the operator to copy it
    # across by hand is a step that gets missed, and the failure is a connection refused
    # at startup with nothing to say why.
    text, wired = _wire_database_password(text)
    if wired:
        report["XAUUSD_DATABASE__URL"] = "database password filled in"

    if text and not text.endswith("\n"):
        text += "\n"
    env_path.write_text(text)
    return report


DB_PLACEHOLDERS = ("CHANGEME", "xauusd:xauusd@")


def _wire_database_password(text: str) -> tuple[str, bool]:
    """Put the generated password into XAUUSD_DATABASE__URL, if it is still a placeholder.

    Only placeholders are touched. A URL the operator has already edited — a different
    host, a managed database, their own password — is left exactly as it is.
    """
    parsed = parse_env(text)
    url = parsed.get("XAUUSD_DATABASE__URL", "")
    password = parsed.get("POSTGRES_PASSWORD", "")
    if not url or not password or not url.startswith("postgres"):
        return text, False
    if not any(ph in url for ph in DB_PLACEHOLDERS):
        return text, False

    new_url = url.replace("CHANGEME", password).replace("xauusd:xauusd@", f"xauusd:{password}@")
    lines = text.splitlines()
    out = [
        f"XAUUSD_DATABASE__URL={new_url}" if ln.strip().startswith("XAUUSD_DATABASE__URL=") else ln
        for ln in lines
    ]
    return "\n".join(out) + ("\n" if text.endswith("\n") else ""), True


def main() -> int:
    report = ensure_env_file(Path.cwd())
    for key, what in report.items():
        # Never print the values themselves — this runs in a console window that may be
        # screen-shared, and the whole point of the file is that they stay in it.
        print(f"   {key}: {what}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
