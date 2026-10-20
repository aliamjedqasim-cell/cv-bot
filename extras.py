"""Gifts for the buyer and the pre-payment teaser:
- an ATS version of the CV (one column, plain text, no photo or graphics: what online screening systems read best)
- an English cover letter (written by the AI from the applicant's own facts)
- a blurred preview image of the CV, shown before payment
Everything reuses cv_pdf so the facts are exactly the same as in the designed CV."""
import time
from io import BytesIO

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import HRFlowable, KeepTogether, Paragraph, SimpleDocTemplate, Spacer

import cv_pdf
from cv_pdf import (FONT_DIR, _duration, _sentence, additional_info, build_profile, core_skills, edu_status_text,
                    esc, format_phone, job_duties, nice_name, title_case)

INK = HexColor("#111214")
GREY = HexColor("#3A3D42")


def _st(name, font, size, lead, color=INK, **kw):
    return ParagraphStyle(name, fontName=font, fontSize=size, leading=lead, textColor=color, **kw)


S = {
    "name": _st("name", "Body-Bold", 22, 26),
    "title": _st("title", "Body", 13, 17, GREY),
    "contact": _st("contact", "Body", 10.5, 14, GREY),
    "h": _st("h", "Body-Bold", 12.5, 16, spaceBefore=4),
    "body": _st("body", "Body", 11, 14.5),
    "job": _st("job", "Body-Bold", 11.5, 15),
    "meta": _st("meta", "Body", 10.5, 14, GREY),
    "bullet": _st("bullet", "Body", 11, 14.5, leftIndent=12, bulletIndent=1),
}


def _contact_line(d):
    parts = [format_phone(d.get("phone")), d.get("email"), (d['city'] if 'iraq' in d['city'].lower() else f"{d['city']}, Iraq") if d.get('city') else ""]
    return "  |  ".join(esc(p) for p in parts if p)


def _section(title):
    return [Spacer(1, 6), Paragraph(esc(title.upper()), S["h"]),
            HRFlowable(width="100%", thickness=0.6, color=INK, spaceBefore=1, spaceAfter=4)]


def _doc(buf, d, kind):
    return SimpleDocTemplate(buf, pagesize=A4, leftMargin=54, rightMargin=54, topMargin=46, bottomMargin=40,
                             title=f"{kind} - {nice_name(d.get('full_name'))}", author=nice_name(d.get("full_name")))


def build_ats_cv(d):
    """One column, standard headings, real text: easy for ATS software and for printing."""
    cv_pdf._register_fonts()
    out = [Paragraph(esc(nice_name(d.get("full_name"))), S["name"]),
           Paragraph(esc(title_case(d.get("job_title") or "")), S["title"]), Spacer(1, 2),
           Paragraph(_contact_line(d), S["contact"])]
    out += _section("Professional Summary") + [Paragraph(esc(build_profile(d)), S["body"])]
    exps = [e for e in d.get("experiences") or [] if e.get("company")]
    if d.get("has_experience") and exps:
        out += _section("Professional Experience")
        target = 4 if len(exps) == 1 else 3
        for e in exps:
            head = " - ".join(x for x in (title_case(e.get("title") or ""), e["company"]) if x)
            meta = " | ".join(x for x in (e.get("location"), _duration(e.get("duration"))) if x)
            block = [Paragraph(esc(head), S["job"])]
            if meta:
                block.append(Paragraph(esc(meta), S["meta"]))
            block += [Paragraph(esc(x), S["bullet"], bulletText="•") for x in job_duties(d, e, target)]
            out.append(KeepTogether(block + [Spacer(1, 4)]))
        reach = [(label, value) for label, value in (
            ("Customers Covered", ", ".join(d.get("customers") or [])), ("Products / Specialties", d.get("products")),
            ("Territory Covered", d.get("territory"))) if value]
        out += [Paragraph(f"<b>{label}:</b> {esc(value)}", S["body"]) for label, value in reach]
    achievements = [_sentence(x) for x in d.get("achievements") or [] if _sentence(x)]
    if achievements:
        out += _section("Key Achievements") + [Paragraph(esc(x), S["bullet"], bulletText="•") for x in achievements]
    out += _section("Core Skills") + [Paragraph(esc(", ".join(core_skills(d))), S["body"])]
    edu = " - ".join(str(x).strip() for x in [d.get("degree"), d.get("university"), edu_status_text(d)] if x)
    if edu:
        out += _section("Education") + [Paragraph(esc(edu), S["body"])]
    courses = [c for c in d.get("courses") or [] if c]
    if courses:
        out += _section("Courses & Certifications") + [Paragraph(esc(c), S["bullet"], bulletText="•") for c in courses]
    info = additional_info(d)
    if info:
        out += _section("Additional Information") + [Paragraph(esc(x), S["bullet"], bulletText="•") for x in info]
    langs = [x for x in d.get("languages") or [] if x]
    if langs:
        out += _section("Languages") + [Paragraph(esc(", ".join(langs)), S["body"])]
    buf = BytesIO()
    _doc(buf, d, "CV (ATS)").build(out)
    return buf.getvalue()


def fallback_letter(d):
    """Used only when the AI is not available: built from the applicant's facts."""
    title = title_case(d.get("job_title") or "Sales Representative")
    exps = [e for e in d.get("experiences") or [] if e.get("company")]
    first = (f"I am writing to apply for the {title} position. "
             + (f"My experience as {title_case(exps[0].get('title') or 'a team member')} at {exps[0]['company']} "
                "has given me practical, hands-on knowledge of the field and of working with customers and colleagues."
                if exps else
                f"As a {d.get('degree') or 'motivated graduate'}, I bring a solid foundation, strong communication "
                "skills and a genuine interest in building my career in this field."))
    skills = core_skills(d)[:4]
    second = (f"My strengths include {', '.join(skills[:-1]).lower()} and {skills[-1].lower()}. " if len(skills) > 1 else "") + \
             "I learn quickly, plan my work carefully and take full ownership of my targets and responsibilities."
    third = ("I would welcome the opportunity to discuss how I can contribute to your team. "
             "Thank you for your time and consideration.")
    return "\n\n".join([first, second, third])


def build_cover_letter(d, body_text=None):
    cv_pdf._register_fonts()
    name = nice_name(d.get("full_name"))
    body = (body_text or "").strip() or fallback_letter(d)
    out = [Paragraph(esc(name), S["name"]), Paragraph(esc(title_case(d.get("job_title") or "")), S["title"]),
           Spacer(1, 2), Paragraph(_contact_line(d), S["contact"]),
           HRFlowable(width="100%", thickness=0.6, color=INK, spaceBefore=8, spaceAfter=16),
           Paragraph(time.strftime("%d %B %Y"), S["body"]), Spacer(1, 14),
           Paragraph("Dear Hiring Manager,", S["body"]), Spacer(1, 10)]
    for para in [p.strip() for p in body.split("\n") if p.strip()]:
        out += [Paragraph(esc(para), _st("p", "Body", 11.5, 16, alignment=4)), Spacer(1, 9)]
    out += [Spacer(1, 6), Paragraph("Sincerely,", S["body"]), Spacer(1, 4), Paragraph(esc(name), S["job"])]
    buf = BytesIO()
    _doc(buf, d, "Cover Letter").build(out)
    return buf.getvalue()


def preview_image(pdf_bytes):
    """First page of the CV as an image: the top stays sharp (they see it is theirs and looks professional),
    the rest is blurred with a PREVIEW mark. Returns JPEG bytes, or None if no renderer is available."""
    try:
        import pypdfium2 as pdfium
        page = pdfium.PdfDocument(pdf_bytes)[0]
        img = page.render(scale=1.6).to_pil().convert("RGB")
    except Exception:
        return None
    w, h = img.size
    cut = int(h * 0.36)
    lower = img.crop((0, cut, w, h)).filter(ImageFilter.GaussianBlur(9))
    img.paste(lower, (0, cut))
    over = Image.new("RGBA", img.size, (0, 0, 0, 0))
    dr = ImageDraw.Draw(over)
    font = ImageFont.truetype(str(FONT_DIR / "Poppins-Bold.ttf"), int(w * 0.11))
    mark = Image.new("RGBA", (int(w * 1.2), int(w * 0.2)), (0, 0, 0, 0))
    ImageDraw.Draw(mark).text((mark.width // 2, mark.height // 2), "PREVIEW", font=font, anchor="mm",
                              fill=(30, 31, 36, 70))
    mark = mark.rotate(28, expand=True)
    over.alpha_composite(mark, ((w - mark.width) // 2, cut + (h - cut - mark.height) // 2))
    band_y = cut + int((h - cut) * 0.08)
    dr.rounded_rectangle((int(w * 0.18), band_y, int(w * 0.82), band_y + int(w * 0.09)), int(w * 0.02),
                         fill=(30, 31, 36, 225))
    small = ImageFont.truetype(str(FONT_DIR / "Poppins-Bold.ttf"), int(w * 0.032))
    dr.text((w // 2, band_y + int(w * 0.045)), "FULL CV AFTER PAYMENT", font=small, anchor="mm", fill=(255, 255, 255, 255))
    img = Image.alpha_composite(img.convert("RGBA"), over).convert("RGB")
    buf = BytesIO()
    img.save(buf, "JPEG", quality=82)
    return buf.getvalue()


def sample_image(pdf_bytes):
    """A finished CV of an invented person, shown as "a sample of our work" (marked SAMPLE)."""
    try:
        import pypdfium2 as pdfium
        page = pdfium.PdfDocument(pdf_bytes)[0]
        img = page.render(scale=1.6).to_pil().convert("RGB")
    except Exception:
        return None
    w, h = img.size
    over = Image.new("RGBA", img.size, (0, 0, 0, 0))
    font = ImageFont.truetype(str(FONT_DIR / "Poppins-Bold.ttf"), int(w * 0.13))
    mark = Image.new("RGBA", (int(w * 1.2), int(w * 0.22)), (0, 0, 0, 0))
    ImageDraw.Draw(mark).text((mark.width // 2, mark.height // 2), "SAMPLE", font=font, anchor="mm",
                              fill=(30, 31, 36, 38))
    mark = mark.rotate(28, expand=True)
    over.alpha_composite(mark, ((w - mark.width) // 2, int(h * 0.55) - mark.height // 2))
    img = Image.alpha_composite(img.convert("RGBA"), over).convert("RGB")
    buf = BytesIO()
    img.save(buf, "JPEG", quality=84)
    return buf.getvalue()


def sample_photo():
    """A plain silhouette instead of a real person's face, for the sample CV."""
    size = 600
    im = Image.new("RGB", (size, size), (214, 222, 230))
    d = ImageDraw.Draw(im)
    d.ellipse((size * 0.32, size * 0.16, size * 0.68, size * 0.52), fill=(150, 164, 180))
    d.ellipse((size * 0.14, size * 0.58, size * 0.86, size * 1.3), fill=(150, 164, 180))
    buf = BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()
