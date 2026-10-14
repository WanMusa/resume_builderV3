"""Deterministic JSON/Markdown renderer; DOCX is deliberately not implemented."""

import json
from pathlib import Path


def render_package(package: dict, output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    resume = package["resume"]
    identity = resume["identity"]
    lines = [f"# {identity['name']}", identity.get("contact", ""), "",
             "## Summary", resume["summary"], "", "## Expertise",
             *[f"- {item}" for item in resume["expertise"]], "", "## Experience"]
    for role in resume["experience"]:
        lines.extend([f"### {role['title']} — {role['employer']}", role["dates"]])
        lines.extend(f"- {a['text']}" for a in resume["achievements"]
                     if a["experience_id"] == role["id"])
    for section in ("education", "certifications"):
        lines.extend(["", f"## {section.title()}", *[f"- {v}" for v in resume.get(section, [])]])
    if package["provenance"]["provider"] == "mock" or package["provenance"]["is_example"]:
        lines.insert(0, "> DEVELOPMENT EXAMPLE — NOT FOR SUBMISSION\n")
    elif package.get("review_required"):
        lines.insert(0, "> GENERATED DRAFT — FACTUAL REVIEW REQUIRED\n")
    letter = "\n\n".join(package["cover_letter"]["paragraphs"])
    banner = lines[0] + "\n\n" if lines[0].startswith(">") else ""
    contents = {
        "package.json": json.dumps(package, ensure_ascii=False, indent=2),
        "resume.md": "\n".join(lines) + "\n",
        "cover_letter.md": banner + letter + "\n",
    }
    paths = []
    for name, content in contents.items():
        path = output_dir / name
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
        paths.append(path)
    return paths
