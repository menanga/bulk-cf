"""
Cloudflare API Token Creator — full-access token, created directly via API.

Docs (latest, 2026-09-28):
- https://developers.cloudflare.com/fundamentals/api/how-to/create-via-api/
- https://developers.cloudflare.com/api/resources/user/subresources/tokens/methods/create/

How full access is built (per docs):
1. GET /api/v4/user/tokens/permission_groups -> every selectable permission group
   (ids only; group names are cosmetic and subject to change).
2. POST /api/v4/user/tokens with one allow-policy PER RESOURCE SCOPE:
   account-scoped groups over "com.cloudflare.api.account.*", zone-scoped
   groups over "com.cloudflare.api.account.zone.*", user-scoped groups over
   "com.cloudflare.api.user.tag.*". A permission group only applies to the
   resource type it is scoped for — mixing all groups into a single policy
   fails validation with HTTP 400 ("These rules must pass for ...").
   Each policy is also chunked to max 300 permission_groups (API limit).
3. If the full-access payload is still rejected (400), fall back to a
   Workers AI Read/Write + Workers Scripts: Edit + Account Analytics: Read
   token so every account is AI-capable, can edit Workers scripts, and can
   read account analytics. Both name-resolved groups are matched by exact
   name in the live listing and are REQUIRED: if either is absent, the
   fallback variant is dropped and the run fails hard rather than minting a
   token that silently lacks a promised capability.
4. The response contains result.value (cfut_...) — the secret is shown only once.

Why API-only:
- Cloudflare token creation UI is a React SPA and custom checkboxes are brittle.
- The website journey (/profile/api-tokens click-through) was removed; the API
  POST from the authenticated dashboard session is now the sole creation path.

Authentication note:
- Normally POST /user/tokens requires a bootstrap token with token-edit rights.
  Here we reuse the logged-in dashboard session: fetch() is called from the
  dash.cloudflare.com tab with credentials: 'include', so the session cookie
  authorizes the request and no bootstrap token is needed.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import nodriver as uc

# One policy per resource scope (docs: a permission group only applies to the
# resource type it is scoped for; mixing scopes in a single policy => HTTP 400).
POLICY_RESOURCES = {
    "account": {"com.cloudflare.api.account.*": "*"},
    "zone": {"com.cloudflare.api.account.zone.*": "*"},
    "user": {"com.cloudflare.api.user.tag.*": "*"},
}

# API validation enforces 1-300 permission_groups per policy, so oversized
# scope buckets are split across multiple policies with the same resources.
MAX_GROUPS_PER_POLICY = 300

# Workers AI permission group ids (stable, from the original API path) —
# guaranteed fallback scope so accounts always end up Workers AI-capable.
WORKERS_AI_READ_ID = "a92d2450e05d4e7bb7d0a64968f83d11"
WORKERS_AI_WRITE_ID = "bacc64e0f6c34fc0883a1223f938a104"

# Fallback capabilities with no documented-stable id, so each is resolved from
# the live permission-group listing by exact name. Cloudflare renames groups over
# time (e.g. "Workers Scripts: Edit" is now "Workers Scripts Write"/"Read"), so
# each entry may be a tuple of candidate spellings — the first match wins. All of
# them are REQUIRED: a missing match disables the fallback variant and fails the
# run rather than silently downgrading the token.
WORKERS_SCRIPTS_EDIT_NAMES = ("Workers Scripts: Edit", "Workers Scripts Write", "Workers Scripts Read")
ACCOUNT_ANALYTICS_READ_NAME = "Account Analytics: Read"
# Each entry is a name or a tuple of candidate names (first match wins).
REQUIRED_FALLBACK_GROUPS = (WORKERS_SCRIPTS_EDIT_NAMES, ACCOUNT_ANALYTICS_READ_NAME)

# Cloudflare flips the "email verified" flag asynchronously after the link is
# opened; tolerate that propagation when the API reports verify-your-email.
VERIFY_RETRY_INTERVAL = 15
VERIFY_PROPAGATION_MAX_WAIT = 120


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

    def __getitem__(self, key: str):
        """Support dict-style access for backward compat."""
        return getattr(self, key)


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


async def _session_fetch(
    page: uc.Tab,
    url: str,
    method: str = "GET",
    body: str = "",
) -> dict:
    """Fetch a Cloudflare API path from the authenticated dashboard tab.

    Returns {"status": int, "body": str}; status None means the fetch itself failed.
    """
    url_js = json.dumps(url)
    method_js = json.dumps(method)
    body_js = json.dumps(body) if body else "null"
    raw = await page.evaluate(
        f"""
        (async () => {{
            const opts = {{
                method: {method_js},
                credentials: 'include',
                headers: {{
                    'Content-Type': 'application/json',
                    'Accept': 'application/json'
                }}
            }};
            const bodyJson = {body_js};
            if (bodyJson !== null) opts.body = bodyJson;
            try {{
                const r = await fetch({url_js}, opts);
                const text = await r.text();
                return JSON.stringify({{status: r.status, body: text}});
            }} catch (e) {{
                return JSON.stringify({{status: null, body: String(e)}});
            }}
        }})()
        """,
        await_promise=True,
        return_by_value=True,
    )
    return _loads_wrapped_json(raw, default={}) or {}


async def _get_all_permission_groups(page: uc.Tab) -> tuple[list[dict], dict]:
    """Enumerate every selectable user permission group (paginated).

    Returns ([{id, scopes}, ...], error_result). error_result is {} when listing succeeded.
    """
    groups: list[dict] = []
    api_page = 1
    while api_page <= 10:  # hard page cap; ~100 groups per page
        data = await _session_fetch(
            page,
            f"/api/v4/user/tokens/permission_groups?per_page=100&page={api_page}",
        )
        status = data.get("status")
        if status != 200:
            return [], {"status": status, "body": (data.get("body") or "")[:500]}

        try:
            resp = json.loads(data.get("body") or "{}")
        except Exception:
            return [], {"status": status, "body": "permission_groups_invalid_json"}

        for group in resp.get("result") or []:
            gid = group.get("id")
            # Docs: only groups flagged selectable are grantable (account endpoint);
            # the user endpoint may omit the flag — default to include.
            if gid and group.get("is_selectable", True):
                scopes = group.get("scopes")
                if scopes is None:
                    scopes = (group.get("meta") or {}).get("scopes")
                groups.append({"id": gid, "name": group.get("name", ""), "scopes": scopes})

        result_info = resp.get("result_info") or {}
        total_pages = result_info.get("total_pages") or 1
        if api_page >= total_pages:
            break
        api_page += 1

    return groups, {}


def _normalize_group_name(name: str) -> str:
    """Canonical form of a human-readable permission-group label.

    Casing, punctuation (the ": " in "Workers Scripts: Edit") and whitespace
    drift across the listing, so they are normalised away before comparing.
    """
    spaced = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in name)
    return " ".join(spaced.lower().split())


def _find_group_exact(groups: list[dict], expected_name: str) -> dict:
    """First group whose normalised name equals `expected_name` exactly.

    A broader or similarly named group (for example a compound "... Read &
    Edit" label) never stands in for the required one — exactness is what lets
    a miss be treated as a hard failure instead of a silent downgrade.
    """
    target = _normalize_group_name(expected_name)
    if not target:
        return {}
    for group in groups:
        if _normalize_group_name(group.get("name") or "") == target:
            return group
    return {}


def _similar_group_names(groups: list[dict], expected_name: str, limit: int = 5) -> list[str]:
    """Listing names sharing the leading words of `expected_name`.

    Used only to make a required-group miss debuggable at a glance: the log
    shows what Cloudflare actually calls the group in this account's listing.
    """
    words = _normalize_group_name(expected_name).split()
    if not words:
        return []
    probe = " ".join(words[:2])
    near = [
        str(group.get("name") or "")
        for group in groups
        if probe in _normalize_group_name(group.get("name") or "")
    ]
    return near[:limit]


def _scope_buckets(scopes: Any) -> set:
    """Map a permission group's scopes onto policy resource buckets."""
    if isinstance(scopes, str):
        scopes = [scopes]
    buckets = set()
    for scope in scopes or []:
        if scope == "com.cloudflare.api.account.zone":
            buckets.add("zone")
        elif scope.startswith("com.cloudflare.api.account"):
            # account, account.member, etc. — all apply to the account resource
            buckets.add("account")
        elif scope.startswith("com.cloudflare.api.user"):
            buckets.add("user")
    return buckets


def _build_full_access_policies(groups: list[dict]) -> tuple[list[dict], int]:
    """Build one allow-policy per resource bucket from scoped groups.

    Returns (policies, skipped_count). Groups without a recognised scope are
    skipped rather than risk a mixed-scope validation error.
    """
    ids_by_bucket: dict[str, list[str]] = {"account": [], "zone": [], "user": []}
    skipped = 0
    for group in groups:
        buckets = _scope_buckets(group.get("scopes"))
        if not buckets:
            skipped += 1
            continue
        for bucket in buckets:
            ids_by_bucket[bucket].append(group["id"])

    policies = []
    for bucket in ("account", "zone", "user"):
        ids = ids_by_bucket[bucket]
        # Chunk per bucket to respect the 1-300 permission_groups policy limit.
        for i in range(0, len(ids), MAX_GROUPS_PER_POLICY):
            chunk = ids[i : i + MAX_GROUPS_PER_POLICY]
            policies.append(
                {
                    "effect": "allow",
                    "resources": dict(POLICY_RESOURCES[bucket]),
                    "permission_groups": [{"id": gid} for gid in chunk],
                }
            )
    return policies, skipped


async def wait_for_verified_session(
    page: uc.Tab,
    verification_link: str = "",
    account_id: str = "",
    timeout: float = 120,
    interval: float = 8,
) -> bool:
    """Poll the dashboard until the account is no longer flagged as email-unverified.

    There is no public "verified" status endpoint, so we use the same signal the
    removed UI journey used: the dashboard renders a "verify your email" banner
    while the account email is unconfirmed. Re-opening the verification link on
    each round also covers links that need a second attempt to complete.

    Returns True when the banner is gone (verified), False on timeout.
    """
    probe_url = (
        f"https://dash.cloudflare.com/{account_id}/home"
        if account_id
        else "https://dash.cloudflare.com/"
    )
    start = time.time()
    while True:
        await page.get(probe_url)
        await asyncio.sleep(interval)
        raw = await page.evaluate("document.body ? document.body.innerText : ''")
        body = str(_unwrap_nodriver_value(raw, default="")).lower()
        if "verify your email" not in body and "confirm your email" not in body:
            return True
        if time.time() - start >= timeout:
            return False
        print("    ⏳ Account still shows 'verify your email'; reopening link...")
        if verification_link:
            await page.get(verification_link)
            await asyncio.sleep(interval)


async def create_token_api(
    page: uc.Tab,
    token_name: str = "full-access-auto",
    max_retries: int = 3,
) -> TokenResult:
    """Create a full-access API token (cfut_*) via POST /api/v4/user/tokens.

    Enumerates permission groups, then creates a token with one allow-policy
    per resource scope (account / zone / user tag) — groups must be paired
    with their own scoped resource; a mixed-scope policy fails validation
    with HTTP 400 ("These rules must pass for ...").

    If the strict payload is rejected with 400, one fallback attempt is made
    with Workers AI Read/Write + Workers Scripts: Edit + Account Analytics:
    Read (guaranteed capability, like the original API path). Retries on 429
    with exponential backoff.

    Every name in REQUIRED_FALLBACK_GROUPS must exist in the live
    permission-group listing (matched by exact normalised name). If any is
    missing, no fallback variant is offered and a 400 on the full-access
    payload fails the run with "fallback_group_missing:<names>" instead of
    creating a token that lacks a promised capability.
    """
    print("    Listing permission groups for full-access token...")
    groups, list_error = await _get_all_permission_groups(page)
    if list_error:
        snippet = str(list_error.get("body", ""))[:200]
        return TokenResult(
            False,
            token_name=token_name,
            error=f"permission_groups_http_{list_error.get('status')}: {snippet}",
            method="api",
            raw=list_error,
        )
    if not groups:
        return TokenResult(False, token_name=token_name, error="permission_groups_empty", method="api")

    policies, skipped = _build_full_access_policies(groups)
    print(f"    {len(groups)} groups -> {len(policies)} scoped policies ({skipped} unrecognised skipped).")
    if not policies:
        return TokenResult(False, token_name=token_name, error="permission_groups_no_scopes", method="api")

    # Guaranteed fallback scope: Workers AI Read/Write (stable ids) plus every
    # REQUIRED_FALLBACK_GROUPS name, matched exactly in the live listing.
    fallback_account_ids = [WORKERS_AI_READ_ID, WORKERS_AI_WRITE_ID]
    fallback_zone_ids: list[str] = []
    missing_required: list[str] = []

    def _add_required_group(candidate_names: tuple[str, ...] | str) -> None:
        """Resolve a required fallback capability and bucket it by its own scope.

        `candidate_names` may be a single name or a tuple of candidate spellings
        (Cloudflare renames groups over time). The first match wins. A miss is
        recorded rather than downgrading the fallback token with a partial scope.
        """
        names = (candidate_names,) if isinstance(candidate_names, str) else candidate_names
        group = {}
        matched_name = ""
        for name in names:
            group = _find_group_exact(groups, name)
            if group:
                matched_name = name
                break
        if not group:
            missing_required.append(str(names[0]))
            near = _similar_group_names(groups, names[0])
            print(
                f"    ❌ Required group '{names[0]}' not found in listing "
                f"(similar entries: {near or 'none'})"
            )
            return
        buckets = _scope_buckets(group.get("scopes")) or {"account"}
        if "zone" in buckets and "account" not in buckets:
            fallback_zone_ids.append(group["id"])
        else:
            fallback_account_ids.append(group["id"])
        print(f"    Fallback includes '{group.get('name')}' ({group['id']}).")

    for required_name in REQUIRED_FALLBACK_GROUPS:
        _add_required_group(required_name)

    fallback_error = ""
    if missing_required:
        fallback_error = "fallback_group_missing:" + ", ".join(missing_required)
        print("    ❌ Fallback disabled: required permission group(s) unavailable.")

    fallback_policies = [
        {
            "effect": "allow",
            "resources": dict(POLICY_RESOURCES["account"]),
            "permission_groups": [{"id": gid} for gid in fallback_account_ids],
        }
    ]
    if fallback_zone_ids:
        fallback_policies.append(
            {
                "effect": "allow",
                "resources": dict(POLICY_RESOURCES["zone"]),
                "permission_groups": [{"id": gid} for gid in fallback_zone_ids],
            }
        )

    # Variant 1: full access (all groups, chunked under the 300-per-policy cap).
    # Variant 2: the reduced fallback, only when every required group resolved.
    variants = [("full", policies)]
    if not fallback_error:
        variants.append(("fallback", fallback_policies))

    # The verified flag can lag behind the banner check; keep trying while the
    # API still reports verify-your-email instead of failing the whole account.
    waited = 0.0
    while True:
        result = await _post_token_variants(
            page, variants, token_name, max_retries, fallback_error=fallback_error
        )
        if result.error != "email_not_verified":
            return result
        if waited >= VERIFY_PROPAGATION_MAX_WAIT:
            return result
        waited += VERIFY_RETRY_INTERVAL
        print(
            f"    ⏳ API still reports email unverified; retrying token in "
            f"{VERIFY_RETRY_INTERVAL}s (waited {int(waited)}s)..."
        )
        await asyncio.sleep(VERIFY_RETRY_INTERVAL)


async def _post_token_variants(
    page: uc.Tab,
    variants: list,
    token_name: str,
    max_retries: int,
    fallback_error: str = "",
) -> TokenResult:
    """POST each policy variant until a token is created (400 on 'full' -> next).

    fallback_error carries the reason the fallback variant is unavailable (one
    or more required permission groups were missing). When set, a 400 on the
    full-access payload surfaces that reason instead of a generic validation
    failure.
    """
    for variant_name, variant_policies in variants:
        payload = json.dumps({"name": token_name, "condition": {}, "policies": variant_policies})
        print(f"    Creating token via POST /api/v4/user/tokens ({variant_name} policies)...")

        for attempt in range(max_retries):
            if attempt > 0:
                wait = 2 ** attempt  # exponential: 2s, 4s, 8s
                print(f"    Retry {attempt}/{max_retries} after {wait}s (429 rate limit)...")
                await asyncio.sleep(wait)

            data = await _session_fetch(page, "/api/v4/user/tokens", method="POST", body=payload)
            status = data.get("status")
            body_text = data.get("body") or ""

            # Handle 429 rate limit - retry
            if status == 429:
                if attempt < max_retries - 1:
                    continue
                snippet = body_text[:200]
                return TokenResult(
                    False,
                    token_name=token_name,
                    error=f"api_http_429_exhausted: {snippet}",
                    method="api",
                    raw={"status": status, "body": snippet},
                )

            err_messages = ""
            err_resp = None
            try:
                err_resp = json.loads(body_text)
                err_messages = " | ".join(
                    str(e.get("message", e)) for e in (err_resp.get("errors") or [])
                )
            except Exception:
                pass
            lower_messages = err_messages.lower()
            if "verify" in lower_messages and "email" in lower_messages:
                return TokenResult(
                    False,
                    token_name=token_name,
                    error="email_not_verified",
                    method="api",
                    raw={"status": status, "body": err_resp or body_text[:500]},
                )

            if status != 200:
                # Keep validation messages long enough to debug policy rejections.
                snippet = body_text[:2000].replace("\n", " ")
                if status == 400 and variant_name == "full":
                    if fallback_error:
                        # No fallback variant exists — report the missing
                        # required group rather than minting a degraded token.
                        return TokenResult(
                            False,
                            token_name=token_name,
                            error=(
                                f"{fallback_error} "
                                f"(full-access payload also rejected: {snippet[:300]})"
                            ),
                            method="api",
                            raw={"status": status, "body": snippet},
                        )
                    print("    ⚠️ Full-access payload rejected (400); falling back to the reduced scope...")
                    break
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
            return TokenResult(
                False,
                token_name=token_name,
                error=f"api_token_failed: {messages}",
                method="api",
                raw=resp,
            )

    # Fallback if all variants/retries were exhausted without an explicit return
    return TokenResult(False, token_name=token_name, error="api_retry_exhausted", method="api")


async def create_token(
    page: uc.Tab,
    account_id: str = "",
    token_name: str = "full-access-auto",
    timeout: float = 180,
) -> TokenResult:
    """
    Create a full-access Cloudflare API token (cfut_*) directly via the API.

    The dashboard UI journey was removed. Callers should gate on
    wait_for_verified_session() first; create_token_api additionally retries
    while Cloudflare reports the email as unverified (flag propagation).

    account_id / timeout are kept in the signature for caller compatibility;
    a user-owned full-access token does not require account resolution.
    """
    return await create_token_api(page, token_name=token_name)


__all__ = ["TokenResult", "create_token", "create_token_api", "wait_for_verified_session"]

if __name__ == "__main__":
    print("This module is imported by main.py. Run main.py instead.")
