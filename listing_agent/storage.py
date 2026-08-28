from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

import httpx


class SupabaseStorage:
    """Minimal private-bucket client; service credentials must remain server-side."""

    def __init__(self, project_url: str | None = None, service_key: str | None = None,
                 timeout: float = 60.0):
        project_url = project_url or os.environ.get("SUPABASE_URL")
        service_key = service_key or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        if not project_url or not service_key:
            raise RuntimeError("Missing required environment variables: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY")
        self.base_url = project_url.rstrip("/") + "/storage/v1"
        self.headers = {"Authorization": f"Bearer {service_key}", "apikey": service_key}
        self.timeout = timeout

    def upload(self, bucket: str, path: str, file_path: str | Path, content_type: str) -> None:
        encoded_bucket = quote(bucket, safe="")
        encoded_path = quote(path, safe="/")
        with Path(file_path).open("rb") as handle:
            response = httpx.post(
                f"{self.base_url}/object/{encoded_bucket}/{encoded_path}",
                headers={**self.headers, "Content-Type": content_type, "x-upsert": "true"},
                content=handle,
                timeout=self.timeout,
            )
        response.raise_for_status()

    def signed_url(self, bucket: str, path: str, expires_in: int = 3600) -> str:
        encoded_bucket = quote(bucket, safe="")
        encoded_path = quote(path, safe="/")
        response = httpx.post(
            f"{self.base_url}/object/sign/{encoded_bucket}/{encoded_path}",
            headers={**self.headers, "Content-Type": "application/json"},
            json={"expiresIn": expires_in},
            timeout=self.timeout,
        )
        response.raise_for_status()
        signed = response.json().get("signedURL")
        if not signed:
            raise RuntimeError("Supabase Storage returned no signed URL")
        if signed.startswith("http"):
            return signed
        if signed.startswith("/storage/v1"):
            return self.base_url.rsplit("/storage/v1", 1)[0] + signed
        return self.base_url + signed
