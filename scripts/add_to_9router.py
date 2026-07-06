#!/usr/bin/env python3
"""Bulk-add exported Cloudflare API keys into local 9Router.

Reads the text format produced by scripts/export_9router_txt.py and automates
9Router's Cloudflare provider form.
"""

from __future__ import annotations

import argparse
import asyncio
import re
from pathlib import Path


ENTRY_RE = re.compile(
    r"Name:\s*(?P<name>.+?)\n"
    r"API Key:\s*(?P<api_key>cfut_[A-Za-z0-9]+)\n"
    r"Account ID:\s*(?P<account_id>[a-f0-9]{32})\n"
    r"Priority:\s*(?P<priority>\d+)\n"
    r"Proxy Pool:\s*(?P<proxy_pool>.+?)(?:\n\n|\Z)",
    re.S | re.I,
)


def read_entries(path: str):
    text = Path(path).read_text()
    return [m.groupdict() for m in ENTRY_RE.finditer(text)]


async def main_async(args):
    from playwright.async_api import async_playwright

    entries = read_entries(args.input)
    if not entries:
        raise SystemExit(f"No entries found in {args.input}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=args.headless, args=["--no-sandbox"])
        page = await browser.new_page(viewport={"width": 1366, "height": 900})

        print(f"Opening 9Router: {args.url}")
        await page.goto(args.url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(1500)

        # Login screen is optional depending on user's 9Router session.
        pw = page.locator('input[type="password"], input[placeholder*="password" i]')
        if await pw.count() > 0:
            await pw.first.fill(args.password)
            login_btn = page.locator('button:has-text("Login"), button[type="submit"]')
            if await login_btn.count() > 0:
                await login_btn.first.click()
                await page.wait_for_timeout(2000)

        # Navigate providers directly; tolerate SPAs that keep same shell.
        await page.goto(args.url.rstrip("/") + "/dashboard/providers", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(1500)
        cloudflare = page.locator('text=Cloudflare').first
        if await cloudflare.count() > 0:
            await cloudflare.click()
            await page.wait_for_timeout(1500)

        ok = 0
        failed = 0
        for i, entry in enumerate(entries, 1):
            print(f"[{i}/{len(entries)}] adding {entry['name']} {entry['account_id'][:10]}...")
            try:
                add_btn = page.locator('button:has-text("Add"), button:has-text("Add API Key")').first
                await add_btn.click(timeout=10000)
                await page.wait_for_timeout(800)

                # Single tab if present.
                single = page.locator('button:has-text("Single"), text=Single').first
                if await single.count() > 0:
                    try:
                        await single.click(timeout=2000)
                    except Exception:
                        pass

                name_in = page.locator('input[placeholder="Production Key"], input[name="name"]').first
                await name_in.fill(entry["name"])

                api_in = page.locator('input[type="password"], input[placeholder*="API" i]').first
                await api_in.fill(entry["api_key"])

                acc_in = page.locator('input[placeholder="abc123def456..."], input[name*="account" i]').first
                await acc_in.fill(entry["account_id"])

                prio = page.locator('input[name*="priority" i], label:has-text("Priority") + input').first
                if await prio.count() > 0:
                    await prio.fill(str(entry.get("priority") or args.priority))

                save = page.locator('button:has-text("Save"), button:has-text("Add Key"), button:has-text("Add")').last
                await save.click(timeout=10000)
                await page.wait_for_timeout(1500)
                ok += 1
            except Exception as exc:
                failed += 1
                print(f"  FAIL: {exc}")
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass

        print(f"Done. success={ok} failed={failed}")
        await browser.close()


def main():
    ap = argparse.ArgumentParser(description="Bulk-add Cloudflare keys to local 9Router")
    ap.add_argument("--input", "-i", default="9router-cloudflare-keys.txt")
    ap.add_argument("--url", default="http://localhost:20128")
    ap.add_argument("--password", default="123456")
    ap.add_argument("--priority", default="1")
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
