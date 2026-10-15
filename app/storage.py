"""Private storage adapters. Persist object keys, never expiring signed URLs."""

import shutil
from pathlib import Path
from urllib.parse import quote

import httpx


class LocalStorage:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def upload(self, path: Path, key: str) -> dict:
        target = (self.root / key).resolve()
        if not target.is_relative_to(self.root) or target == self.root:
            raise ValueError("Unsafe artifact object key")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.parent.chmod(0o700)
        shutil.copyfile(path, target)
        target.chmod(0o600)
        return {"backend": "local", "bucket": None, "object_key": key,
                "local_path": str(target)}


class SupabaseStorage:
    def __init__(self, settings):
        if not all((settings.supabase_url, settings.supabase_key, settings.supabase_bucket)):
            raise ValueError("Supabase URL, key and private bucket must be configured")
        if not settings.supabase_url.startswith("https://"):
            raise ValueError("Supabase requires HTTPS")
        self.settings = settings

    def upload(self, path: Path, key: str) -> dict:
        settings = self.settings
        url = (f"{settings.supabase_url.rstrip('/')}/storage/v1/object/"
               f"{quote(settings.supabase_bucket, safe='')}/{quote(key, safe='/')}")
        with httpx.Client(timeout=60) as client:
            response = client.post(url, content=path.read_bytes(), headers={
                "Authorization": "Bearer" + " " + settings.supabase_key,
                "apikey": settings.supabase_key,
                "x-upsert": "true",
                "Content-Type": "application/json" if path.suffix == ".json" else "text/markdown",
            })
        if not response.is_success:
            raise RuntimeError(f"Storage provider returned HTTP {response.status_code}")
        return {"backend": "supabase", "bucket": settings.supabase_bucket,
                "object_key": key, "local_path": None}


def get_storage(settings):
    if settings.storage_backend == "supabase":
        return SupabaseStorage(settings)
    return LocalStorage(settings.data_dir / "storage")
