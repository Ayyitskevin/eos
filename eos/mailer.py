"""Transactional email — Gmail SMTP or Postmark API, with per-studio branding."""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

import httpx

from . import config, studio

log = logging.getLogger("eos.mailer")


def configured() -> bool:
    if config.EMAIL_PROVIDER == "postmark":
        return bool(config.POSTMARK_API_KEY and config.POSTMARK_FROM_EMAIL)
    return bool(config.GMAIL_USER and config.GMAIL_APP_PASSWORD)


def _default_from_email() -> str:
    return config.POSTMARK_FROM_EMAIL or config.GMAIL_USER or config.CONTACT_EMAIL


def _platform_from_name() -> str:
    return config.PLATFORM_EMAIL_NAME or config.SITE_NAME or "Eos"


def studio_envelope() -> dict[str, str]:
    """Reply-to studio; From shows studio name via platform sender."""
    row = studio.get_studio()
    name = row["name"] if row else _platform_from_name()
    reply = (row["contact_email"] if row else "") or config.CONTACT_REPLY_TO or config.CONTACT_EMAIL
    return {
        "from_name": f"{name} via {_platform_from_name()}",
        "from_email": _default_from_email(),
        "reply_to": reply.strip(),
    }


def send(
    to: str,
    subject: str,
    body: str,
    *,
    reply_to: str = "",
    from_name: str = "",
    from_email: str = "",
) -> None:
    to = to.strip()
    if not to:
        raise ValueError("recipient required")
    display = from_name or _platform_from_name()
    addr = from_email or _default_from_email()
    if config.EMAIL_PROVIDER == "postmark":
        _send_postmark(
            to=to,
            subject=subject,
            body=body,
            from_name=display,
            from_email=addr,
            reply_to=reply_to,
        )
    else:
        _send_smtp(
            to=to,
            subject=subject,
            body=body,
            from_name=display,
            from_email=addr,
            reply_to=reply_to,
        )


def send_for_studio(to: str, subject: str, body: str) -> None:
    env = studio_envelope()
    send(
        to,
        subject,
        body,
        reply_to=env["reply_to"],
        from_name=env["from_name"],
        from_email=env["from_email"],
    )


def send_platform(to: str, subject: str, body: str) -> None:
    send(
        to,
        subject,
        body,
        from_name=_platform_from_name(),
        from_email=_default_from_email(),
        reply_to=config.CONTACT_REPLY_TO or config.CONTACT_EMAIL,
    )


def _send_smtp(
    *,
    to: str,
    subject: str,
    body: str,
    from_name: str,
    from_email: str,
    reply_to: str,
) -> None:
    msg = EmailMessage()
    msg["From"] = f"{from_name} <{from_email}>"
    msg["To"] = to
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as s:
        s.login(config.GMAIL_USER, config.GMAIL_APP_PASSWORD)
        s.send_message(msg)


def _send_postmark(
    *,
    to: str,
    subject: str,
    body: str,
    from_name: str,
    from_email: str,
    reply_to: str,
) -> None:
    payload: dict = {
        "From": f"{from_name} <{from_email}>",
        "To": to,
        "Subject": subject,
        "TextBody": body,
    }
    if reply_to:
        payload["ReplyTo"] = reply_to
    resp = httpx.post(
        "https://api.postmarkapp.com/email",
        headers={
            "X-Postmark-Server-Token": config.POSTMARK_API_KEY,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=20.0,
    )
    if resp.status_code >= 400:
        log.error("postmark error %s: %s", resp.status_code, resp.text[:300])
        raise RuntimeError(f"Postmark send failed ({resp.status_code})")