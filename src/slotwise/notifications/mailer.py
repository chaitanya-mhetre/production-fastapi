"""Email sending. SMTP to Mailpit locally; Amazon SES's SMTP interface in AWS (same code path)."""

import asyncio
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage as MimeMessage
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Email:
    to: str
    subject: str
    body: str


class Mailer(Protocol):
    async def send(self, email: Email) -> None: ...


class SmtpMailer:
    def __init__(self, host: str, port: int, sender: str, timeout: float = 10) -> None:
        self.host, self.port, self.sender, self.timeout = host, port, sender, timeout

    async def send(self, email: Email) -> None:
        # smtplib is blocking; run it in a thread so the event loop keeps serving other work.
        await asyncio.to_thread(self._send_sync, email)

    def _send_sync(self, email: Email) -> None:
        msg = MimeMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, email.to, email.subject
        msg.set_content(email.body)
        with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
            smtp.send_message(msg)


class RecordingMailer:
    """Test double: keeps sent emails in memory."""

    def __init__(self) -> None:
        self.sent: list[Email] = []

    async def send(self, email: Email) -> None:
        self.sent.append(email)
