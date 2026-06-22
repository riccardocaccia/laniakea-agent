"""
Laniakea Notifier sends email notifications to users via gmail SMTP
once the deployment is either failed or completed.
"""

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)

# SMTP info
# NOTE: default value ...
SMTP_HOST     = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT     = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER     = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM     = os.getenv("SMTP_FROM", "Laniakea <email@example.org>")


def _send_email(to: str, subject: str, body_html: str, body_text: str):
    """
    The feature attaches both a plain text version and an HTML-formatted version to the message. 
    If the user opens the email on an older device or with HTML disabled, 
    they'll see the plain text, otherwise, they'll see the graphics formatted with bold text.

      1. Opens the connection to the SMTP server (smtp.gmail.com).
      2. Sends the ehlo() (Extended HELLO) command to introduce itself to the server.
      3. Runs starttls() to encrypt the connection before entering passwords
      4. Logs in and sends the packet.
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_FROM
    msg["To"]      = to

    msg.attach(MIMEText(body_text, "plain"))
    msg.attach(MIMEText(body_html, "html"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()                                     # extended HELLO
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, to, msg.as_string())


def send_success(to: str, username: str, deployment_uuid: str, vm_ip: str | None = None):
    """
    Deployment completed case
    """
    if not to:
        # TODO: improve this case -> an email must be present? 
        logger.warning(f"[{deployment_uuid}] If no email address provided the success notification is skipped.")
        return

    # Do not leave a blank space if ip not present
    ip_line_html = f"<p><b>IP della VM:</b> {vm_ip}</p>" if vm_ip else ""
    ip_line_text = f"IP della VM: {vm_ip}\n" if vm_ip else ""

    subject = f"[Laniakea] ✅ Deployment ✅  {deployment_uuid} completed"

    body_html = f"""
    <html><body>
    <p>Hello <b>{username}</b>,</p>
    <p>Your deployment has been completed successfully.</p>
    <p><b>Deployment ID:</b> {deployment_uuid}</p>
    {ip_line_html}
    <p>You can access the dashboard to view the details.</p>
    <br>
    <p>— The Laniakea Team</p>
    </body></html>
    """

    body_text = (
        f"Hello {username},\n\n"
        f"Your deployment has been completed successfully.\n\n"
        f"Deployment ID: {deployment_uuid}\n"
        f"{ip_line_text}"
        f"\nYou can access the dashboard to view the details.\n\n"
        f"— The Laniakea Team"
    )

    try:
        _send_email(to, subject, body_html, body_text)
        logger.info(f"[{deployment_uuid}] Success notification sent to {to}")

    # error in email sending
    except Exception as exc:
        logger.error(f"[{deployment_uuid}] Failed to send success email to {to}: {exc}")


def send_failure(to: str, username: str, deployment_uuid: str, reason: str | None = None):
    """
    Failed deployment case
    """
    if not to:
        logger.warning(f"[{deployment_uuid}] If no email address provided the success notification is skipped.")
        return

    # Reason of the failure
    reason_line_html = f"<p><b>Reason:</b> {reason}</p>" if reason else ""
    reason_line_text = f"Reason: {reason}\n" if reason else ""

    subject = f"[Laniakea] ❌ Deployment ❌ {deployment_uuid} failed"

    body_html = f"""
    <html><body>
    <p>Hello <b>{username}</b>,</p>
    <p>Unfortunately, your deployment encountered an error and has been canceled.</p>
    <p>The cloud resources have been automatically cleaned up.</p>
    <p><b>Deployment ID:</b> {deployment_uuid}</p>
    {reason_line_html}
    <p>You can try again from the dashboard or contact support if the problem persists.</p>
    <br>
    <p>— The Laniakea Team</p>
    </body></html>
    """

    body_text = (
        f"Hello {username},\n\n"
        f"Unfortunately, your deployment encountered an error and has been canceled.\n"
        f"The cloud resources have been automatically cleaned up.\n\n"
        f"Deployment ID: {deployment_uuid}\n"
        f"{reason_line_text}"
        f"\nYou can try again from the dashboard or contact support if the problem persists.\n\n"
        f"— The Laniakea Team"
    )

    try:
        _send_email(to, subject, body_html, body_text)
        logger.info(f"[{deployment_uuid}] Failure notification sent to {to}")

    # error in mail sending
    except Exception as exc:
        logger.error(f"[{deployment_uuid}] Failed to send failure email to {to}: {exc}")
