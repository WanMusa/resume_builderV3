"""Private storage adapters. Persist object keys, never expiring signed URLs."""

from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlsplit, urlunsplit

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

    def signed_download_url(self, key: str, expires_in: int = 300,
                            download: bool = False) -> str:
        if (not (key.endswith("-Resume.docx") or key.endswith("-Cover Letter.docx"))
                or any(c in key for c in '/\\:*?"<>|')
                or any(ord(c) < 32 for c in key)):
            raise ValueError("Artifact must have a safe root-level Word filename")
        try:
            self.check_bucket()
        except httpx.HTTPError:
            raise RuntimeError("Storage private-bucket check failed") from None
        settings = self.settings
        storage_root = f"{settings.supabase_url.rstrip('/')}/storage/v1"
        url = (f"{storage_root}/object/sign/"
               f"{quote(settings.supabase_bucket, safe='')}/{quote(key, safe='')}")
        try:
            with httpx.Client(timeout=20) as client:
                response = client.post(url, json={"expiresIn": expires_in}, headers={
                    "Authorization": "Bearer " + settings.supabase_key,
                    "apikey": settings.supabase_key,
                })
        except httpx.HTTPError:
            raise RuntimeError("Storage signed-link request failed") from None
        if not response.is_success:
            raise RuntimeError(f"Storage signed-link request returned HTTP {response.status_code}")
        signed_url = response.json().get("signedURL")
        if not isinstance(signed_url, str) or not signed_url:
            raise RuntimeError("Storage provider returned no signed link")
        provider_url = urlparse(signed_url)
        configured = urlparse(settings.supabase_url)
        if provider_url.scheme or provider_url.netloc:
            if provider_url.scheme != "https" or provider_url.netloc != configured.netloc:
                raise RuntimeError("Storage provider returned a signed link for an unexpected origin")
            result = signed_url
        else:
            relative = signed_url.lstrip("/")
            result = (f"{settings.supabase_url.rstrip('/')}/{relative}"
                      if relative.startswith("storage/v1/")
                      else f"{storage_root}/{relative}")
        parsed = urlparse(result)
        if parsed.scheme != "https" or parsed.netloc != configured.netloc:
            raise RuntimeError("Storage provider returned a signed link for an unexpected origin")
        if download:
            parts = urlsplit(result)
            query = parse_qsl(parts.query, keep_blank_values=True)
            query.append(("download", key))
            result = urlunsplit(parts._replace(query=urlencode(query)))
        return result


def get_storage(settings):
    if settings.storage_backend != "supabase":
        raise ValueError("Final documents require Supabase storage; local output is disabled")
    return SupabaseStorage(settings)
