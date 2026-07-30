"""RE email templates — gallery delivery, MLS ready, proposals, invoices."""

from . import tenant

KINDS = ("gallery", "proposal", "contract", "invoice")


def _first(name: str) -> str:
    return (name or "there").split()[0]


def gallery_delivery(
    *,
    client_name: str,
    title: str,
    link: str,
    pin: str,
    expires: str | None,
    rebook_link: str = "",
    referral_link: str = "",
) -> tuple[str, str]:
    subject = f"Your listing photos are ready — {title}"
    body = f"""Hi {_first(client_name)},

Your gallery is ready to view and download:

{link}
PIN: {pin}
"""
    if expires:
        body += f"Available until {expires}\n"
    if rebook_link:
        body += f"\nReady for the next listing? Book here: {rebook_link}\n"
    if referral_link:
        body += f"\nKnow an agent who could use us? Share your link: {referral_link}\n"
    body += f"\nThank you!\n{tenant.get_site_name()}"
    return subject, body


def agent_view_digest(*, client_name: str, rows: list[dict], total_views: int) -> tuple[str, str]:
    """Weekly per-agent traffic digest — one line + link per active listing."""
    noun = "view" if total_views == 1 else "views"
    subject = f"Your listings got {total_views} {noun} this week"
    lines = [
        f"Hi {_first(client_name)},",
        "",
        "Here's how your listings performed over the last 7 days:",
        "",
    ]
    for row in rows:
        lines.append(
            f"{row['address']} — {row['views']} views · {row['unique_visitors']} unique visitors"
        )
        lines.append(row["url"])
        lines.append("")
    lines.append(f"Thank you!\n{tenant.get_site_name()}")
    return subject, "\n".join(lines)


def lead_notification(
    *, name: str, email: str, phone: str, message: str, address: str, site_url: str
) -> tuple[str, str]:
    subject = f"New property lead — {address}"
    lines = [
        "Someone asked about your listing on the property site:",
        "",
        f"Property: {address}",
        f"Name: {name}",
        f"Email: {email}",
    ]
    if phone:
        lines.append(f"Phone: {phone}")
    if message:
        lines += ["", "Message:", message]
    lines += ["", site_url, "", f"Thank you!\n{tenant.get_site_name()}"]
    return subject, "\n".join(lines)


def gallery_mls_ready(*, client_name: str, title: str, link: str, pin: str) -> tuple[str, str]:
    subject = f"MLS-ready exports included — {title}"
    body = f"""Hi {_first(client_name)},

Your gallery is live with MLS and Zillow-sized exports built in:

{link}
PIN: {pin}

Download individual photos or the full ZIP from the gallery page.

Thank you!
{tenant.get_site_name()}"""
    return subject, body


def proposal_send(*, client_name: str, title: str, link: str) -> tuple[str, str]:
    subject = f"Proposal — {title}"
    body = f"""Hi {_first(client_name)},

Here's your photography proposal for review:

{link}

You can accept or decline directly on that page. Reply to this email with any questions.

Thank you!
{tenant.get_site_name()}"""
    return subject, body


def contract_send(*, client_name: str, title: str, link: str) -> tuple[str, str]:
    subject = f"Services agreement — {title}"
    body = f"""Hi {_first(client_name)},

Please review and sign the photography services agreement:

{link}

Type your full name on the page to sign electronically.

Thank you!
{tenant.get_site_name()}"""
    return subject, body


def invoice_send(*, client_name: str, title: str, link: str, amount: str) -> tuple[str, str]:
    subject = f"Invoice — {title}"
    body = f"""Hi {_first(client_name)},

Invoice for {amount}:

{link}

Thank you!
{tenant.get_site_name()}"""
    return subject, body


TEMPLATES = {
    "gallery_delivery": gallery_delivery,
    "gallery_mls": gallery_mls_ready,
    "proposal": proposal_send,
    "contract": contract_send,
    "invoice": invoice_send,
}
