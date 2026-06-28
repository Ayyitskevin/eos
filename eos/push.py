"""Web push subscription storage (send requires VAPID keys — future)."""

from . import db
from .vocab import STUDIO_ID


def save_subscription(
    *,
    endpoint: str,
    p256dh: str,
    auth: str,
    client_id: int | None = None,
) -> None:
    db.run(
        """INSERT INTO push_subscriptions (studio_id, client_id, endpoint, p256dh, auth)
           VALUES (?,?,?,?,?)
           ON CONFLICT(endpoint) DO UPDATE SET p256dh=excluded.p256dh, auth=excluded.auth""",
        (STUDIO_ID, client_id, endpoint.strip(), p256dh.strip(), auth.strip()),
    )
