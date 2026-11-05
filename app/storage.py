"""Private storage adapters. Persist object keys, never expiring signed URLs."""

from urllib.parse import quote, urlencode, urlparse

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
        if (
            not key.endswith(".docx")
            or not (
                key.endswith(("-Resume.docx", "-Cover Letter.docx"))
                or "-Resume-" in key
                or "-Cover Letter-" in key
            )
            or any(c in key for c in '/\\:*?"<>|')
            or any(ord(c) < 32 for c in key)
        ):
            raise ValueError("Artifact must have a safe root-level Word filename")
        try:
            self.check_bucket()
        except httpx.HTTPError:
            raise RuntimeError("Storage private-bucket check failed") from None
        settings = self.settings
        storage_root = f"{settings.supabase_url.rstrip('/')}/storage/v1"
        # Sign the exact object key in JSON. The single-object endpoint derives
        # its signed claim from the request URI, whose reserved-character
        # decoding can differ from the decoded object name used for downloads.
        url = f"{storage_root}/object/sign/{quote(settings.supabase_bucket, safe='')}"
        try:
            with httpx.Client(timeout=20) as client:
                response = client.post(url, json={"expiresIn": expires_in, "paths": [key]}, headers={
                    "Authorization": "Bearer " + settings.supabase_key,
                    "apikey": settings.supabase_key,
                })
        except httpx.HTTPError:
            raise RuntimeError("Storage signed-link request failed") from None
        if not response.is_success:
            raise RuntimeError(f"Storage signed-link request returned HTTP {response.status_code}")
        payload = response.json()
        if (not isinstance(payload, list) or len(payload) != 1
                or not isinstance(payload[0], dict) or payload[0].get("error")
                or payload[0].get("path") != key):
            raise RuntimeError("Storage provider returned no signed link for the requested object")
        signed_url = payload[0].get("signedURL")
        if not isinstance(signed_url, str) or not signed_url:
            raise RuntimeError("Storage provider returned no signed link")
        # Supabase may return an unescaped object name, including '#' or '%'.
        # Separate the token before URL parsing so filename characters cannot
        # become fragments, query delimiters, or another layer of escaping.
        provider_path, separator, token = signed_url.rpartition("?token=")
        if not separator or not token or any(c not in
                "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_." for c in token):
            raise RuntimeError("Storage provider returned an invalid signed link")
        provider_url = urlparse(provider_path)
        configured = urlparse(settings.supabase_url)
        if provider_url.scheme or provider_url.netloc:
            if provider_url.scheme != "https" or provider_url.netloc != configured.netloc:
                raise RuntimeError("Storage provider returned a signed link for an unexpected origin")
        # Construct the path from the authoritative key, encoding each segment
        # exactly once. Keep the provider-issued token unchanged.
        result = (f"{storage_root}/object/sign/{quote(settings.supabase_bucket, safe='')}/"
                  f"{quote(key, safe='')}?token={token}")
        if download:
            result += "&" + urlencode({"download": key})
        return result


def get_storage(settings):
    if settings.storage_backend != "supabase":
        raise ValueError("Final documents require Supabase storage; local output is disabled")
    return SupabaseStorage(settings)
