"""First-run bootstrap — owner user from env."""

import logging

from . import config, users

log = logging.getLogger("eos.bootstrap")


def maybe_bootstrap() -> None:
    email = config.BOOTSTRAP_EMAIL.strip().lower()
    password = config.ADMIN_PASSWORD
    if not email or not password:
        return
    users.bootstrap_owner(email, password)