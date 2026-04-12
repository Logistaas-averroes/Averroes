"""
scheduler/delivery.py

Report delivery module for the Logistaas Ads Intelligence System.
Delivers completed report files to the configured recipient email via SendGrid.

Responsibility: delivery only. No business logic, no analysis, no API writes.

Supported report patterns:
  - outputs/weekly_report_YYYY-MM-DD.md
  - outputs/monthly_report_YYYY-MM.md

Required environment variables:
  SENDGRID_API_KEY       — SendGrid API key
  REPORT_SENDER_EMAIL    — Verified sender address (must be authenticated in SendGrid)
  REPORT_RECIPIENT_EMAIL — Recipient address for delivered reports
"""

import base64
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Attachment, Disposition, FileContent, FileName, FileType, Mail

load_dotenv()

log = logging.getLogger(__name__)


def deliver_report(report_path: str) -> bool:
    """
    Deliver a completed report file to the configured recipient via email.

    Args:
        report_path: Path to the report file (e.g. outputs/weekly_report_2025-01-06.md).

    Returns:
        True on successful delivery, False on any failure.

    Raises:
        Nothing — all failures are logged and False is returned for clean failure handling.
    """
    log.info(f"[DELIVERY START] report_path={report_path}")

    # Validate file exists before attempting delivery
    path = Path(report_path)
    if not path.exists():
        log.error(f"[DELIVERY FAILURE] Report file not found: {report_path}")
        return False

    # Read required env vars
    api_key = os.environ.get("SENDGRID_API_KEY", "").strip()
    sender = os.environ.get("REPORT_SENDER_EMAIL", "").strip()
    recipient = os.environ.get("REPORT_RECIPIENT_EMAIL", "").strip()

    if not api_key:
        log.error("[DELIVERY FAILURE] SENDGRID_API_KEY is not set")
        return False
    if not sender:
        log.error("[DELIVERY FAILURE] REPORT_SENDER_EMAIL is not set")
        return False
    if not recipient:
        log.error("[DELIVERY FAILURE] REPORT_RECIPIENT_EMAIL is not set")
        return False

    # Read report content
    try:
        report_content = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.error(f"[DELIVERY FAILURE] Could not read report file: {exc}")
        return False

    # Build email subject from filename
    # e.g. weekly_report_2025-01-06.md → "Logistaas Weekly Report — 2025-01-06"
    stem = path.stem  # e.g. weekly_report_2025-01-06
    if stem.startswith("weekly_report_"):
        date_label = stem[len("weekly_report_"):]
        subject = f"Logistaas Weekly Ads Intelligence Report — {date_label}"
    elif stem.startswith("monthly_report_"):
        date_label = stem[len("monthly_report_"):]
        subject = f"Logistaas Monthly Ads Intelligence Report — {date_label}"
    else:
        subject = f"Logistaas Ads Intelligence Report — {path.name}"

    # Build email with report as plain-text body and .md file attachment
    encoded_content = base64.b64encode(report_content.encode("utf-8")).decode()

    message = Mail(
        from_email=sender,
        to_emails=recipient,
        subject=subject,
        plain_text_content=report_content,
    )

    attachment = Attachment(
        file_content=FileContent(encoded_content),
        file_name=FileName(path.name),
        file_type=FileType("text/markdown"),
        disposition=Disposition("attachment"),
    )
    message.attachment = attachment

    # Send via SendGrid
    try:
        client = SendGridAPIClient(api_key)
        response = client.send(message)
        status = response.status_code
    except Exception as exc:
        log.error(f"[DELIVERY FAILURE] SendGrid error: {exc}")
        return False

    # 2xx = accepted
    if 200 <= status < 300:
        log.info(f"[DELIVERY SUCCESS] report={path.name} recipient={recipient} status={status}")
        return True

    log.error(f"[DELIVERY FAILURE] SendGrid returned status={status} for report={path.name}")
    return False
