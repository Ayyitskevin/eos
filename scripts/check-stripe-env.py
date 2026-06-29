#!/usr/bin/env python3
"""Print Stripe test-mode readiness for local dogfood."""

from __future__ import annotations

import os

KEY = "EOS_STRIPE_PLATFORM_SECRET_KEY"
WEBHOOK = "EOS_STRIPE_PLATFORM_WEBHOOK_SECRET"
STARTER = "EOS_STRIPE_PRICE_STARTER"
PRO = "EOS_STRIPE_PRICE_PRO"


def main() -> int:
    sk = os.environ.get(KEY, "")
    wh = os.environ.get(WEBHOOK, "")
    starter = os.environ.get(STARTER, "")
    pro = os.environ.get(PRO, "")
    ok = True
    if not sk:
        print(f"MISSING {KEY}")
        ok = False
    elif not sk.startswith("sk_test_"):
        print(f"WARN {KEY} is not a test key (expected sk_test_…)")
    else:
        print(f"OK   {KEY}")
    if not wh:
        print(f"MISSING {WEBHOOK} — run: make stripe-listen")
        ok = False
    else:
        print(f"OK   {WEBHOOK}")
    if not starter:
        print(f"MISSING {STARTER}")
        ok = False
    else:
        print(f"OK   {STARTER}")
    if not pro:
        print(f"MISSING {PRO}")
        ok = False
    else:
        print(f"OK   {PRO}")
    if ok:
        print("\nStripe test env ready — see docs/STRIPE_TEST.md")
        return 0
    print("\nIncomplete — see docs/STRIPE_TEST.md")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
