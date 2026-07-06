"""Cloudflare email verification via temp-mail inbox."""

from __future__ import annotations

import asyncio
import html
import re
import time
from typing import Any
from urllib.parse import unquote

import nodriver as uc

from .email_generator import EmailGenerator


class EmailVerifyResult:
    def __init__(self, success: bool, error: str = "", link: str = ""):
        self.success = success
        self.error = error
        self.link = link


CF_VERIFY_PATTERNS = [
    # Common Cloudflare dashboard/email verification links.
    r'https://dash\.cloudflare\.com/[^\"><\s]+',
    r'https://www\.cloudflare\.com/[^\"><\s]+',
    r'https://cloudflare\.com/[^\"><\s]+',
]


def _mail_blob(mail: dict[str, Any]) -> str:
    parts = []
    for key in ("from", "sender", "subject", "text", "html", "body", "raw", "snippet"):
        val = mail.get(key)
        if val:
            parts.append(str(val))
    return "\n".join(parts)


def _is_cloudflare_verification(mail: dict[str, Any]) -> bool:
    blob = _mail_blob(mail).lower()
    return "cloudflare" in blob and any(
        word in blob for word in ("verify", "verification", "confirm", "activate", "email")
    )


def extract_verification_link(mail: dict[str, Any]) -> str:
    """Extract the most likely Cloudflare verification link from parsed mail."""
    blob = html.unescape(_mail_blob(mail))
    blob = blob.replace("\\/", "/")

    candidates: list[str] = []
    for pattern in CF_VERIFY_PATTERNS:
        candidates.extend(re.findall(pattern, blob, flags=re.I))

    cleaned = []
    for url in candidates:
        url = url.rstrip("').,;]>\"\\")
        url = unquote(url)
        low = url.lower()
        # Keep likely action links, drop generic marketing/docs links.
        if any(k in low for k in ("verify", "confirm", "activation", "email", "token", "challenge")):
            cleaned.append(url)

    # Fallback: if only dash.cloudflare.com links are present, use the first one.
    if not cleaned:
        for url in candidates:
            url = url.rstrip("').,;]>\"\\")
            if "dash.cloudflare.com" in url.lower():
                cleaned.append(unquote(url))

    return cleaned[0] if cleaned else ""


async def verify_cloudflare_email(
    page: uc.Tab,
    mail_api: str,
    jwt: str,
    timeout: int = 120,
    poll_interval: int = 5,
) -> EmailVerifyResult:
    """Poll temp inbox, open Cloudflare verification link in the same browser session."""
    if not jwt:
        return EmailVerifyResult(False, error="missing_mail_jwt")

    print("  [verify] Waiting for Cloudflare verification email...")
    gen = EmailGenerator(mail_api, [])
    start = time.time()

    try:
        while time.time() - start < timeout:
            try:
                mails = gen.check_inbox(jwt, limit=20, offset=0)
            except Exception as e:
                print(f"  [verify] inbox error: {e}")
                await asyncio.sleep(poll_interval)
                continue

            for mail in mails:
                mail_id = str(mail.get("id") or mail.get("mail_id") or mail.get("uid") or mail.get("_id") or "")

                full = mail
                # Some list endpoints only include metadata; fetch body by id when possible.
                if mail_id and not any(full.get(k) for k in ("text", "html", "body", "raw")):
                    try:
                        full = gen.get_mail(jwt, mail_id)
                    except Exception:
                        full = mail

                if not _is_cloudflare_verification(full):
                    continue

                link = extract_verification_link(full)
                if not link:
                    continue

                print("  [verify] Cloudflare verification link found; opening...")
                await page.get(link)
                await asyncio.sleep(15)

                body_raw = await page.evaluate(
                    "document.body ? document.body.innerText : ''",
                    return_by_value=True,
                )
                body = str(body_raw).lower()
                url = str(await page.evaluate("location.href", return_by_value=True)).lower()

                if any(k in body for k in ("verified", "success", "email has been verified", "already verified")):
                    return EmailVerifyResult(True, link=link)
                if "dash.cloudflare.com" in url and "login" not in url:
                    # Cloudflare often redirects to dashboard after successful verification.
                    return EmailVerifyResult(True, link=link)

                if "expired" in body or "invalid" in body:
                    return EmailVerifyResult(False, error="verification_link_invalid_or_expired", link=link)

                return EmailVerifyResult(True, link=link)

            await asyncio.sleep(poll_interval)

        return EmailVerifyResult(False, error=f"verification_email_not_found_after_{timeout}s")
    finally:
        gen.close()


__all__ = ["EmailVerifyResult", "verify_cloudflare_email", "extract_verification_link"]
