"""
Cloudflare API Token Creator — API-first with UI fallback.

Patch source: https://github.com/andreanocalvin/cloudflare-autologin
Useful extracted parts:
- POST /api/v4/user/tokens from the authenticated dashboard session
- Workers AI permission group IDs:
  - Read:  a92d2450e05d4e7bb7d0a64968f83d11
  - Write: bacc64e0f6c34fc0883a1223f938a104

Why API-first:
- Cloudflare token creation UI is a React SPA and custom checkboxes are brittle.
- The API response contains result.value (cfut_...) directly when successful.

Fallback:
- If API POST is blocked/fails, use /profile/api-tokens UI entry point.
- UI still requires verified Cloudflare email; if email is unverified, no token is returned.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

import nodriver as uc


WORKERS_AI_READ_ID = "a92d2450e05d4e7bb7d0a64968f83d11"
WORKERS_AI_WRITE_ID = "bacc64e0f6c34fc0883a1223f938a104"


class TokenResult:
    def __init__(
        self,
        success: bool,
        token: str = "",
        token_name: str = "",
        error: str = "",
        token_id: str = "",
        method: str = "",
        raw: Any = None,
    ):
        self.success = success
        self.token = token
        self.token_name = token_name
        self.error = error
        self.token_id = token_id
        self.method = method
        self.raw = raw


def _unwrap_nodriver_value(value: Any, default: Any = None) -> Any:
    """Best-effort parser for nodriver page.evaluate() wrapped results."""
    if value is None:
        return default

    # Common wrapper for JSON.stringify output: [[..., {type: 'string', value: '...'}]]
    if isinstance(value, list):
        for item in value:
            if isinstance(item, list) and len(item) >= 2 and isinstance(item[1], dict):
                if "value" in item[1]:
                    return item[1]["value"]
        return value

    if isinstance(value, dict):
        if "value" in value:
            return value["value"]
        return value

    return value


def _loads_wrapped_json(raw: Any, default: Any = None) -> Any:
    unwrapped = _unwrap_nodriver_value(raw, default="{}")
    if isinstance(unwrapped, (dict, list)):
        return unwrapped
    try:
        return json.loads(str(unwrapped))
    except Exception:
        return default


async def _get_body_text(page: uc.Tab) -> str:
    raw = await page.evaluate("document.body ? document.body.innerText : ''")
    return str(_unwrap_nodriver_value(raw, default=""))


async def get_account_id(page: uc.Tab, known_account_id: str = "") -> str:
    """Get Cloudflare account_id from known value, URL, then /api/v4/accounts fallback."""
    if known_account_id and re.fullmatch(r"[a-f0-9]{32}", known_account_id):
        return known_account_id

    try:
        url = str(await page.evaluate("location.href"))
        m = re.search(r"/([a-f0-9]{32})(?:/|$)", url)
        if m:
            return m.group(1)
    except Exception:
        pass

    try:
        raw = await page.evaluate(
            """
            (async () => {
                const r = await fetch('/api/v4/accounts?per_page=50', {
                    credentials: 'include',
                    headers: {'Accept': 'application/json'}
                });
                const text = await r.text();
                return JSON.stringify({status: r.status, body: text});
            })()
            """,
            await_promise=True,
            return_by_value=True,
        )
        data = _loads_wrapped_json(raw, default={}) or {}
        if data.get("status") == 200:
            body = json.loads(data.get("body") or "{}")
            accounts = body.get("result") or []
            if accounts and accounts[0].get("id"):
                return accounts[0]["id"]
    except Exception:
        pass

    return ""


async def create_token_api(
    page: uc.Tab,
    account_id: str = "",
    token_name: str = "workers-ai-auto",
) -> TokenResult:
    """Create a Workers AI API token via Cloudflare dashboard session API."""
    account_id = await get_account_id(page, account_id)

    print("    Trying direct token API POST /api/v4/user/tokens...")
    token_name_js = json.dumps(token_name)
    raw = await page.evaluate(
        f"""
        (async () => {{
            const body = {{
                name: {token_name_js},
                condition: {{}},
                policies: [{{
                    effect: 'allow',
                    resources: {{'com.cloudflare.api.account.*': '*'}},
                    permission_groups: [
                        {{id: 'a92d2450e05d4e7bb7d0a64968f83d11'}},
                        {{id: 'bacc64e0f6c34fc0883a1223f938a104'}}
                    ]
                }}]
            }};

            const r = await fetch('/api/v4/user/tokens', {{
                method: 'POST',
                credentials: 'include',
                headers: {{
                    'Content-Type': 'application/json',
                    'Accept': 'application/json'
                }},
                body: JSON.stringify(body)
            }});
            const text = await r.text();
            return JSON.stringify({{status: r.status, body: text}});
        }})()
        """,
        await_promise=True,
        return_by_value=True,
    )

    data = _loads_wrapped_json(raw, default={}) or {}
    status = data.get("status")
    body_text = data.get("body") or ""

    if status != 200:
        snippet = body_text[:500].replace("\n", " ")
        try:
            err_resp = json.loads(body_text)
            err_messages = " | ".join(
                str(e.get("message", e)) for e in (err_resp.get("errors") or [])
            )
            if "verify" in err_messages.lower() and "email" in err_messages.lower():
                return TokenResult(
                    False,
                    token_name=token_name,
                    error="email_not_verified",
                    method="api",
                    raw={"status": status, "body": err_resp},
                )
        except Exception:
            pass
        if status == 403 and "Attention Required" in body_text:
            return TokenResult(
                False,
                token_name=token_name,
                error="api_waf_403_attention_required",
                method="api",
                raw={"status": status, "body": snippet},
            )
        return TokenResult(
            False,
            token_name=token_name,
            error=f"api_http_{status}: {snippet}",
            method="api",
            raw={"status": status, "body": snippet},
        )

    try:
        resp = json.loads(body_text)
    except Exception:
        return TokenResult(False, token_name=token_name, error="api_invalid_json", method="api", raw=body_text[:500])

    if resp.get("success") and resp.get("result", {}).get("value"):
        return TokenResult(
            True,
            token=resp["result"]["value"],
            token_name=token_name,
            token_id=resp.get("result", {}).get("id", ""),
            method="api",
            raw=resp,
        )

    errors = resp.get("errors") or []
    messages = " | ".join(str(e.get("message", e)) for e in errors) if errors else str(resp)[:500]
    if "verify" in messages.lower() and "email" in messages.lower():
        err = "email_not_verified"
    else:
        err = f"api_token_failed: {messages}"

    return TokenResult(False, token_name=token_name, error=err, method="api", raw=resp)


async def _press_tab(page: uc.Tab) -> None:
    await page.send(uc.cdp.input_.dispatch_key_event("keyDown", key="Tab", code="Tab"))
    await page.send(uc.cdp.input_.dispatch_key_event("keyUp", key="Tab", code="Tab"))


async def _press_space(page: uc.Tab) -> None:
    await page.send(uc.cdp.input_.dispatch_key_event("keyDown", key=" ", code="Space"))
    await page.send(uc.cdp.input_.dispatch_key_event("keyUp", key=" ", code="Space"))


async def _focused_checkbox_state(page: uc.Tab) -> dict:
    raw = await page.evaluate(
        """
        (() => {
            const el = document.activeElement;
            if (!el) return JSON.stringify({tag: 'none'});
            const rect = el.getBoundingClientRect();
            return JSON.stringify({
                tag: el.tagName,
                role: el.getAttribute('role') || '',
                ariaChecked: el.getAttribute('aria-checked') || '',
                text: (el.textContent || '').substring(0, 60),
                x: Math.round(rect.x),
                y: Math.round(rect.y)
            });
        })()
        """
    )
    return _loads_wrapped_json(raw, default={}) or {}


async def create_token_ui(
    page: uc.Tab,
    account_id: str,
    token_name: str = "workers-ai-auto",
    timeout: float = 160,
) -> TokenResult:
    """UI fallback via account API Tokens page -> Create a token.

    User-confirmed working path:
    - https://dash.cloudflare.com/{account_id}/api-tokens
    - Click body link/button "Create a token" (or header "+ Create Token")
    - Lands on /{account_id}/api-tokens/create with Token name, AI & ML, Review token.

    Profile page /profile/api-tokens remains a secondary fallback.
    """
    start = time.time()
    print("    Opening account API Tokens page...")

    account_id = await get_account_id(page, account_id)
    entry_urls = []
    if account_id:
        entry_urls.append(f"https://dash.cloudflare.com/{account_id}/api-tokens")
    entry_urls.append("https://dash.cloudflare.com/profile/api-tokens")

    clicked = False
    for entry_url in entry_urls:
        await page.get(entry_url)
        await asyncio.sleep(12)

        current_url = str(await page.evaluate("location.href"))
        if "login" in current_url.lower():
            return TokenResult(False, token_name=token_name, error="Not logged in — session expired", method="ui")

        # Cookie banner blocks clicks.
        try:
            allow_btn = await page.find("Allow All", best_match=True, timeout=4)
            if allow_btn:
                await allow_btn.click()
                await asyncio.sleep(2)
        except Exception:
            pass

        # Prefer the body link the user identified: "Create a token".
        # Then try header "+ Create Token" / "Create Token".
        print(f"    Clicking Create token entry from {entry_url}...")
        for label in ("Create a token", "Create Token", "Create token"):
            try:
                btn = await page.find(label, best_match=True, timeout=7)
                if btn:
                    await btn.scroll_into_view()
                    await asyncio.sleep(0.5)
                    await btn.click()
                    await asyncio.sleep(10)
                    clicked = True
                    break
            except Exception:
                continue

        if clicked:
            break

    if not clicked:
        return TokenResult(False, token_name=token_name, error="ui_create_token_entry_not_found", method="ui")

    # Some layouts show token templates first; use Custom/Get started if present.
    for label in ("Get started", "Custom"):
        try:
            el = await page.find(label, best_match=True, timeout=5)
            if el:
                await el.scroll_into_view()
                await asyncio.sleep(0.5)
                await el.click()
                await asyncio.sleep(5)
                if label == "Custom":
                    ai_opt = await page.find("AI & Machine Learning", best_match=True, timeout=5)
                    if ai_opt:
                        await ai_opt.click()
                        await asyncio.sleep(5)
                break
        except Exception:
            pass

    # Try fill token name if field exists (Cloudflare often auto-fills random name).
    try:
        name_input = await page.find("Token name", best_match=True, timeout=5)
        if not name_input:
            name_input = await page.find("Enter token name", best_match=True, timeout=5)
        if name_input:
            await name_input.click()
            await asyncio.sleep(0.3)
            # Avoid Control+A portability; append unique suffix is okay if auto-filled.
            await name_input.send_keys(token_name)
            await asyncio.sleep(1)
    except Exception:
        pass

    # Permission: choose AI & ML template and search Workers AI; then Tab+Space on custom checkbox.
    print("    Selecting Workers AI permission via keyboard...")
    try:
        custom_btn = await page.find("Custom", best_match=True, timeout=6)
        if custom_btn:
            await custom_btn.scroll_into_view()
            await asyncio.sleep(0.5)
            await custom_btn.click()
            await asyncio.sleep(3)
            ai_opt = await page.find("AI & Machine Learning", best_match=True, timeout=5)
            if ai_opt:
                await ai_opt.click()
                await asyncio.sleep(5)
    except Exception:
        pass

    search = None
    try:
        search = await page.find("Search for permission groups", best_match=True, timeout=8)
        if search:
            await search.scroll_into_view()
            await asyncio.sleep(0.5)
            await search.click()
            await asyncio.sleep(0.5)
            await search.send_keys("Workers AI")
            await asyncio.sleep(3)
    except Exception:
        pass

    if search:
        await search.scroll_into_view()
        await asyncio.sleep(0.5)
        await search.click()
        await asyncio.sleep(0.5)
        for i in range(30):
            await _press_tab(page)
            await asyncio.sleep(0.25)
            focused = await _focused_checkbox_state(page)
            if focused.get("role") == "checkbox" and focused.get("ariaChecked") == "false":
                await _press_space(page)
                await asyncio.sleep(2)
                focused_after = await _focused_checkbox_state(page)
                if focused_after.get("ariaChecked") == "true":
                    print(f"    ✅ Checked permission checkbox at tab #{i}")
                    break

    # Review button can be "Review token" or "Continue to summary" depending entry point.
    print("    Clicking Review/Continue...")
    reviewed = False
    for label in ("Review token", "Continue to summary"):
        try:
            btn = await page.find(label, best_match=True, timeout=10)
            if btn:
                await btn.scroll_into_view()
                await asyncio.sleep(0.8)
                await btn.click()
                await asyncio.sleep(12)
                reviewed = True
                break
        except Exception:
            continue
    if not reviewed:
        body = await _get_body_text(page)
        if "Please verify your email" in body:
            return TokenResult(False, token_name=token_name, error="email_not_verified", method="ui")
        return TokenResult(False, token_name=token_name, error="ui_review_button_not_found_or_disabled", method="ui")

    body = await _get_body_text(page)
    if "Please verify your email" in body:
        return TokenResult(False, token_name=token_name, error="email_not_verified", method="ui")

    print("    Clicking final Create token...")
    final_clicked = False
    for label in ("Create token", "Create Token"):
        try:
            btn = await page.find(label, best_match=True, timeout=12)
            if btn:
                await btn.scroll_into_view()
                await asyncio.sleep(0.8)
                await btn.click()
                await asyncio.sleep(18)
                final_clicked = True
                break
        except Exception:
            continue
    if not final_clicked:
        return TokenResult(False, token_name=token_name, error="ui_final_create_token_not_found", method="ui")

    # Extract token from HTML/body.
    for _ in range(8):
        content = ""
        try:
            content = str(await page.get_content())
        except Exception:
            pass
        body = await _get_body_text(page)
        for haystack in (content, body):
            token_match = re.search(r"cfut_[A-Za-z0-9_\-]{20,}", haystack)
            if token_match:
                return TokenResult(True, token=token_match.group(0), token_name=token_name, method="ui")
        if "Please verify your email" in body:
            return TokenResult(False, token_name=token_name, error="email_not_verified", method="ui")
        await asyncio.sleep(3)

    elapsed = time.time() - start
    return TokenResult(False, token_name=token_name, error=f"ui_token_not_found_after_{elapsed:.0f}s", method="ui")


async def create_token(
    page: uc.Tab,
    account_id: str,
    token_name: str = "workers-ai-auto",
    timeout: float = 180,
) -> TokenResult:
    """
    Create Cloudflare Workers AI token.

    User-confirmed bot v1 entry point:
    1. Open https://dash.cloudflare.com/{account_id}/api-tokens first
       (this is the Account API Tokens page observed after signup).
    2. Click the body "Create a token" link/button, or header "+ Create Token".
    3. It should open https://dash.cloudflare.com/{account_id}/api-tokens/create
       with Token name, AI & Machine Learning permissions, Review token.
    4. Fill/select token settings and extract cfut_*.

    /profile/api-tokens and direct API are kept only as fallback/debug paths,
    not the primary v1 path.
    """
    ui_result = await create_token_ui(page, account_id=account_id, token_name=token_name, timeout=timeout)
    if ui_result.success:
        print("    ✅ Token created via /profile/api-tokens UI")
        return ui_result

    print(f"    ⚠️ Profile UI token creation failed: {ui_result.error}")

    api_result = await create_token_api(page, account_id=account_id, token_name=token_name)
    if api_result.success:
        print("    ✅ Token created via API fallback")
        return api_result

    api_result.error = f"profile_ui={ui_result.error}; api={api_result.error}"
    return api_result


__all__ = ["TokenResult", "create_token", "create_token_api", "create_token_ui", "get_account_id"]

if __name__ == "__main__":
    print("This module is imported by main.py. Run main.py instead.")
