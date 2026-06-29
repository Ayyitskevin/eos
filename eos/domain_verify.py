"""Custom domain DNS verification — CNAME to tenant subdomain or TXT token."""

from __future__ import annotations

import logging
import secrets

import httpx

from . import config, db
from .vocab import STUDIO_ID

log = logging.getLogger("eos.domain_verify")

_DNS_API = "https://dns.google/resolve"


def issue_token() -> str:
    return f"eos-{secrets.token_hex(8)}"


def expected_cname_target(slug: str) -> str:
    return f"{slug}.{config.BASE_DOMAIN}".lower()


def verification_instructions(*, domain: str, slug: str, token: str) -> dict:
    domain = domain.lower().strip()
    cname_target = expected_cname_target(slug) if config.BASE_DOMAIN else ""
    return {
        "domain": domain,
        "cname_host": domain,
        "cname_target": cname_target,
        "txt_host": f"_eos-verify.{domain}",
        "txt_value": token,
    }


def _dns_lookup(name: str, rtype: str) -> list[str]:
    try:
        resp = httpx.get(_DNS_API, params={"name": name, "type": rtype}, timeout=8.0)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.warning("dns lookup failed %s %s: %s", name, rtype, exc)
        return []
    answers = data.get("Answer") or []
    out: list[str] = []
    for ans in answers:
        val = str(ans.get("data", "")).strip().rstrip(".")
        if val:
            out.append(val.lower())
    return out


def verify_domain(*, domain: str, slug: str, token: str) -> tuple[bool, str]:
    domain = domain.lower().strip()
    if not domain:
        return False, "Enter a hostname."

    cname_vals = _dns_lookup(domain, "CNAME")
    if config.BASE_DOMAIN:
        target = expected_cname_target(slug)
        for val in cname_vals:
            if val == target or val.endswith(f".{target}"):
                return True, f"CNAME points to {target}"

    txt_vals = _dns_lookup(f"_eos-verify.{domain}", "TXT")
    want = token.lower()
    for val in txt_vals:
        if want in val.replace('"', "").lower():
            return True, "TXT verification record found"

    if config.BASE_DOMAIN:
        return (
            False,
            f"Add CNAME {domain} → {expected_cname_target(slug)} "
            f"or TXT _eos-verify.{domain} → {token}",
        )
    return False, f"Add TXT _eos-verify.{domain} → {token}"


def save_pending_domain(domain: str) -> str:
    domain = domain.strip().lower()
    if not domain:
        db.run(
            """UPDATE studio SET custom_domain=NULL, custom_domain_verified=0,
               domain_verify_token='' WHERE id=?""",
            (STUDIO_ID,),
        )
        return ""
    token = issue_token()
    db.run(
        """UPDATE studio SET custom_domain=?, custom_domain_verified=0,
           domain_verify_token=? WHERE id=?""",
        (domain, token, STUDIO_ID),
    )
    return token


def try_verify_saved() -> tuple[bool, str]:
    row = db.one(
        """SELECT custom_domain, slug, domain_verify_token, custom_domain_verified
           FROM studio WHERE id=?""",
        (STUDIO_ID,),
    )
    if not row or not row["custom_domain"]:
        return False, "No custom domain configured."
    if row["custom_domain_verified"]:
        return True, "Already verified."
    token = row["domain_verify_token"] or issue_token()
    if not row["domain_verify_token"]:
        db.run("UPDATE studio SET domain_verify_token=? WHERE id=?", (token, STUDIO_ID))
    ok, msg = verify_domain(
        domain=row["custom_domain"],
        slug=row["slug"],
        token=token,
    )
    if ok:
        db.run("UPDATE studio SET custom_domain_verified=1 WHERE id=?", (STUDIO_ID,))
        db.audit("admin", "domain.verified", row["custom_domain"])
    return ok, msg
