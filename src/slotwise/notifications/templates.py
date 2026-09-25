"""Plain-text email bodies. Times are shown in the tenant's local timezone."""

from datetime import datetime
from zoneinfo import ZoneInfo

from slotwise.notifications.mailer import Email

SUBJECTS = {
    "booking.created": "Your booking at {tenant} is received",
    "booking.confirmed": "Your booking at {tenant} is confirmed",
    "booking.rescheduled": "Your booking at {tenant} has moved",
    "booking.cancelled": "Your booking at {tenant} was cancelled",
    "reminder": "Reminder: {service} at {tenant} tomorrow",
}


def booking_email(
    kind: str, *, to: str, customer: str, tenant: str, tz: str, service: str, starts_at: datetime
) -> Email:
    local = starts_at.astimezone(ZoneInfo(tz)).strftime("%a %d %b %Y, %H:%M")
    subject = SUBJECTS[kind].format(tenant=tenant, service=service)
    body = (
        f"Hi {customer},\n\n"
        f"{subject}.\n\n"
        f"Service: {service}\nWhen: {local} ({tz})\n\n"
        f"- {tenant} (sent by Slotwise)\n"
    )
    return Email(to=to, subject=subject, body=body)
