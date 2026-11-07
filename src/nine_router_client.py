import os
import socket
import httpx
from typing import Any


def _env_bool(key: str, default: bool = True) -> bool:
    """Parse a boolean env var (true/1/yes = on)."""
    return os.getenv(key, str(default).lower()).strip().lower() in ("true", "1", "yes")


class NineRouterClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(timeout=30.0)
        self.token: str | None = None
        # Toggle tag_provider() on/off after a successful deploy (default: on).
        self.tag_provider_enabled: bool = _env_bool("NINE_ROUTER_TAG_PROVIDER", True)

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

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Cookie": f"auth_token={token};9router={token}",
            "Content-Type": "application/json"
        }

    def deploy_provider(self, token: str, entry: dict) -> dict[str, Any]:
        """Push a single Cloudflare AI credential to /api/providers."""
        payload = {
            "provider": "cloudflare-ai",
            "apiKey": entry["apiKey"],
            "name": entry.get("email") or entry.get("name"),
            "testStatus": "active",
            "providerSpecificData": {
                "accountId": entry["accountId"]
            }
        }

        response = self.client.post(
            f"{self.base_url}/providers",
            headers=self._headers(token),
            json=payload
        )
        response.raise_for_status()

        try:
            return response.json()
        except ValueError:
            return {}

    def tag_provider(self, token: str, provider_id: str, tag: str) -> dict[str, Any]:
        """Tag an existing provider connection with the email domain."""
        if not self.tag_provider_enabled:
            return {}
        response = self.client.put(
            f"{self.base_url}/providers/{provider_id}",
            headers=self._headers(token),
            json={
                "providerSpecificData": {
                    "tag": tag
                }
            }
        )
        response.raise_for_status()

        try:
            return response.json()
        except ValueError:
            return {}

    @staticmethod
    def extract_provider_id(data: dict[str, Any]) -> str | None:
        """Read the connection id from a POST /api/providers response."""
        connection = data.get("connection") or {}
        return connection.get("id") or data.get("id")

    @staticmethod
    def domain_from_email(email: str | None) -> str | None:
        """Extract the domain part of an address (account@gmail.com -> gmail.com)."""
        if not email or "@" not in email:
            return None
        return email.rsplit("@", 1)[1].strip().lower()
