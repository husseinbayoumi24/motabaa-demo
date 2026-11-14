"""mailer.py — sends the password-reset code by e-mail (SMTP).

Gmail / Google Workspace (faculty mail): create an "App password" for the sending account and set
MOTABAA_SMTP_HOST=smtp.gmail.com, MOTABAA_SMTP_PORT=465, MOTABAA_SMTP_USER=<account>,
MOTABAA_SMTP_PASSWORD=<app password>. The password is read from environment variables only and
is never logged or stored in the database.
"""
import logging
import smtplib
import ssl
from email.message import EmailMessage

import config

log = logging.getLogger("motabaa.mail")


def is_configured() -> bool:
    return bool(config.SMTP_HOST and config.SMTP_USER and config.SMTP_PASSWORD)


def _send(to_email: str, subject: str, body: str) -> str:
    """Returns 'sent', 'not_configured' or 'failed'. Failures are logged WITHOUT any secret."""
    if not is_configured():
        return "not_configured"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.SMTP_FROM or config.SMTP_USER
    msg["To"] = to_email
    msg.set_content(body)
    try:
        ctx = ssl.create_default_context()
        if config.SMTP_PORT == 465:
            server = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=15, context=ctx)
        else:
            server = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=15)
            server.starttls(context=ctx)
        with server:
            server.login(config.SMTP_USER, config.SMTP_PASSWORD)
            server.send_message(msg)
        return "sent"
    except Exception as exc:                       # network, TLS, auth ... : log the TYPE only, never credentials
        log.warning("e-mail to a user failed: %s", type(exc).__name__)
        return "failed"


def send_reset_code(to_email: str, code: str, minutes: int = 10, org: str = "") -> str:
    """Password-reset code. `org` = university / faculty name typed by the admin (shown in the message)."""
    return _send(
        to_email, "كود استعادة كلمة المرور — مُتابع | Password reset code",
        f"{org}\n"
        f"كود استعادة كلمة المرور: {code}\n"
        f"الكود صالح لمدة {minutes} دقائق ولا تعطيه لأي شخص.\n"
        f"إذا لم تطلب ذلك تجاهل هذه الرسالة.\n\n"
        f"Motabaa — {org}\n"
        f"Your password reset code: {code}\n"
        f"It is valid for {minutes} minutes. Never share it. If you did not request it, ignore this e-mail.")


def send_activation_code(to_email: str, code: str, minutes: int = 10, org: str = "") -> str:
    """Account-activation code, sent to the student's UNIVERSITY mailbox to prove they own it."""
    return _send(
        to_email, "كود تفعيل الحساب — مُتابع | Account activation code",
        f"{org}\n"
        f"كود تفعيل حسابك: {code}\n"
        f"الكود صالح لمدة {minutes} دقائق ولا تعطيه لأي شخص. إذا لم تطلب ذلك تجاهل هذه الرسالة.\n\n"
        f"Motabaa — {org}\n"
        f"Your account activation code: {code}\n"
        f"It is valid for {minutes} minutes. Never share it. If you did not request it, ignore this e-mail.")
