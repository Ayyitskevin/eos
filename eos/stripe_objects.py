"""Convert SDK resources only at boundaries that consume dictionary data."""

import stripe


def as_dict(value: dict | stripe.StripeObject) -> dict:
    """Keep plain payloads unchanged; recursively convert SDK metadata and objects."""
    return value.to_dict() if isinstance(value, stripe.StripeObject) else value
