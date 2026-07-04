"""
Turnstile Bypass — Solves Cloudflare Turnstile using nodriver's built-in verify_cf().

The ONLY approach that works (July 2026). No OpenCV, no xdotool, no template matching.
Just call: await page.verify_cf()
"""

import asyncio
from typing import Optional


async def verify_cf(page, timeout: float = 30.0) -> str:
    """Solve Turnstile using nodriver's built-in method."""
    result = await page.verify_cf()
    return result or ""


async def is_turnstile_present(page) -> bool:
    """Check if Turnstile CAPTCHA is present."""
    return bool(await page.evaluate('''(() => {
        if (document.querySelector('input[name="cf_challenge_response"]')) return true;
        if (document.querySelector('input[name="cf-turnstile-response"]')) return true;
        const iframes = document.querySelectorAll("iframe");
        for (const f of iframes) {
            if (f.src && f.src.includes("challenges.cloudflare.com")) return true;
        }
        const body = document.body.innerText;
        if (body.includes("Verify you are human") || body.includes("Let us know you are human")) return true;
        return false;
    })()'''))
