"""Merge deployment secrets into the private VPS runtime file without logging them."""

import os
import re
import stat
import tempfile
from pathlib import Path


SECRET_KEYS = ("DATABASE_PASSWORD", "OPENAI_API_KEY", "API_TOKEN", "JOB_RUNS_API_TOKEN",
               "SUPABASE_URL", "SUPABASE_KEY", "SUPABASE_BUCKET")


def configure(directory: Path, environment: dict[str, str]) -> None:
    target = directory / ".env"
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise ValueError("Runtime .env must be a regular file")
    if not target.exists() and not all(environment.get(f"DEPLOY_{key}") for key in
                                       ("DATABASE_PASSWORD", "API_TOKEN")):
        raise ValueError("First deployment requires DATABASE_PASSWORD and API_TOKEN secrets")
    content = (target if target.exists() else directory / ".env.example").read_text()
    updates = {key: environment[f"DEPLOY_{key}"] for key in SECRET_KEYS
               if environment.get(f"DEPLOY_{key}")}
    if environment.get("DEPLOY_SUPABASE_SECRET_KEY") and "SUPABASE_KEY" not in updates:
        updates["SUPABASE_KEY"] = environment["DEPLOY_SUPABASE_SECRET_KEY"]
    updates["STORAGE_BACKEND"] = "supabase"
    for key, value in updates.items():
        if any(character in value for character in ("\n", "\r", "\0")):
            raise ValueError(f"{key} must be a single-line value")
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$")
        assignment = f'{key}="{escaped}"'
        pattern = rf"^[ \t]*(?:export[ \t]+)?{key}[ \t]*=.*$"
        if re.search(pattern, content, flags=re.MULTILINE):
            content = re.sub(pattern, lambda match: assignment, content, flags=re.MULTILINE)
        else:
            content = content.rstrip("\n") + "\n" + assignment + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=".env.", dir=directory)
    try:
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
        Path(temporary).replace(target)
    finally:
        Path(temporary).unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        configure(Path.cwd(), dict(os.environ))
    except (OSError, ValueError):
        raise SystemExit("Runtime settings update failed; check secret names and .env permissions.") from None
