"""Alert delivery. Pluggable channels; failures never propagate into the trading loop."""

from __future__ import annotations

import smtplib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from enum import IntEnum
from typing import Any, Protocol

from xauusd.config.settings import AlertConfig
from xauusd.monitoring.logging import get_logger

log = get_logger(__name__)


class AlertLevel(IntEnum):
    INFO = 10
    WARNING = 20
    ERROR = 30
    CRITICAL = 40

    @classmethod
    def parse(cls, name: str) -> AlertLevel:
        return cls[name.upper()] if name.upper() in cls.__members__ else cls.WARNING


@dataclass(frozen=True, slots=True)
class Alert:
    level: AlertLevel
    category: str
    title: str
    body: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    ts: datetime = field(default_factory=lambda: datetime.now(UTC))

    def format_text(self) -> str:
        icon = {
            AlertLevel.INFO: "•",
            AlertLevel.WARNING: "!",
            AlertLevel.ERROR: "!!",
            AlertLevel.CRITICAL: "!!!",
        }[self.level]
        lines = [f"{icon} [{self.level.name}] {self.category}: {self.title}"]
        if self.body:
            lines.append(self.body)
        if self.context:
            lines.extend(f"  {k}: {v}" for k, v in self.context.items())
        lines.append(f"  at {self.ts.isoformat()}")
        return "\n".join(lines)


class Channel(Protocol):
    name: str

    def send(self, alert: Alert) -> bool: ...


class LogChannel:
    name = "log"

    def send(self, alert: Alert) -> bool:
        log.warning(
            "alert",
            level=alert.level.name,
            category=alert.category,
            title=alert.title,
            body=alert.body,
            **alert.context,
        )
        return True


class TelegramChannel:
    name = "telegram"

    def __init__(self, token: str, chat_id: str, timeout: float = 10.0) -> None:
        self._token = token
        self._chat_id = chat_id
        self._timeout = timeout

    def send(self, alert: Alert) -> bool:
        import httpx

        try:
            r = httpx.post(
                f"https://api.telegram.org/bot{self._token}/sendMessage",
                json={"chat_id": self._chat_id, "text": alert.format_text()},
                timeout=self._timeout,
            )
            return r.status_code == 200
        except Exception as exc:
            log.error("telegram_send_failed", error=str(exc))
            return False


class EmailChannel:
    name = "email"

    def __init__(self, cfg: AlertConfig) -> None:
        self._cfg = cfg

    def send(self, alert: Alert) -> bool:
        c = self._cfg
        if not (c.smtp_host and c.email_to):
            return False
        try:
            msg = EmailMessage()
            msg["Subject"] = f"[XAUUSD {alert.level.name}] {alert.title}"
            msg["From"] = c.smtp_user or "xauusd-bot@localhost"
            msg["To"] = c.email_to
            msg.set_content(alert.format_text())
            with smtplib.SMTP(c.smtp_host, c.smtp_port, timeout=15) as s:
                s.starttls()
                if c.smtp_user and c.smtp_password:
                    s.login(c.smtp_user, c.smtp_password)
                s.send_message(msg)
            return True
        except Exception as exc:
            log.error("email_send_failed", error=str(exc))
            return False


class Notifier:
    """Fan-out to every configured channel, with de-duplication of alert storms."""

    def __init__(self, config: AlertConfig | None = None) -> None:
        self._cfg = config or AlertConfig()
        self._min = AlertLevel.parse(self._cfg.min_level)
        self._channels: list[Channel] = [LogChannel()]
        self._recent: dict[str, datetime] = {}
        self._dedupe_seconds = 300.0
        self.sent: list[Alert] = []  # retained for tests and the dashboard

        if (
            self._cfg.telegram_enabled
            and self._cfg.telegram_bot_token
            and self._cfg.telegram_chat_id
        ):
            self._channels.append(
                TelegramChannel(self._cfg.telegram_bot_token, self._cfg.telegram_chat_id)
            )
        if self._cfg.email_enabled:
            self._channels.append(EmailChannel(self._cfg))

    def add_channel(self, channel: Channel) -> None:
        self._channels.append(channel)

    def send(self, alert: Alert) -> None:
        if alert.level < self._min:
            return
        key = f"{alert.category}:{alert.title}"
        now = alert.ts
        last = self._recent.get(key)
        # CRITICAL always goes out; lower levels are throttled.
        if (
            last is not None
            and alert.level < AlertLevel.CRITICAL
            and (now - last).total_seconds() < self._dedupe_seconds
        ):
            return
        self._recent[key] = now
        self.sent.append(alert)
        for ch in self._channels:
            try:
                ch.send(alert)
            except Exception as exc:
                log.error("alert_channel_failed", channel=ch.name, error=str(exc))

    # Convenience wrappers -------------------------------------------------------------
    def info(self, category: str, title: str, body: str = "", **ctx: Any) -> None:
        self.send(Alert(AlertLevel.INFO, category, title, body, ctx))

    def warning(self, category: str, title: str, body: str = "", **ctx: Any) -> None:
        self.send(Alert(AlertLevel.WARNING, category, title, body, ctx))

    def error(self, category: str, title: str, body: str = "", **ctx: Any) -> None:
        self.send(Alert(AlertLevel.ERROR, category, title, body, ctx))

    def critical(self, category: str, title: str, body: str = "", **ctx: Any) -> None:
        self.send(Alert(AlertLevel.CRITICAL, category, title, body, ctx))
