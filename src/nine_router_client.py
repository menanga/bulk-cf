import socket
import httpx
from typing import Any


class NineRouterClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(timeout=30.0)
        self.token: str | None = None

        # Pre-flight DNS check
        netloc = self.base_url.split("//")[-1].split("/")[0]
        try:
            socket.getaddrinfo(netloc, 443, proto=socket.IPPROTO_TCP)
        except socket.gaierror as e:
            raise ConnectionError(f"Cannot resolve 9router host: {netloc}") from e

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.client.close()
        return False

    def login(self, password: str) -> str:
        response = self.client.post(
            f"{self.base_url}/auth/login",
            json={"password": password}
        )
        response.raise_for_status()

        cookie_header = response.headers.get("set-cookie", "")
        for part in cookie_header.split(";"):
            if part.strip().startswith(("auth_token=", "9router=")):
                token = part.split("=", 1)[1]
                self.token = token
                return token

        raise ValueError("auth_token not found in Set-Cookie header")

    def deploy_provider(self, token: str, entry: dict) -> dict[str, Any]:
        """Push a single Cloudflare AI credential to /api/providers."""
        payload = {
            "provider": "cloudflare-ai",
            "apiKey": entry["apiKey"],
            "name": entry.get("email") or entry.get("name"),
            "priority": 1,
            "testStatus": "active",
            "providerSpecificData": {
                "accountId": entry["accountId"]
            }
        }

        response = self.client.post(
            f"{self.base_url}/providers",
            headers={
                "Cookie": f"auth_token={token};9router={token}",
                "Content-Type": "application/json"
            },
            json=payload
        )
        response.raise_for_status()

        try:
            return response.json()
        except ValueError:
            return {"success": True}
