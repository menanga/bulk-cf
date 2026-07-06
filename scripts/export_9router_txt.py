#!/usr/bin/env python3
"""Export valid Cloudflare Workers AI keys from results.json for 9Router bulk import."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def iter_valid(results):
    for item in results:
        token = item.get("api_token", "")
        account_id = item.get("account_id", "")
        if token.startswith("cfut_") and account_id and item.get("token_valid"):
            yield item


def main():
    ap = argparse.ArgumentParser(description="Export valid Cloudflare API keys to 9Router-friendly txt")
    ap.add_argument("--input", "-i", default="results.json")
    ap.add_argument("--output", "-o", default="9router-cloudflare-keys.txt")
    ap.add_argument("--proxy-pool", default="None")
    ap.add_argument("--priority", default="1")
    args = ap.parse_args()

    in_path = Path(args.input)
    if not in_path.exists():
        raise SystemExit(f"Input not found: {in_path}")

    data = json.loads(in_path.read_text())
    lines = ["Cloudflare Workers AI keys for 9Router", ""]
    count = 0
    for count, item in enumerate(iter_valid(data), start=1):
        lines.extend([
            f"[{count}]",
            f"Name: {item.get('email', f'cloudflare-key-{count}')}",
            f"API Key: {item['api_token']}",
            f"Account ID: {item['account_id']}",
            f"Priority: {args.priority}",
            f"Proxy Pool: {args.proxy_pool}",
            "",
        ])

    lines.extend(["--- Summary ---", f"Valid keys: {count}"])
    Path(args.output).write_text("\n".join(lines) + "\n")
    print(f"Exported {count} valid keys to {args.output}")


if __name__ == "__main__":
    main()
