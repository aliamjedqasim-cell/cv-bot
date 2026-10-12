"""The one-message form ("كليشة"): the text applicants fill in, and a reader for it that works
without AI (labels -> fields -> English via translate.py). Returns the same shape as ai.read_cv()."""
import re

from translate import (AR_DIGITS, clean, company_en, duration_en, has_ar, language_en, name_en, norm,
                       place_en, products_en, sentence_en, skill_en, specialty_en, title_en,
                       university_en)

TEMPLATE = """الاسم الثلاثي:
العمر:
رقم الهاتف:
الإيميل:
المحافظة والمنطقة:
الوظيفة اللي تريدها:
الشهادة والتخصص:
الجامعة أو المعهد:
سنة التخرج أو المرحلة:
الخبرات:
مهامك بالشغل:
المنتجات اللي سوّقتها:
المناطق اللي غطيتها:
الزبائن (أطباء، صيدليات، عيادات...):
المهارات:
مستوى الإنگليزي:
لغات ثانية:
الدورات:
عندك إجازة سوق وسيارة؟
سويت مسح ميداني؟"""

# (keywords in the label, field) - checked in this order
LABELS = [
    (("دورات", "دوره", "كورسات"), "courses"), (("مسح",), "survey"), (("اجازه", "سياره", "سياقه"), "licence"),
    (("لغات", "لغه ثانيه"), "other_langs"), (("انكليزي", "انجليزي", "الانكليزيه", "انكليزيه"), "english"),
    (("مهام", "مهامك", "شغلك"), "duties"), (("منتجات", "اصناف"), "products"), (("مناطق", "غطيت"), "territory"),
    (("زبائن", "عملاء"), "customers"), (("مهارات",), "skills"), (("خبرات", "خبره", "الخبرات"), "experiences"),
    (("تخرج", "مرحله", "سنه التخرج"), "edu_status"), (("جامعه", "معهد", "كليه"), "university"),
    (("شهاده", "تخصص"), "degree"), (("وظيفه", "الوظيفه"), "job_title"),
    (("محافظه", "سكن", "مدينه", "منطقه"), "city"), (("ايميل", "بريد", "email"), "email"),
    (("هاتف", "رقم", "موبايل", "تلفون"), "phone"), (("عمر", "مواليد", "تولد"), "age"), (("اسم", "name"), "name"),
]
DEGREE_WORDS = [("بكالوريوس|بكلوريوس|بكالوريس|بكلوريس", "Bachelor's Degree"), ("دبلوم", "Diploma"),
                ("ماجستير|ماستر", "Master's Degree"), ("دكتوراه|دكتوراة|دكتوراه", "PhD"),
                ("اعداديه|سادس|ثانويه", "High School Diploma")]
CUSTOMER_WORDS = [("طبيب|اطباء|دكاتره|دكتور", "Doctors"), ("صيدلي|صيدليات|صيدليه", "Pharmacies"),
                  ("عياد", "Clinics"), ("مستشف", "Hospitals"), ("محل|اسواق|سوق|ماركت", "Shops & Markets"),
                  ("شركات", "Companies")]
ENGLISH_WORDS = [("ممتاز|فلونت|طلاقه|بطلاقه|fluent", "Fluent"), ("جيد جدا|جيد جداً|very good", "Very Good"),
                 ("متوسط|intermediate", "Intermediate"), ("ضعيف|مبتدئ|بسيط|شوي|basic", "Basic"),
                 ("ما اعرف|ماعرف|لا اعرف|ما اجيد|no", "none"), ("جيد|زين|good", "Good")]
YES = ("نعم", "اي", "ايه", "اكيد", "عندي", "موجود", "سويت", "yes")
NO = ("لا", "ما عندي", "ماعندي", "ما سويت", "ماسويت", "كلا", "no")
STAGES = {"اولى": "1st", "الاولى": "1st", "ثانيه": "2nd", "الثانيه": "2nd", "ثالثه": "3rd", "الثالثه": "3rd",
          "رابعه": "4th", "الرابعه": "4th", "خامسه": "5th", "الخامسه": "5th", "سادسه": "6th", "السادسه": "6th",
          "اخيره": "Final", "الاخيره": "Final"}


LABELS = [(tuple(norm(w) for w in words), field) for words, field in LABELS]  # same spelling rules as the input


def _label(line):
    """'الاسم الثلاثي: علي' -> ('name', 'علي'); None if the line is not a form label."""
    m = re.match(r"^\s*([^:：؟?]{1,45})[:：؟?]\s*(.*)$", line)
    if not m:
        return None
    head = norm(re.sub(r"\(.*?\)", "", m.group(1))).lower()
    for words, field in LABELS:
        if any(w in head for w in words):
            return field, m.group(2).strip()
    return None


def parse_form(text):
    """Splits a filled form into {field: raw text}. Returns None if it doesn't look like the form."""
    fields, current = {}, None
    for raw_line in (text or "").splitlines():
        line = clean(raw_line)
        if not line:
            continue
        hit = _label(line)
        if hit:
            current, value = hit
            if current in fields:  # the same label twice (e.g. two "الخبرات"): keep both
                fields[current] += ("\n" + value) if value else ""
            else:
                fields[current] = value
        elif current:
            fields[current] = (fields[current] + "\n" + line).strip()
    fields = {k: v.strip() for k, v in fields.items() if v and v.strip(" -.")}
    return fields if len(fields) >= 2 else None


def _yes_no(text):
    t = norm(text).lower()
    if any(t.startswith(w) or f" {w} " in f" {t} " for w in NO):
        return False
    if any(w in t for w in YES):
        return True
    return None


def _english(text):
    t = norm(text).lower()
    for words, level in ENGLISH_WORDS:
        if any(w in t for w in words.split("|")):
            return level
    return None


def _edu_status(text):
    t = clean(text).translate(AR_DIGITS)
    year = re.search(r"(19[7-9]\d|20[0-3]\d)", t)
    if year:
        return year.group(1)
    words = norm(t).split()
    stage = next((STAGES[w] for w in words if w in STAGES), None)
    m = re.search(r"\b([1-6])\b", t)
    if not stage and m:
        stage = {"1": "1st", "2": "2nd", "3": "3rd", "4": "4th", "5": "5th", "6": "6th"}[m.group(1)]
    if stage:
        return f"{stage}-Year Student"
    if not has_ar(t) and t:
        return t[:40]
    return "University Student" if any(w in norm(t) for w in ("طالب", "طالبه", "مرحله")) else None


def _degree(text):
    """'بكالوريوس صيدلة' -> ("Bachelor's Degree", "Pharmacy", 'صيدلة')."""
    t = norm(text)
    level = None
    for words, value in DEGREE_WORDS:
        for w in words.split("|"):
            if w in t:
                level = value
                t = t.replace(w, " ")
    level_words = {w for words, _ in DEGREE_WORDS for w in words.split("|")}
    rest = " ".join(w for w in clean(text).split() if norm(w) not in level_words and norm(w) not in ("في", "ب", "-"))
    rest = clean(rest.strip(" -،,"))
    if level == "High School Diploma" or not rest:
        return level, None, None
    return level, specialty_en(rest)[0], rest


def _job_line(line):
    parts = [clean(p) for p in re.split(r"\s*[|،,]\s*|\s+[-–—]\s+|\s+/\s+", line) if clean(p)]
    if not parts:
        return None
    dur_re = re.compile(r"\d|سنه|سنة|سنتين|شهر|اشهر|أشهر|شهور|حاليا|لحد|year|month|present", re.I)
    duration = next((p for p in parts[1:] if dur_re.search(p)), "")
    rest = [p for p in parts[1:] if p != duration]
    title = rest[0] if rest else ""
    location = ", ".join(rest[1:]) if len(rest) > 1 else ""
    return {
        "company_en": company_en(parts[0])[0], "company_ar": parts[0] if has_ar(parts[0]) else None,
        "title_en": title_en(title)[0] if title else None, "title_ar": title or None,
        "location_en": place_en(location)[0] if location else None, "location_ar": location or None,
        "duration_en": duration_en(duration)[0] if duration else None, "duration_ar": duration or None,
        "duties_en": [], "duties_ar": [],
    }


def _lines(text):
    out = [clean(x).strip(" -•*") for x in re.split(r"\n|،(?=\s*\S{3,})", text or "") if clean(x).strip(" -•*")]
    return out


def convert_form(fields):
    """Form fields -> the same dict shape ai.read_cv() returns."""
    f = fields
    out = {k: None for k in ("full_name_en", "full_name_ar", "age", "phone", "email", "city_en", "city_ar",
                             "job_title_en", "job_title_ar", "degree_level", "specialty_en", "specialty_ar",
                             "university_en", "university_ar", "edu_status", "edu_status_ar", "products_en",
                             "products_ar", "territory_en", "territory_ar", "survey", "licence",
                             "english_level", "profile_en", "objective_en")}
    out.update(experiences=[], customers_en=[], skills_en=[], skills_ar=[], other_languages_en=[],
               courses_en=[], courses_ar=[])
    if f.get("name"):
        out["full_name_en"], out["full_name_ar"] = name_en(f["name"])[0], f["name"] if has_ar(f["name"]) else None
    if f.get("age"):
        digits = re.findall(r"\d+", f["age"].translate(AR_DIGITS))
        if digits:
            n = int(digits[0])
            out["age"] = str(2026 - n) if n > 1900 else str(n)
    if f.get("phone"):
        out["phone"] = f["phone"]
    if f.get("email") and re.search(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", f["email"]):
        out["email"] = re.search(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", f["email"]).group(0).lower()
    for key, fn in (("city", place_en), ("job_title", title_en), ("university", university_en),
                    ("products", products_en), ("territory", place_en)):
        if f.get(key):
            out[f"{key}_en"], out[f"{key}_ar"] = fn(f[key])[0], f[key]
    if f.get("degree"):
        out["degree_level"], out["specialty_en"], out["specialty_ar"] = _degree(f["degree"])
    if f.get("edu_status"):
        out["edu_status"], out["edu_status_ar"] = _edu_status(f["edu_status"]), f["edu_status"]
    if f.get("experiences") and not any(w in norm(f["experiences"]) for w in ("ما عندي", "ماعندي", "لا يوجد")):
        out["experiences"] = [j for j in (_job_line(x) for x in f["experiences"].splitlines() if clean(x)) if j][:6]
    if f.get("duties") and out["experiences"]:
        duties = _lines(f["duties"])[:5]
        out["experiences"][0]["duties_ar"] = duties
        out["experiences"][0]["duties_en"] = [e for e in (sentence_en(x)[0] for x in duties) if e]
    if f.get("customers"):
        t = norm(f["customers"])
        out["customers_en"] = [en for words, en in CUSTOMER_WORDS if any(w in t for w in words.split("|"))]
    if f.get("skills"):
        items = [x for x in re.split(r"[,\n،;]+", f["skills"]) if clean(x)][:12]
        out["skills_ar"] = [clean(x) for x in items]
        out["skills_en"] = [skill_en(clean(x))[0] for x in items]
    if f.get("english"):
        out["english_level"] = _english(f["english"])
    if f.get("other_langs") and _yes_no(f["other_langs"]) is not False:
        langs = [x for x in re.split(r"[,\n،;\s]+و?|\s+و", f["other_langs"]) if clean(x)]
        out["other_languages_en"] = [en for en, how in (language_en(clean(x)) for x in langs)
                                     if en and en.lower() not in ("arabic", "english") and how != "translit"][:5]
    if f.get("courses") and _yes_no(f["courses"]) is not False:
        courses = _lines(f["courses"])[:8]
        out["courses_ar"] = courses
        out["courses_en"] = [e for e in (sentence_en(x)[0] for x in courses) if e]
    if f.get("licence"):
        t = norm(f["licence"])
        no_car = any(w in t for w in ("بدون سياره", "ما عندي سياره", "ماعندي سياره", "ما عندي سيار", "اجازه فقط",
                                      "بس اجازه", "اجازه بس"))
        yn = _yes_no(f["licence"])
        out["licence"] = "licence" if no_car else (None if yn is None else ("car" if yn else "none"))
    if f.get("survey"):
        out["survey"] = _yes_no(f["survey"])
    return out
