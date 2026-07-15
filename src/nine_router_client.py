import httpx
from typing import Any


class NineRouterClient:
    BASE_URL = "https://my-9router-or-omniroute.com/api"

    def __init__(self):
        self.client = httpx.Client(timeout=30.0)
        self.token: str | None = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.client.close()
        return False

    def login(self, password: str) -> str:
        response = self.client.post(
            f"{self.BASE_URL}/auth/login",
            json={"password": password}
        )
        response.raise_for_status()

        cookie_header = response.headers.get("set-cookie", "")
        for part in cookie_header.split(";"):
            if part.strip().startswith("auth_token="):
                token = part.split("=", 1)[1]
                self.token = token
                return token

        raise ValueError("auth_token not found in Set-Cookie header")

    def bulk_deploy(self, token: str, entries: list[dict]) -> dict[str, Any]:
        response = self.client.post(
            f"{self.BASE_URL}/providers/bulk",
            headers={
                "Cookie": f"auth_token={token}",
                "Content-Type": "application/json"
            },
            json={
                "provider": "cloudflare-ai",
                "entries": entries,
                "validateKeys": True
            }
        )
        response.raise_for_status()
        return response.json()
