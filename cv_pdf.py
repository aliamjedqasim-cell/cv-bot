"""Builds an English CV (PDF) in the layout of Ali's reference CV.

Dark header with a round photo, name and job title; a contact row with icons;
then Profile, Skills (3 columns), Professional Experience, Career Objective,
Education, Courses (optional) and Languages, separated by thin rules.

Usage:
    pdf_bytes = build_cv(data, photo_bytes=None)

`data` keys:
    full_name, job_title, phone, email, city,
    university, degree, edu_status ("2021" or "3rd-Year Student"),
    has_experience (bool),
    experiences: [{company, title, location, duration, duties: [..]}],
    customers: [..], products, territory          (sales roles with experience)
    survey (bool), licence ("car" | "licence" | "none")   (sales / field roles)
    skills: [..], languages: ["Arabic: Native", ..], courses: [..]
"""
import re
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from PIL import Image, ImageOps
from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Flowable,
    Frame,
    KeepTogether,
    NextPageTemplate,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

# ---------------------------------------------------------------- look & feel
HEADER_BG = HexColor("#1E1F24")
INK = HexColor("#111214")
BODY = HexColor("#1F2226")
RULE = HexColor("#2B2D31")
PLACEHOLDER = HexColor("#3A3C43")

PAGE_W, PAGE_H = A4
HEADER_H = 193
MARGIN_L = 60
MARGIN_R = PAGE_W - 536
CONTENT_W = PAGE_W - MARGIN_L - MARGIN_R

FONT_DIR = Path(__file__).parent / "fonts"
_fonts_ready = False

SALES_DUTIES = [
    "Promoting company products and services to customers and points of sale across the assigned area.",
    "Negotiating prices and payment terms, and closing sales deals with new and existing clients.",
    "Building strong, long-term relationships with clients and ensuring product availability and visibility.",
    "Driving sales growth toward daily and monthly targets and contributing to increasing the company's market share.",
]
MEDICAL_DUTIES = [
    "Promoting company products to doctors, pharmacists and clinics through regular, well-planned field visits.",
    "Delivering clear, persuasive product presentations that highlight key benefits and build prescriber confidence.",
    "Building strong, long-term relationships with healthcare professionals and key accounts in the territory.",
    "Driving sales growth toward monthly targets and reporting market feedback and competitor activity to management.",
]
GENERAL_DUTIES = [
    "Performing daily professional duties accurately and efficiently in line with company policies and quality standards.",
    "Communicating effectively with colleagues, customers and healthcare professionals.",
    "Maintaining accurate records and contributing to smooth, well-organized daily operations.",
    "Continuously developing technical knowledge and actively supporting team goals.",
]
PHARMACY_DUTIES = [
    "Dispensing medications accurately and advising patients on correct use, dosage and possible side effects.",
    "Building strong relationships with doctors, medical representatives and regular customers.",
    "Managing stock levels, expiry dates and supplier orders to ensure continuous product availability.",
    "Recommending suitable products to customers and actively supporting the pharmacy's sales.",
]
LAB_DUTIES = [
    "Performing laboratory tests accurately in line with quality control and safety standards.",
    "Preparing samples, operating and maintaining laboratory equipment, and recording results precisely.",
    "Communicating results clearly with doctors and patients and maintaining accurate records.",
    "Managing laboratory supplies and supporting smooth, well-organized daily operations.",
]
SALES_SKILLS = [
    "Strong Communication Skills", "Negotiation Skills in Sales", "Persuasion & Closing Deals",
    "Relationship Building", "Customer Service Skills", "Presentation Skills",
    "Teamwork Collaboration", "Time Management", "Working Under Pressure",
    "Problem-Solving Skills", "Fast Learning", "Detail-Oriented",
]
GENERAL_SKILLS = [
    "Strong Communication Skills", "Teamwork Collaboration", "Attention to Detail",
    "Time Management", "Problem-Solving Skills", "Working Under Pressure",
    "Fast Learning", "Organization & Planning", "Customer Service Skills",
    "Professional Ethics", "Adaptability", "Presentation Skills",
]
DEFAULT_SKILLS = SALES_SKILLS
FIELD_WORDS = ("sales", "representative", "rep", "supervisor", "marketing", "promoter", "business development")


def format_phone(raw):
    """07701234567 / 9647701234567 / 00964... -> +964 770 123 4567 (other numbers -> +digits)."""
    raw = str(raw or "")
    digits = "".join(ch for ch in raw if ch.isdigit())
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("964"):
        local = digits[3:].lstrip("0")
    elif digits.startswith("07") and len(digits) == 11:
        local = digits[1:]
    elif digits.startswith("7") and len(digits) == 10:
        local = digits
    else:
        return ("+" + digits) if raw.strip().startswith("+") or len(digits) > 11 else raw.strip()
    if len(local) == 10:
        return f"+964 {local[:3]} {local[3:6]} {local[6:]}"
    return f"+964 {local}"


def _register_fonts():
    global _fonts_ready
    if _fonts_ready:
        return
    for name, file in [
        ("Body", "Carlito-Regular.ttf"),
        ("Body-Bold", "Carlito-Bold.ttf"),
        ("Head", "Poppins-Regular.ttf"),
        ("Head-Light", "Poppins-Light.ttf"),
        ("Head-Bold", "Poppins-Bold.ttf"),
    ]:
        pdfmetrics.registerFont(TTFont(name, str(FONT_DIR / file)))
    pdfmetrics.registerFontFamily("Body", normal="Body", bold="Body-Bold", italic="Body", boldItalic="Body-Bold")
    _fonts_ready = True


def _styles(f=1.0):
    def st(name, font, size, lead, color=INK, **kw):
        return ParagraphStyle(name, fontName=font, fontSize=size * f, leading=lead * f, textColor=color, **kw)

    return {
        "f": f,
        "headline": st("headline", "Head", 14.5, 19),
        "h": st("h", "Body-Bold", 15, 19),
        "body": st("body", "Body", 11, 14.6, BODY, leftIndent=2),
        "bullet": st("bullet", "Body", 11, 14.6, BODY, leftIndent=13, bulletIndent=2,
                     bulletFontName="Body-Bold", bulletFontSize=11 * f),
        "job": st("job", "Body-Bold", 13.5, 17),
        "dur": st("dur", "Body-Bold", 13.5, 17, alignment=2),
        "edu": st("edu", "Body-Bold", 13, 17),
        "role": st("role", "Body", 12, 15, HexColor("#3A3D42")),
        "kv": st("kv", "Body", 11, 14.6, BODY, leftIndent=2),
        "lang": st("lang", "Body", 12, 15, BODY, leftIndent=2),
    }


def _one_line(text_markup, plain, style, width, min_size=10.5):
    """Shrink a bold one-liner (job / education line) so it fits on one line."""
    size = style.fontSize
    while size > min_size and pdfmetrics.stringWidth(plain, style.fontName, size) > width:
        size -= 0.25
    return Paragraph(text_markup, ParagraphStyle(style.name + "1", parent=style, fontSize=size, leading=max(size * 1.26, style.leading * 0.9)))


def esc(text):
    return escape(str(text or "").strip())


# --------------------------------------------------------------- text helpers
def nice_name(name):
    name = " ".join((name or "").split())
    return name.title() if (name.islower() or name.isupper()) else name


def is_medical(d):
    return "medic" in (d.get("job_title") or "").lower()


def is_field_role(d):
    title = (d.get("job_title") or "").lower()
    return any(w in title for w in FIELD_WORDS)


def default_skills(d):
    return SALES_SKILLS if is_field_role(d) else GENERAL_SKILLS


def default_duties(d):
    if not is_field_role(d):
        return GENERAL_DUTIES
    medical_customers = {"doctors", "clinics", "pharmacies", "hospitals"} & {c.lower() for c in d.get("customers") or []}
    return MEDICAL_DUTIES if is_medical(d) or medical_customers else SALES_DUTIES


def duties_for_job(d, job):
    """Standard duties that match this job's own title (falls back to the target role)."""
    title = (job.get("title") or "").lower()
    if not title:
        return default_duties(d)
    if any(w in title for w in FIELD_WORDS):
        return MEDICAL_DUTIES if ("medic" in title or is_medical(d)) else SALES_DUTIES
    if "pharmac" in title:
        return PHARMACY_DUTIES
    if "lab" in title:
        return LAB_DUTIES
    return GENERAL_DUTIES


def _join(items):
    items = [i for i in items if i]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1] if items else ""


def _field(d):
    if d.get("specialty"):
        return d["specialty"]
    degree = " ".join((d.get("degree") or "").split())
    m = re.search(r"\bin\s+(.+)$", degree, flags=re.I)
    field = m.group(1) if m else re.sub(r"^(bachelor('?s)?( degree)?|diploma|master('?s)?)( of)?\s*", "", degree, flags=re.I)
    field = field.strip() or degree or "university"
    return field.title() if field.islower() else field


def is_student(d):
    return not re.fullmatch(r"(19|20)\d{2}", str(d.get("edu_status") or "").strip())


def edu_status_text(d):
    status = str(d.get("edu_status") or "").strip()
    if re.fullmatch(r"(19|20)\d{2}", status):
        return f"Graduated {status}"
    return status


def _companies(d):
    names = [e["company"] for e in d.get("experiences") or [] if e.get("company")][:3]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def build_profile(d):
    if d.get("profile_en"):  # written by the AI from the applicant's own facts
        return d["profile_en"]
    field = _field(d)
    title = d.get("job_title") or "Sales Representative"
    stage = "student" if is_student(d) else "graduate"
    exp = d.get("has_experience") and _companies(d)
    if not is_field_role(d):
        if exp:
            return (f"Highly motivated and dedicated {title} with hands-on professional experience at {_companies(d)}. "
                    f"Known for accuracy, strong work ethic and excellent communication with colleagues, customers and "
                    f"healthcare professionals. A {field} {stage} who learns fast, adapts quickly and is fully "
                    f"committed to delivering high-quality work and continuous professional growth.")
        return (f"Ambitious, dedicated and highly motivated {field} {stage} with a solid academic foundation, "
                f"excellent communication skills and a strong passion for the healthcare sector. Quick to learn, "
                f"detail-oriented and ready to make an immediate, meaningful contribution to a professional team. "
                f"Highly adaptable and committed to continuous professional growth.")
    domain = "pharmaceutical and medical sales" if is_medical(d) else "sales and business development"
    if exp:
        reach = ""
        if d.get("customers"):
            reach = f", serving {_join([c.lower() for c in d['customers']])}"
            if d.get("territory"):
                reach += f" across {d['territory']}"
        return (f"Highly motivated and results-driven {title} with hands-on field experience at {_companies(d)}{reach}. "
                f"Skilled in building strong, long-lasting relationships with clients and key accounts, delivering "
                f"persuasive product presentations, negotiating effectively and driving sales toward daily and monthly "
                f"targets. A {field} {stage} who learns fast, adapts quickly and is fully committed to "
                f"continuous professional growth.")
    return (f"Ambitious, energetic and highly motivated {field} {stage} with excellent communication, persuasion "
            f"and negotiation skills and a strong passion for {domain}. Quick to learn products and markets, "
            f"confident in dealing with customers, and ready to make an immediate, meaningful contribution to a "
            f"dynamic sales team. Highly adaptable, target-oriented and committed to continuous professional growth.")


def build_objective(d):
    if d.get("objective_en"):
        return d["objective_en"]
    title = d.get("job_title") or "Sales Representative"
    if not is_field_role(d):
        knowledge = "my knowledge" if _field(d) == "High School" else f"my {_field(d)} knowledge"
        return (f"To join a reputable healthcare organization as a {title}, where I can apply {knowledge}, "
                f"strong work ethic and communication skills to deliver accurate, high-quality work, "
                f"support the team's goals and grow professionally in a dynamic, performance-driven environment.")
    extra = (", together with my scientific background, to build trust with healthcare professionals"
             if is_medical(d) else "")
    return (f"To join a reputable, growing company as a {title}, where I can leverage my strong communication, "
            f"persuasion and negotiation skills{extra}, expand the customer base, strengthen the company's market "
            f"presence and consistently work to achieve and exceed sales targets, while continuously developing my "
            f"professional skills in a dynamic, performance-driven environment.")


def additional_info(d):
    """Extra facts the applicant gave: age, and field-sales facts for sales roles."""
    out = [f"Age: {d['age']} years old."] if d.get("age") else []
    if not is_field_role(d):
        return out
    if d.get("survey"):
        out.append(f"Conducted a field market survey of doctors, clinics and potential customers in {d.get('city') or 'my city'}.")
    if d.get("licence") == "car":
        out.append("Holds a valid driving licence and owns a car, ready for daily field visits across the territory.")
    elif d.get("licence") == "licence":
        out.append("Holds a valid driving licence.")
    return out


_FILLER = {"skills", "skill", "strong", "in", "sales", "and", "&", "of", "the", "good", "excellent", "customer"}


def _words(text):
    """Key words of a skill, used to avoid near-duplicates like 'Communication' vs 'Strong Communication Skills'."""
    words = {w for w in re.findall(r"[a-z]+", text.lower()) if w not in _FILLER}
    return {w[:6] for w in words}  # 'negotiation' ~ 'negotiating', 'team' ~ 'teamwork'


def _initials(name):
    parts = [p for p in (name or "").split() if p]
    if not parts:
        return "CV"
    return (parts[0][0] + (parts[-1][0] if len(parts) > 1 else "")).upper()


def _sentence(t):
    t = " ".join(str(t or "").split()).strip(" -•*")
    if not t:
        return ""
    t = t[0].upper() + t[1:]
    return t if t[-1] in ".!?" else t + "."


# ------------------------------------------------------------------ flowables
class Rule(Flowable):
    def __init__(self, f=1.0):
        super().__init__()
        self.sb, self.sa = 4 * f, 7 * f

    def wrap(self, aw, ah):
        self.aw = aw
        return aw, self.sb + self.sa

    def draw(self):
        self.canv.setStrokeColor(RULE)
        self.canv.setLineWidth(0.9)
        self.canv.line(0, self.sa, self.aw, self.sa)


def section(title, s):
    return [Rule(s["f"]), Paragraph(esc(title), s["h"]), Spacer(1, 3 * s["f"])]


def _grid_table(rows, col_widths):
    t = Table(rows, colWidths=col_widths)
    t.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return t


# ------------------------------------------------------------------- drawing
def _load_photo(photo_bytes):
    if not photo_bytes:
        return None
    try:
        im = Image.open(BytesIO(photo_bytes))
        im = ImageOps.exif_transpose(im).convert("RGB")
        side = min(im.size)
        left = (im.width - side) // 2
        top = max(0, (im.height - side) // 4)  # keep the face in frame
        im = im.crop((left, top, left + side, top + side)).resize((700, 700))
        buf = BytesIO()
        im.save(buf, "JPEG", quality=90)
        buf.seek(0)
        return ImageReader(buf)
    except Exception:
        return None


def _fit_font(c, text, font, size, max_w, min_size):
    while size > min_size and c.stringWidth(text, font, size) > max_w:
        size -= 0.5
    return size


def _icon_box(c, x, y, kind):
    """Black rounded square with a white vector icon. (x, y) = bottom-left."""
    s = 15
    c.saveState()
    c.setFillColor(INK)
    c.roundRect(x, y, s, s, 2, stroke=0, fill=1)
    c.setFillColor(white)
    c.setStrokeColor(white)
    if kind == "phone":  # smartphone
        c.roundRect(x + 4.6, y + 2.6, 5.8, 9.8, 1.2, stroke=0, fill=1)
        c.setFillColor(INK)
        c.rect(x + 5.6, y + 4.4, 3.8, 6.4, stroke=0, fill=1)
        c.circle(x + 7.5, y + 3.5, 0.5, stroke=0, fill=1)
    elif kind == "mail":  # envelope
        c.rect(x + 3, y + 4, 9, 6.6, stroke=0, fill=1)
        c.setStrokeColor(INK)
        c.setLineWidth(1)
        p = c.beginPath()
        p.moveTo(x + 3.2, y + 10.4)
        p.lineTo(x + 7.5, y + 6.6)
        p.lineTo(x + 11.8, y + 10.4)
        c.drawPath(p, stroke=1, fill=0)
    else:  # location pin
        p = c.beginPath()
        p.moveTo(x + 7.5, y + 2.4)
        p.lineTo(x + 4.3, y + 8.2)
        p.lineTo(x + 10.7, y + 8.2)
        p.close()
        c.drawPath(p, stroke=0, fill=1)
        c.circle(x + 7.5, y + 9.0, 3.4, stroke=0, fill=1)
        c.setFillColor(INK)
        c.circle(x + 7.5, y + 9.0, 1.3, stroke=0, fill=1)
    c.restoreState()


def _draw_first_page(d, photo):
    name = nice_name(d.get("full_name"))
    title = (d.get("job_title") or "Sales Representative").upper()

    def draw(c, doc):
        c.saveState()
        # header band
        c.setFillColor(HEADER_BG)
        c.rect(0, PAGE_H - HEADER_H, PAGE_W, HEADER_H, stroke=0, fill=1)
        cy = PAGE_H - 98
        if photo:
            cx, r = 118, 72
            c.setFillColor(white)
            c.circle(cx, cy, r + 3.5, stroke=0, fill=1)
            p = c.beginPath()
            p.circle(cx, cy, r)
            c.clipPath(p, stroke=0, fill=0)
            c.drawImage(photo, cx - r, cy - r, width=2 * r, height=2 * r)
            c.restoreState()
            c.saveState()
            text_cx, text_w = 389, 345
        else:
            text_cx, text_w = PAGE_W / 2, PAGE_W - 110

        # name (shrinks to fit; very long names wrap onto two lines)
        size = _fit_font(c, name, "Head-Bold", 31, text_w, 22)
        lines = [name]
        if c.stringWidth(name, "Head-Bold", size) > text_w:
            words = name.split()
            half = (len(words) + 1) // 2
            lines = [" ".join(words[:half]), " ".join(words[half:])]
            size = max(_fit_font(c, max(lines, key=len), "Head-Bold", 28, text_w, 18), 18)
        c.setFillColor(white)
        c.setFont("Head-Bold", size)
        base = PAGE_H - 112 + (size * 0.6 if len(lines) == 2 else 0)
        for i, line in enumerate(lines):
            c.drawCentredString(text_cx, base - i * size * 1.15, line)
        # job title with letter spacing
        tsize = _fit_font(c, title, "Head-Light", 15.5, text_w - 30, 10)
        ty = base - (len(lines) - 1) * size * 1.15 - 34
        t = c.beginText()
        t.setFont("Head-Light", tsize)
        t.setCharSpace(0.6)
        width = c.stringWidth(title, "Head-Light", tsize) + 0.6 * (len(title) - 1)
        t.setTextOrigin(text_cx - width / 2, ty)
        t.textOut(title)
        c.drawText(t)

        # contact row
        row_y = PAGE_H - HEADER_H - 23
        items = [("phone", format_phone(d.get("phone")), 66), ("mail", d.get("email"), 237), ("pin", f"Iraq / {d.get('city')}" if d.get("city") else "", 436)]
        items = [it for it in items if it[1]]
        if len(items) < 3:  # spread the remaining items evenly
            step = CONTENT_W / max(len(items), 1)
            items = [(k, v, MARGIN_L + 6 + i * step) for i, (k, v, _) in enumerate(items)]
        c.setFillColor(INK)
        for i, (kind, val, x) in enumerate(items):
            nxt = items[i + 1][2] if i + 1 < len(items) else PAGE_W - MARGIN_R
            _icon_box(c, x, row_y - 4, kind)
            fsize = _fit_font(c, val, "Head", 9.6, nxt - x - 30, 7.5)
            c.setFillColor(HexColor("#26282C"))
            c.setFont("Head", fsize)
            c.drawString(x + 21, row_y + 0.6, val)
        c.setStrokeColor(RULE)
        c.setLineWidth(0.9)
        c.line(MARGIN_L, row_y - 13, PAGE_W - MARGIN_R, row_y - 13)
        c.restoreState()

    return draw


# ---------------------------------------------------------------------- story
def _story(d, s, max_skills=12, duty_targets=(4, 3)):
    f = s["f"]
    out = [
        Paragraph(esc(d.get("job_title") or "Sales Representative"), s["headline"]),
        Spacer(1, 2 * f),
        Paragraph(esc(build_profile(d)), s["body"]),
        Spacer(1, 4 * f),
    ]

    skills = ([x for x in d.get("skills") or [] if x][:12] or list(default_skills(d)))[:max_skills]
    for extra in default_skills(d):
        if len(skills) >= 9:
            break
        if not (_words(extra) & set().union(*[_words(k) for k in skills] or [set()])):
            skills.append(extra)
    if skills:
        cols = 3
        per_col = (len(skills) + cols - 1) // cols
        columns = [skills[i * per_col:(i + 1) * per_col] for i in range(cols)]
        rows = []
        for r in range(per_col):
            rows.append([Paragraph(esc(col[r]), s["bullet"], bulletText="•") if r < len(col) else "" for col in columns])
        out.append(KeepTogether([Rule(f), Paragraph("SKILLS", s["h"]), Spacer(1, 3 * f),
                                 _grid_table(rows, [CONTENT_W / 3] * 3), Spacer(1, 4 * f)]))

    exps = [e for e in d.get("experiences") or [] if e.get("company")]
    if d.get("has_experience") and exps:
        target = duty_targets[0] if len(exps) == 1 else duty_targets[1]  # standard duties are topped up to this
        for i, e in enumerate(exps):
            plain = e["company"] + (f" | {e['location']}" if e.get("location") else "")
            dur_plain = f"( {e['duration']} )" if e.get("duration") else ""
            dur_w = pdfmetrics.stringWidth(dur_plain, "Body-Bold", s["dur"].fontSize) + 8 if dur_plain else 0
            head_w = CONTENT_W - dur_w
            block = section("Professional Experience", s) if i == 0 else []  # heading stays with 1st job
            block += [Spacer(1, 5 * f), _grid_table(
                [[_one_line(esc(plain), plain, s["job"], head_w - 6), Paragraph(esc(dur_plain), s["dur"])]],
                [head_w, CONTENT_W - head_w] if dur_w else [CONTENT_W - 1, 1],
            ), Spacer(1, 1 * f)]
            if e.get("title"):
                block += [Paragraph(esc(e["title"]), s["role"]), Spacer(1, 2 * f)]
            else:
                block.append(Spacer(1, 2 * f))
            duties = [_sentence(x) for x in (e.get("duties") or []) if _sentence(x)][:5]
            verbs = {x.split()[0].lower() for x in duties}
            for extra in duties_for_job(d, e):
                if len(duties) >= target or (d.get("_ai") and len(duties) >= 3):  # AI already wrote enough
                    break
                if extra.split()[0].lower() not in verbs:  # don't repeat what they already wrote
                    duties.append(extra)
            block += [Paragraph(esc(x), s["bullet"], bulletText="•") for x in duties if x]
            out.append(KeepTogether(block))
        highlights = []
        if is_field_role(d):
            for key, val in [("Customers Covered", _join(d.get("customers") or [])),
                             ("Products / Specialties", d.get("products")),
                             ("Territory Covered", d.get("territory"))]:
                if val:
                    highlights.append(Paragraph(f"<font name='Body-Bold'>{key}:</font> {esc(val)}", s["kv"]))
        if highlights:
            out.append(KeepTogether([Spacer(1, 5 * f)] + highlights))
        out.append(Spacer(1, 4 * f))

    out.append(KeepTogether(section("Career Objective", s) + [
        Paragraph(esc(build_objective(d)), s["body"]), Spacer(1, 4 * f)]))

    edu_plain = " | ".join(str(x).strip() for x in [d.get("university"), d.get("degree"), edu_status_text(d)] if x)
    out.append(KeepTogether(section("Education", s) + [
        _one_line(esc(edu_plain), edu_plain, s["edu"], CONTENT_W - 4, 10), Spacer(1, 4 * f)]))

    courses = [c for c in d.get("courses") or [] if c][:8]
    if courses:
        out.append(KeepTogether(section("Courses & Certifications", s) + [
            Paragraph(esc(c), s["bullet"], bulletText="•") for c in courses] + [Spacer(1, 4 * f)]))

    extra_info = additional_info(d)
    if extra_info:
        out.append(KeepTogether(section("Additional Information", s) + [
            Paragraph(esc(x), s["bullet"], bulletText="•") for x in extra_info] + [Spacer(1, 4 * f)]))

    langs = [x for x in d.get("languages") or [] if x][:6]
    if langs:
        out.append(KeepTogether(section("Languages", s) + [Paragraph(esc(x), s["lang"]) for x in langs]))
    return out


def _render(data, photo, f, **density):
    s = _styles(f)
    buf = BytesIO()
    doc = BaseDocTemplate(
        buf, pagesize=A4, leftMargin=MARGIN_L, rightMargin=MARGIN_R, topMargin=40, bottomMargin=30,
        title=f"CV - {nice_name(data.get('full_name'))}", author=nice_name(data.get("full_name")),
    )
    first_top = PAGE_H - HEADER_H - 50
    first = Frame(MARGIN_L, 30, CONTENT_W, first_top - 30, id="first",
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    later = Frame(MARGIN_L, 30, CONTENT_W, PAGE_H - 70, id="later",
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([
        PageTemplate(id="first", frames=[first], onPage=_draw_first_page(data, photo)),
        PageTemplate(id="later", frames=[later]),
    ])
    doc.build([NextPageTemplate("later")] + _story(data, s, **density))
    return buf.getvalue(), doc.page


def build_cv(data, photo_bytes=None):
    """Returns PDF bytes, kept to one page when possible: first the text shrinks a little, then
    fewer auto-added skills / standard duties are used. The applicant's own content is never cut."""
    _register_fonts()
    photo = _load_photo(photo_bytes)
    plans = [
        (dict(max_skills=12, duty_targets=(4, 3)), (1.0, 0.97, 0.94, 0.91, 0.88)),
        (dict(max_skills=9, duty_targets=(4, 3)), (1.0, 0.97, 0.94, 0.91, 0.88)),
        (dict(max_skills=9, duty_targets=(3, 2)), (1.0, 0.97, 0.94, 0.91, 0.88, 0.85, 0.82, 0.79, 0.76)),
    ]
    first = None
    for density, scales in plans:
        for f in scales:
            pdf, pages = _render(data, photo, f, **density)
            first = first or pdf
            if pages == 1:
                return pdf
    return first  # very long CV: two pages at full size
