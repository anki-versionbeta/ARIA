"""Telling the owners a request is waiting.

Mail is best-effort and deliberately cannot fail a request. A person who filled in the form
has done their part; if the relay is down, the request is still queued, the bell still
counts it, and the admin still sees it on the screen. Losing the notification is a nuisance,
losing the request is a bug.

Sends to the fixed administrators only -- the same constant that makes them undemotable, so
"who owns access" is stated once.

Talks to `smtp.abbvienet.com` directly. Not the local Postfix: it is installed here with
`relayhost` set, but it refuses to relay for non-local recipients and answers with its own

    554 5.7.1 <...>: Recipient address rejected: Access denied

which reads exactly like a rejection from the corporate relay and is not one. I lost time to
that. PDS&T Awards reaches the relay directly, with no auth and no STARTTLS, so this route is
already proven on this network.

NOTE (2026-08-19): the relay does not yet permit this host. Connecting directly gives

    554 ux00759p.abbvienet.com  IP Not Permitted

which is the relay refusing the source address, not the message -- 10.224.134.56 is not on
the Mail Relay Authorized Sender list. A ServiceNow mail-relay request adds it; nothing here
changes when it lands. Until then every send logs a warning naming that cause, and the
request itself is unaffected.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.backend.da_platform.auth.access import PROTECTED_ADMIN_USERNAMES
from api.backend.da_platform.db.models import User
from api.backend.da_platform.settings import settings

logger = logging.getLogger(__name__)


def _owner_addresses(session: Session) -> list[str]:
    rows = session.scalars(
        select(User).where(func.lower(User.username).in_(PROTECTED_ADMIN_USERNAMES))
    ).all()
    return [row.email for row in rows if row.email]


def access_request_raised(
    session: Session, requester: User, modules: list[str], note: str | None
) -> None:
    """Email the owners that somebody is waiting. Never raises."""
    try:
        recipients = _owner_addresses(session)
        if not recipients:
            logger.warning(
                "No owner email addresses on file; access request from %s not notified",
                requester.username,
            )
            return

        message = EmailMessage()
        message["From"] = settings.mail_from
        message["To"] = ", ".join(recipients)
        message["Subject"] = (
            f"ARIA access request — {requester.display_name} ({requester.username})"
        )
        body = [
            f"{requester.display_name} ({requester.username}) has requested access to ARIA.",
            "",
            f"Modules: {', '.join(modules)}",
        ]
        if note:
            body += ["", f"Reason given: {note}"]
        body += [
            "",
            "Approve or reject it under User management:",
            f"  {settings.app_base_url}/users",
        ]
        message.set_content("\n".join(body))

        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
            smtp.send_message(message)
        logger.info(
            "Notified %s of the access request from %s",
            ", ".join(recipients),
            requester.username,
        )
    except Exception as exc:
        # Deliberately broad: this is a notification. Whatever went wrong, the request has
        # already been saved and the admin will still see it in the app.
        logger.warning(
            "Could not email the access request from %s (%s). The request is saved and "
            "visible in User management regardless. 'IP Not Permitted' from %s means this "
            "host is not on the Mail Relay Authorized Sender list yet -- a ServiceNow "
            "mail-relay request for 10.224.134.56 fixes it, with no code change.",
            requester.username,
            exc,
            settings.smtp_host,
        )
