"""
Laniakea Notifier — sends email notifications to users via Gmail SMTP.
"""

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)

SMTP_HOST     = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT     = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER     = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM     = os.getenv("SMTP_FROM", "Laniakea <email@example.org>")


def _send_email(to: str, subject: str, body_html: str, body_text: str):
    """
    Base send function that raises on failure.
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_FROM
    msg["To"]      = to

    msg.attach(MIMEText(body_text, "plain"))
    msg.attach(MIMEText(body_html, "html"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, to, msg.as_string())


def send_success(to: str, username: str, deployment_uuid: str, vm_ip: str | None = None):
    """
    Deployment completed notify.
    """
    if not to:
        logger.warning(f"[{deployment_uuid}] No email address — skipping success notification.")
        return

    ip_line_html = f"<p><b>IP della VM:</b> {vm_ip}</p>" if vm_ip else ""
    ip_line_text = f"IP della VM: {vm_ip}\n" if vm_ip else ""

    subject = f"[Laniakea] ✅ Deployment {deployment_uuid} completato"

    body_html = f"""
    <html><body>
    <p>Ciao <b>{username}</b>,</p>
    <p>Il tuo deployment è stato completato con successo.</p>
    <p><b>Deployment ID:</b> {deployment_uuid}</p>
    {ip_line_html}
    <p>Puoi accedere alla dashboard per visualizzare i dettagli.</p>
    <br>
    <p>— Il team Laniakea</p>
    </body></html>
    """

    body_text = (
        f"Ciao {username},\n\n"
        f"Il tuo deployment è stato completato con successo.\n\n"
        f"Deployment ID: {deployment_uuid}\n"
        f"{ip_line_text}"
        f"\nPuoi accedere alla dashboard per visualizzare i dettagli.\n\n"
        f"— Il team Laniakea"
    )

    try:
        _send_email(to, subject, body_html, body_text)
        logger.info(f"[{deployment_uuid}] Success notification sent to {to}")
    except Exception as exc:
        logger.error(f"[{deployment_uuid}] Failed to send success email to {to}: {exc}")


def send_failure(to: str, username: str, deployment_uuid: str, reason: str | None = None):
    """
    Deployment failed.
    """
    if not to:
        logger.warning(f"[{deployment_uuid}] No email address — skipping failure notification.")
        return

    reason_line_html = f"<p><b>Motivo:</b> {reason}</p>" if reason else ""
    reason_line_text = f"Motivo: {reason}\n" if reason else ""

    subject = f"[Laniakea] ❌ Deployment {deployment_uuid} fallito"

    body_html = f"""
    <html><body>
    <p>Ciao <b>{username}</b>,</p>
    <p>Purtroppo il tuo deployment ha incontrato un errore ed è stato annullato.</p>
    <p>Le risorse cloud sono state ripulite automaticamente.</p>
    <p><b>Deployment ID:</b> {deployment_uuid}</p>
    {reason_line_html}
    <p>Puoi riprovare dalla dashboard o contattare il supporto se il problema persiste.</p>
    <br>
    <p>— Il team Laniakea</p>
    </body></html>
    """

    body_text = (
        f"Ciao {username},\n\n"
        f"Purtroppo il tuo deployment ha incontrato un errore ed è stato annullato.\n"
        f"Le risorse cloud sono state ripulite automaticamente.\n\n"
        f"Deployment ID: {deployment_uuid}\n"
        f"{reason_line_text}"
        f"\nPuoi riprovare dalla dashboard o contattare il supporto se il problema persiste.\n\n"
        f"— Il team Laniakea"
    )

    try:
        _send_email(to, subject, body_html, body_text)
        logger.info(f"[{deployment_uuid}] Failure notification sent to {to}")
    except Exception as exc:
        logger.error(f"[{deployment_uuid}] Failed to send failure email to {to}: {exc}")
