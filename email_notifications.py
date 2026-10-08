"""SMTP email notifications for Fernwood checkout and payment events."""

from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage


DEFAULT_RECIPIENT = "harish9.cz@gmail.com"


class EmailNotificationError(RuntimeError):
    """An order or payment notification could not be delivered."""


def _settings() -> tuple[str, int, str, str, str, str]:
    host = os.environ.get("SMTP_HOST", "").strip()
    username = os.environ.get("SMTP_USERNAME", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")
    sender = os.environ.get("SMTP_FROM", username).strip()
    recipient = os.environ.get("ORDER_NOTIFICATION_EMAIL", DEFAULT_RECIPIENT).strip()
    try:
        port = int(os.environ.get("SMTP_PORT", "587"))
    except ValueError:
        raise EmailNotificationError("SMTP_PORT must be a valid port number.") from None
    if not host or not username or not password or not sender or not recipient:
        raise EmailNotificationError(
            "Set SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD, and SMTP_FROM "
            "to enable checkout email notifications."
        )
    if not 1 <= port <= 65535:
        raise EmailNotificationError("SMTP_PORT must be between 1 and 65535.")
    return host, port, username, password, sender, recipient


def validate_configuration() -> None:
    _settings()


def send_notification(subject: str, body: str) -> None:
    host, port, username, password, sender, recipient = _settings()
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message.set_content(body)
    context = ssl.create_default_context()

    try:
        if port == 465:
            with smtplib.SMTP_SSL(
                host, port, timeout=15, context=context
            ) as server:
                server.login(username, password)
                server.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=15) as server:
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
                server.login(username, password)
                server.send_message(message)
    except (OSError, smtplib.SMTPException) as error:
        raise EmailNotificationError(
            "Could not send the order email. Check SMTP settings and try again."
        ) from error


def order_created(
    order_id: str,
    amount_paise: int,
    currency: str,
    customer: dict[str, str],
    items: list[dict[str, object]],
) -> None:
    rows = []
    for item in items:
        line_total = int(item["unit_price_paise"]) * int(item["quantity"])
        rows.append(
            f"- {item['name']} (ID: {item['product_id']}) "
            f"x {item['quantity']}: {currency} {line_total / 100:.2f}"
        )
    address = (
        f"{customer['address']}, {customer['city']}, {customer['state']} "
        f"{customer['pincode']}"
    )
    body = "\n".join(
        [
            "A new Fernwood checkout was created. Payment is not yet confirmed.",
            "",
            f"Razorpay order: {order_id}",
            f"Order total: {currency} {amount_paise / 100:.2f}",
            "",
            "Products:",
            *rows,
            "",
            "Customer:",
            f"Name: {customer['name']}",
            f"Email: {customer['email'] or '(not provided)'}",
            f"Phone: {customer['phone']}",
            f"Delivery address: {address}",
        ]
    )
    send_notification(f"Fernwood checkout created: {order_id}", body)


def payment_confirmed(order_id: str, payment_id: str, amount_paise: int, currency: str) -> None:
    body = "\n".join(
        [
            "Razorpay has confirmed a captured Fernwood payment.",
            "",
            f"Razorpay order: {order_id}",
            f"Payment ID: {payment_id}",
            f"Paid: {currency} {amount_paise / 100:.2f}",
            "Status: PAID",
        ]
    )
    send_notification(f"Fernwood payment confirmed: {order_id}", body)
