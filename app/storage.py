"""Private storage adapters. Persist object keys, never expiring signed URLs."""

from urllib.parse import quote

import httpx


DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class SupabaseStorage:
    def __init__(self, settings):
        if not all((settings.supabase_url, settings.supabase_key, settings.supabase_bucket)):
            raise ValueError("Supabase URL, key and private bucket must be configured")
        if not settings.supabase_url.startswith("https://"):
            raise ValueError("Supabase requires HTTPS")
        self.settings = settings

    def check_bucket(self) -> None:
        settings = self.settings
        with httpx.Client(timeout=30) as client:
            response = client.get(
                f"{settings.supabase_url.rstrip('/')}/storage/v1/bucket/"
                f"{quote(settings.supabase_bucket, safe='')}",
                headers={"Authorization": "Bearer " + settings.supabase_key,
                         "apikey": settings.supabase_key},
            )
        if not response.is_success:
            raise RuntimeError(f"Storage bucket check returned HTTP {response.status_code}")
        if response.json().get("public") is not False:
            raise ValueError("Supabase output bucket must be private")

    def upload(self, content: bytes, key: str) -> dict:
        if (not key.endswith(".docx") or any(c in key for c in '/\\:*?"<>|')
                or any(ord(c) < 32 for c in key)):
            raise ValueError("Output must have a safe Word document filename at the bucket root")
        settings = self.settings
        url = (f"{settings.supabase_url.rstrip('/')}/storage/v1/object/"
               f"{quote(settings.supabase_bucket, safe='')}/{quote(key, safe='/')}")
        with httpx.Client(timeout=60) as client:
            response = client.post(url, content=content, headers={
                "Authorization": "Bearer" + " " + settings.supabase_key,
                "apikey": settings.supabase_key,
                "x-upsert": "true",
                "Content-Type": DOCX_CONTENT_TYPE,
            })
        if not response.is_success:
            raise RuntimeError(f"Storage provider returned HTTP {response.status_code}")
        return {"backend": "supabase", "bucket": settings.supabase_bucket,
                "object_key": key, "local_path": None}


def get_storage(settings):
    if settings.storage_backend != "supabase":
        raise ValueError("Final documents require Supabase storage; local output is disabled")
    return SupabaseStorage(settings)
