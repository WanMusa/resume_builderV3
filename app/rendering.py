"""In-memory Word rendering using V2 templates and Python-controlled formatting."""

from datetime import date
from io import BytesIO
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

FONT = "Lato"
BLUE = RGBColor(0x36, 0x5F, 0x91)


def _document(template: Path):
    doc = Document(str(template))
    for child in list(doc.element.body):
        if child.tag != qn("w:sectPr"):
            doc.element.body.remove(child)
    return doc


def _paragraph(doc, text: str = "", *, bold: bool = False, size: int = 10,
               font: str = FONT, after: int = 4, before: int = 0, justify: bool = False):
    paragraph = doc.add_paragraph()
    run = paragraph.add_run(text)
    run.bold = bold
    run.font.name = font
    run.font.size = Pt(size)
    paragraph.paragraph_format.space_before = Pt(before)
    paragraph.paragraph_format.space_after = Pt(after)
    paragraph.paragraph_format.line_spacing = 1.0
    if justify:
        paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    return paragraph


def _section(doc, title: str):
    paragraph = _paragraph(doc, title, bold=True, size=12, before=14, after=2)
    paragraph.runs[0].font.color.rgb = BLUE
    border = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    for name, value in (("val", "single"), ("sz", "6"), ("space", "0"), ("color", "365F91")):
        bottom.set(qn(f"w:{name}"), value)
    border.append(bottom)
    paragraph._p.get_or_add_pPr().append(border)


def _bullet(doc, text: str):
    paragraph = _paragraph(doc, "\u2022  " + text, justify=True)
    paragraph.paragraph_format.left_indent = Cm(0.35)
    paragraph.paragraph_format.first_line_indent = Cm(-0.35)


def _hyperlink(paragraph, label: str, url: str, font: str, size: int):
    relation = paragraph.part.relate_to(
        url, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
        is_external=True,
    )
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), relation)
    run = OxmlElement("w:r")
    properties = OxmlElement("w:rPr")
    for tag, values in (
        ("rFonts", {"ascii": font, "hAnsi": font}),
        ("sz", {"val": str(size * 2)}),
        ("color", {"val": "0563C1"}),
        ("u", {"val": "single"}),
    ):
        element = OxmlElement(f"w:{tag}")
        for name, value in values.items():
            element.set(qn(f"w:{name}"), value)
        properties.append(element)
    run.append(properties)
    text = OxmlElement("w:t")
    text.text = label
    run.append(text)
    link.append(run)
    paragraph._p.append(link)


def _identity(doc, identity: dict, *, cover: bool = False):
    font, size = ("Aptos", 11) if cover else (FONT, 10)
    name = _paragraph(doc, identity["name"], bold=True, size=11 if cover else 18, font=font)
    if not cover:
        name.runs[0].font.color.rgb = BLUE
    contact = _paragraph(doc, font=font, size=size, after=12 if cover else 6)
    parts = []
    for key in ("email", "mobile", "location", "linkedin"):
        value = identity.get(key)
        if value:
            if parts:
                run = contact.add_run(" | ")
                run.font.name, run.font.size = font, Pt(size)
            if key in ("email", "linkedin"):
                _hyperlink(contact, value, f"mailto:{value}" if key == "email" else value, font, size)
            else:
                run = contact.add_run(value)
                run.font.name, run.font.size = font, Pt(size)
            parts.append(value)
    if not parts and identity.get("contact"):
        run = contact.add_run(identity["contact"])
        run.font.name, run.font.size = font, Pt(size)


def _fixed_value(doc, value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key != "id":
                _paragraph(doc, key.replace("_", " ").title(), bold=True)
                _fixed_value(doc, child)
    elif isinstance(value, list):
        for child in value:
            _fixed_value(doc, child)
    elif value is not None:
        _paragraph(doc, str(value), justify=True)


def _banner(doc, package):
    if package["provenance"]["provider"] == "mock" or package["provenance"]["is_example"]:
        _paragraph(doc, "DEVELOPMENT EXAMPLE - NOT FOR SUBMISSION", bold=True)
    elif package.get("review_required"):
        _paragraph(doc, "GENERATED DRAFT - FACTUAL REVIEW REQUIRED", bold=True)


def _bytes(doc) -> bytes:
    buffer = BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def render_package(package: dict, assets_dir: Path, job: dict | None = None) -> dict[str, bytes]:
    resume = package["resume"]
    doc = _document(assets_dir / "templates" / "resume_template.docx")
    _banner(doc, package)
    _identity(doc, resume["identity"])
    _section(doc, "CAREER PROFILE")
    _paragraph(doc, resume["summary"], justify=True)
    if resume.get("career_profile") and resume["career_profile"] != resume["summary"]:
        _paragraph(doc, resume["career_profile"], justify=True)
    _section(doc, "AREAS OF EXPERTISE/HIGHLIGHTS")
    for item in resume["expertise"]:
        _bullet(doc, item)
    _section(doc, "PROFESSIONAL EXPERIENCE")
    for role in resume["experience"]:
        paragraph = _paragraph(doc, f"{role['title']}  \u2015  {role['employer']}",
                               bold=True, size=11, before=10, after=2)
        section = doc.sections[-1]
        paragraph.paragraph_format.tab_stops.add_tab_stop(
            section.page_width - section.left_margin - section.right_margin,
            WD_TAB_ALIGNMENT.RIGHT,
        )
        run = paragraph.add_run("\t" + role["dates"])
        run.bold, run.font.name, run.font.size = True, FONT, Pt(11)
        for key, value in role.items():
            if key not in ("id", "title", "employer", "dates"):
                _fixed_value(doc, value)
        achievements = [item for item in resume["achievements"] if item["experience_id"] == role["id"]]
        if achievements:
            _paragraph(doc, "Key Achievements", bold=True)
        for achievement in achievements:
            _bullet(doc, achievement["text"])
            for key, value in achievement.items():
                if key not in ("id", "experience_id", "text", "critical"):
                    _fixed_value(doc, value)
    for key, title in (("education", "EDUCATION"), ("certifications", "CERTIFICATIONS")):
        _section(doc, title)
        _fixed_value(doc, resume.get(key, []))
    handled = {"identity", "summary", "career_profile", "expertise", "experience",
               "achievements", "education", "certifications"}
    for key, value in resume.items():
        if key not in handled and value:
            _section(doc, key.replace("_", " ").upper())
            _fixed_value(doc, value)

    letter = _document(assets_dir / "templates" / "cover_letter_template.docx")
    _banner(letter, package)
    _identity(letter, resume["identity"], cover=True)
    _paragraph(letter, date.today().strftime("%d %B %Y").lstrip("0"), font="Aptos", size=11)
    if job and job.get("company"):
        _paragraph(letter, job["company"], font="Aptos", size=11)
    _paragraph(letter, "Dear Hiring Manager,", font="Aptos", size=11, after=10)
    for text in package["cover_letter"]["paragraphs"]:
        _paragraph(letter, text, font="Aptos", size=11, after=10, justify=True)
    _paragraph(letter, "Kind regards,", font="Aptos", size=11)
    _paragraph(letter, resume["identity"]["name"], bold=True, font="Aptos", size=11)
    return {
        "resume.docx": _bytes(doc),
        "cover_letter.docx": _bytes(letter),
    }
