"""Telegram CV & medical-jobs bot (webhook mode, made for Render free web services).

Main menu: create a CV, post a job ad, contact the admin.

Environment variables (Render -> Environment). Only BOT_TOKEN is required:
    BOT_TOKEN         token from @BotFather
    ADMIN_CHAT_ID     your Telegram chat id (send /myid to the bot). Every CV, payment proof,
                      job ad and message comes to you here. 0 or empty = no admin.
    WEBHOOK_SECRET    any random text (Render can generate it)
    CHANNEL_ID        the jobs channel, e.g. @my_channel or -100123... (the bot must be an admin there)
    PRICE_IQD         CV price, e.g. 25000. 0 or empty = free service
    QI_CARD_NAME      name on the Qi Card that receives payments
    QI_CARD_NUMBER    the Qi Card number shown to applicants
    GROQ_API_KEY      free Groq key (console.groq.com) for reading free-form applications with AI
    MYMEMORY_EMAIL    optional; raises the free translation limit (5,000 -> 50,000 characters a day)
    PUBLIC_URL        only needed outside Render (Render provides RENDER_EXTERNAL_URL)
"""
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import requests
from flask import Flask, abort, request

from cv_pdf import build_cv, edu_status_text, format_phone, is_field_role, is_student, nice_name
import ai
from form import TEMPLATE, convert_form, parse_form
from translate import (company_en, duration_en, has_ar, language_en, name_en, norm, place_en, products_en,
                       sentence_en, skill_en, specialty_en, title_en, transliterate, university_en)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cvbot")


def env(name, default=""):
    value = os.environ.get(name, "").strip()
    return default if value.lower() in ("", "0", "-", "none", "null", "no") else value


TOKEN = env("BOT_TOKEN")
ADMIN_CHAT_ID = re.sub(r"[^\d-]", "", env("ADMIN_CHAT_ID"))
CHANNEL_ID = env("CHANNEL_ID")
if CHANNEL_ID and not CHANNEL_ID.lstrip("-").isdigit():  # "t.me/name", "name" or "@name" -> "@name"
    CHANNEL_ID = "@" + re.sub(r"^(https?://)?(t\.me/|telegram\.me/)?@?", "", CHANNEL_ID).strip("/")
PRICE_IQD = int(re.sub(r"\D", "", env("PRICE_IQD")) or 0)
QI_CARD_NAME = env("QI_CARD_NAME")
QI_CARD_NUMBER = env("QI_CARD_NUMBER")
PAID = bool(PRICE_IQD and QI_CARD_NUMBER and ADMIN_CHAT_ID)
SECRET = re.sub(r"[^A-Za-z0-9_-]", "", env("WEBHOOK_SECRET")) or hashlib.sha256(TOKEN.encode()).hexdigest()[:32]
PUBLIC_URL = (env("RENDER_EXTERNAL_URL") or env("PUBLIC_URL")).rstrip("/")
API = f"https://api.telegram.org/bot{TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{TOKEN}"
BOT_USERNAME = ""

app = Flask(__name__)
http = requests.Session()

# =================================================================== texts
SKIP = "⏭️ تخطي"
YES_EXP, NO_EXP = "✅ عندي خبرة", "🎓 ما عندي خبرة"
ADD_JOB, DONE_JOBS = "➕ أضيف شغل ثاني", "✅ لا، كمّل"
YES_SURVEY, NO_SURVEY = "✅ نعم سويت", "❌ لا"
LIC_CAR, LIC_ONLY, LIC_NONE = "🚗 عندي إجازة سوق وسيارة", "🪪 عندي إجازة بدون سيارة", "❌ ما عندي إجازة سوق"
NAME_OK, NAME_FIX = "✅ صحيح", "✏️ أكتبه بالإنگليزي بنفسي"
MAX_JOBS = 6

# (Arabic button, English value on the CV)
TITLE_BUTTONS = [("مندوب طبي", "Medical Representative"), ("مندوب مبيعات طبي", "Medical Sales Representative"),
                 ("مندوب مبيعات", "Sales Representative"), ("مشرف مبيعات", "Sales Supervisor"),
                 ("أخصائي تسويق", "Marketing Specialist"), ("صيدلاني", "Pharmacist"),
                 ("أخصائي مختبر", "Laboratory Specialist")]
CITY_BUTTONS = [("بغداد", "Baghdad"), ("البصرة", "Basra"), ("النجف", "Najaf"), ("كربلاء", "Karbala"),
                ("بابل", "Babylon"), ("ميسان", "Maysan"), ("ذي قار", "Dhi Qar"), ("الديوانية", "Diwaniyah"),
                ("واسط", "Wasit"), ("المثنى", "Al-Muthanna"), ("ديالى", "Diyala"), ("الأنبار", "Anbar"),
                ("نينوى", "Nineveh"), ("صلاح الدين", "Salahuddin"), ("كركوك", "Kirkuk"), ("أربيل", "Erbil")]
DEGREE_BUTTONS = [("بكالوريوس", "Bachelor's Degree"), ("دبلوم", "Diploma"), ("ماجستير", "Master's Degree"),
                  ("دكتوراه", "PhD"), ("إعدادية", "High School Diploma")]
STAGE_BUTTONS = [("طالب مرحلة ثانية", "2nd-Year Student"), ("طالب مرحلة ثالثة", "3rd-Year Student"),
                 ("طالب مرحلة رابعة", "4th-Year Student"), ("طالب مرحلة أخيرة", "Final-Year Student")]
DURATION_BUTTONS = [("٣ أشهر", "3 months"), ("٦ أشهر", "6 months"), ("سنة", "1 year"), ("سنتين", "2 years"),
                    ("٣ سنوات", "3 years"), ("أكثر من ٣ سنوات", "3+ years")]
ENGLISH_LEVELS = [("ممتاز", "Fluent"), ("جيد جداً", "Very Good"), ("جيد", "Good"), ("متوسط", "Intermediate"),
                  ("مبتدئ", "Basic"), ("ما أعرف إنگليزي", "none")]
# (key, Arabic button, English value)
CUSTOMER_TYPES = [("doc", "الأطباء", "Doctors"), ("cli", "العيادات", "Clinics"), ("pha", "الصيدليات", "Pharmacies"),
                  ("hos", "المستشفيات", "Hospitals"), ("shp", "المحلات والأسواق", "Shops & Markets"),
                  ("com", "الشركات", "Companies")]
SKILL_OPTIONS = [("com", "مهارات التواصل", "Strong Communication Skills"), ("neg", "التفاوض", "Negotiation Skills in Sales"),
                 ("per", "الإقناع وإغلاق البيع", "Persuasion & Closing Deals"), ("rel", "بناء العلاقات", "Relationship Building"),
                 ("cus", "خدمة العملاء", "Customer Service Skills"), ("pre", "العرض والتقديم", "Presentation Skills"),
                 ("team", "العمل الجماعي", "Teamwork Collaboration"), ("time", "إدارة الوقت", "Time Management"),
                 ("press", "العمل تحت الضغط", "Working Under Pressure"), ("prob", "حل المشكلات", "Problem-Solving Skills"),
                 ("fast", "سرعة التعلم", "Fast Learning"), ("off", "برامج الأوفيس", "Microsoft Office"),
                 ("soc", "التسويق بالسوشيال ميديا", "Social Media Marketing"), ("det", "الدقة بالتفاصيل", "Detail-Oriented")]
OTHER_LANGS = [("ku", "الكردية", "Kurdish"), ("tr", "التركية", "Turkish"), ("fa", "الفارسية", "Persian"),
               ("fr", "الفرنسية", "French"), ("de", "الألمانية", "German"), ("tk", "التركمانية", "Turkmen")]
MULTI = {  # prefix -> (step, options, selection key, extras key, label when nothing is chosen)
    "c": ("customers", CUSTOMER_TYPES, "_cust_sel", None, "⏭️ تخطي"),
    "s": ("skills", SKILL_OPTIONS, "_skills_sel", "skills_extra", "✨ اختارلي أقوى المهارات"),
    "l": ("other_langs", OTHER_LANGS, "_langs_sel", "langs_extra", "⏭️ لا، بس هذني"),
}

MENU = [
    [{"text": "📝 إنشاء سيرة ذاتية CV", "callback_data": "m:cv"}],
    [{"text": "📢 نشر إعلان وظيفة", "callback_data": "m:ad"}],
    [{"text": "💬 التواصل مع الإدارة", "callback_data": "m:contact"}],
]
WELCOME = (
    "أهلاً وسهلاً 👋\n\n"
    "هذا البوت يساعدك تسوي <b>سيرة ذاتية احترافية بالإنگليزي</b> للوظائف بالقطاع الطبي، "
    "وتنشر إعلانات الوظائف، وتتواصل ويا الإدارة.\n\n"
    "اختار شنو تحتاج 👇\n\n"
    "🔒 <i>معلوماتك تُستخدم فقط لإعداد سيرتك الذاتية وترشيحك لفرص عمل مناسبة، وما تنشر بشكل عام.</i>"
)

QUESTIONS = {  # step -> (question, optional?)
    "form": ("دز معلوماتك برسالة وحدة 👆", False),
    "name": ("شنو اسمك الثلاثي؟ ✍️\nمثال: <b>علي حسن كريم</b>", False),
    "name_ok": ("", False),
    "name_en": ("اكتب اسمك بالحروف الإنگليزية مثل ما تريده يطلع بالسي في\nمثال: <b>Ali Hassan Kareem</b>", False),
    "job_title": ("شنو الوظيفة اللي تريد تقدم عليها؟\nاختار من الأزرار أو اكتبها", False),
    "phone": ("رقم هاتفك 📞\nمثال: <b>07701234567</b>", False),
    "email": ("الإيميل ✉️\nمثال: <b>ali.hassan@gmail.com</b>\nإذا ما عندك اضغط تخطي", True),
    "city": ("وين تسكن؟ اختار محافظتك أو اكتبها 📍", False),
    "degree_level": ("شنو شهادتك؟ 🎓", False),
    "specialty": ("شنو تخصصك؟\nمثال: <b>صيدلة</b> أو <b>تحليلات مرضية</b> أو <b>إدارة أعمال</b>", False),
    "university": ("اسم الجامعة أو الكلية أو المعهد\nمثال: <b>جامعة بغداد</b>", False),
    "edu_status": ("إذا متخرج اكتب <b>سنة التخرج</b>، مثل <b>2021</b>\nوإذا بعدك طالب اختار مرحلتك 👇", False),
    "has_exp": ("عندك خبرة شغل سابقة؟ (حتى لو شهر واحد)", False),
    "job_company": ("💼 اسم الشركة أو المكان اللي اشتغلت بيه\nإذا تعرف اسمها بالإنگليزي أحسن، وإذا لا اكتبه بالعربي\n"
                    "مثال: <b>Pioneer</b> أو <b>صيدلية النور</b>", False),
    "job_role": ("شنو كان عنوانك الوظيفي بهذا الشغل؟\nاختار أو اكتبه", False),
    "job_place": ("وين كان الشغل؟ (المدينة أو المنطقة)\nمثال: <b>بغداد - المنصور</b>", True),
    "job_duration": ("شگد اشتغلت بيه؟ ⏳\nاختار، أو اكتب مثل <b>8 أشهر</b> أو <b>2021 - حاليا</b>", True),
    "duties": ("شنو كانت مهامك بهذا الشغل؟ 📝\nاكتب كل مهمة بسطر، بالعربي عادي\n\n"
               "مثال:\n<b>زيارة الأطباء والصيدليات بمنطقة المنصور\nتحقيق التارگت الشهري</b>\n\n"
               "إذا تريد أكتبلك مهام قوية جاهزة اضغط تخطي", True),
    "more": ("تريد تضيف شغل ثاني؟", False),
    "customers": ("منو الزبائن اللي تعاملت وياهم بشغلك؟\nاختار كل اللي ينطبق، وبعدها اضغط <b>✔️ تم</b>", True),
    "products": ("شنو المنتجات أو التخصصات اللي سوّقتها؟ 💊\nمثال: <b>فلرات، بوتوكس</b> أو <b>مضادات حيوية</b>\n"
                 "إذا ما ينطبق اضغط تخطي", True),
    "territory": ("شنو المناطق اللي غطيتها بشغلك؟ 🗺️\nمثال: <b>الكرخ والمنصور</b>\nإذا ما ينطبق اضغط تخطي", True),
    "survey": ("سويت قبل <b>مسح ميداني</b> للأطباء أو العيادات أو الصيدليات بمدينتك؟ 📊", False),
    "licence": ("عندك إجازة سوق وسيارة؟ 🚗", False),
    "skills": ("اختار مهاراتك 💪 (تگدر تختار أكثر من وحدة)\nإذا عندك مهارة ثانية اكتبها برسالة\n"
               "وبعدها اضغط <b>✔️ تم</b>", True),
    "english": ("شگد مستواك باللغة الإنگليزية؟ 🗣️", False),
    "other_langs": ("تعرف لغات ثانية غير العربي والإنگليزي؟\nاختار، أو اكتبها برسالة، وبعدها اضغط <b>✔️ تم</b>", True),
    "courses": ("عندك دورات أو شهادات؟ 📜\nاكتب كل وحدة بسطر\nمثال: <b>دورة مهارات البيع - 2023</b>\n\n"
                "إذا ما عندك اضغط تخطي", True),
    "photo": ("دز صورة شخصية رسمية واضحة 📸، أو اضغط تخطي", True),
    # job ad
    "ad_company": ("📢 <b>نشر إعلان وظيفة</b>\n\nاسم الشركة أو الجهة", False),
    "ad_title": ("المسمى الوظيفي\nمثال: مندوب مبيعات طبي", False),
    "ad_city": ("مكان العمل (المدينة أو المنطقة)", False),
    "ad_req": ("المؤهلات والخبرة المطلوبة", False),
    "ad_resp": ("المهام الرئيسية للوظيفة", False),
    "ad_contact": ("طريقة التقديم أو التواصل\nمثال: رقم واتساب، إيميل، أو معرف تيليجرام", False),
    # contact
    "contact_msg": ("💬 اكتب رسالتك للإدارة هنا، وراح يردون عليك بهذا البوت", False),
}
EDIT_FIELDS = [  # (label, group)
    ("الاسم", "name"), ("الوظيفة", "job_title"), ("الهاتف", "phone"), ("الإيميل", "email"),
    ("المدينة", "city"), ("الدراسة", "edu"), ("الخبرة", "exp"), ("المسح الميداني", "survey"),
    ("إجازة السوق", "licence"), ("المهارات", "skills"), ("اللغات", "langs"), ("الدورات", "courses"),
    ("الصورة", "photo"),
]
EXP_GROUP = {"has_exp", "job_company", "job_role", "job_place", "job_duration", "duties", "more",
             "customers", "products", "territory"}
EDIT_CONTINUES = {"name": {"name_ok"}, "edu": {"specialty", "university", "edu_status"}, "exp": EXP_GROUP,
                  "langs": {"other_langs"}, "field_extra": {"survey", "licence"}}
EDIT_START = {"edu": "degree_level", "exp": "has_exp", "langs": "english"}
AD_ORDER = ["ad_company", "ad_title", "ad_city", "ad_req", "ad_resp", "ad_contact"]
HIGH_SCHOOL = "High School Diploma"
BACHELOR = "Bachelor's Degree"

# ================================================================ sessions
sessions = {}  # chat_id -> {"mode", "step", "data", "photo", "t", "editing", "admin_msg"}
lock = threading.Lock()
seen_updates = deque(maxlen=2000)


def new_session(chat_id, mode, step, user):
    with lock:
        sessions[chat_id] = {"mode": mode, "step": step, "data": {"tg_user": user, "experiences": []},
                             "photo": None, "t": time.time(), "editing": None, "admin_msg": None}
    return sessions[chat_id]


def cleanup():
    cutoff = time.time() - 48 * 3600
    for cid in [c for c, s in sessions.items() if s["t"] < cutoff]:
        sessions.pop(cid, None)


def cv_steps(d):
    """The numbered questions this applicant gets, in order (used for 'question X of N')."""
    field = is_field_role(d) if d.get("job_title") else True
    exp = d.get("has_experience", True)
    steps = ["name", "job_title", "phone", "email", "city", "degree_level"]
    if d.get("degree_level") != HIGH_SCHOOL:
        steps += ["specialty", "university"]
    steps += ["edu_status", "has_exp"]
    if exp:
        steps.append("job_company")
        if field:
            steps += ["customers", "products", "territory"]
    if field:
        steps += ["survey", "licence"]
    return steps + ["skills", "english", "other_langs", "courses", "photo"]


def next_step(sess, step):
    d = sess["data"]
    field = is_field_role(d)
    after_jobs = "customers" if field else "skills"
    if step == "name":
        return "name_ok" if d.get("_name_check") else "job_title"
    if step == "degree_level":
        return "edu_status" if d.get("degree_level") == HIGH_SCHOOL else "specialty"
    if step == "has_exp":
        return "job_company" if d.get("has_experience") else ("survey" if field else "skills")
    if step == "duties":
        return "more" if len(d.get("experiences") or []) < MAX_JOBS else after_jobs
    if step == "more":
        return "job_company" if d.pop("_add_more", False) else after_jobs
    return {
        "name_ok": "job_title", "name_en": "job_title", "job_title": "phone", "phone": "email", "email": "city",
        "city": "degree_level", "specialty": "university", "university": "edu_status", "edu_status": "has_exp",
        "job_company": "job_role", "job_role": "job_place", "job_place": "job_duration", "job_duration": "duties",
        "customers": "products", "products": "territory", "territory": "survey", "survey": "licence",
        "licence": "skills", "skills": "english", "english": "other_langs", "other_langs": "courses",
        "courses": "photo", "photo": "confirm",
    }[step]


# ============================================================ telegram API
def tg(method, **params):
    try:
        r = http.post(f"{API}/{method}", json=params, timeout=30)
        res = r.json()
        if not res.get("ok"):
            log.warning("telegram %s failed: %s", method, res.get("description"))
        return res
    except Exception as e:  # network hiccups must never crash the bot
        log.warning("telegram %s error: %s", method, e)
        return {"ok": False}


def send(chat_id, text, keyboard=None, remove_keyboard=False, inline=None, reply_to=None):
    params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if inline:
        params["reply_markup"] = {"inline_keyboard": inline}
    elif keyboard:
        params["reply_markup"] = {"keyboard": [[{"text": k} for k in row] for row in keyboard],
                                  "resize_keyboard": True, "one_time_keyboard": True}
    elif remove_keyboard:
        params["reply_markup"] = {"remove_keyboard": True}
    if reply_to:
        params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    return tg("sendMessage", **params)


def send_pdf(chat_id, pdf_bytes, filename, caption, inline=None):
    data = {"chat_id": str(chat_id), "caption": caption[:1024], "parse_mode": "HTML"}
    if inline:
        data["reply_markup"] = json.dumps({"inline_keyboard": inline})
    try:
        r = http.post(f"{API}/sendDocument", data=data,
                      files={"document": (filename, pdf_bytes, "application/pdf")}, timeout=60)
        res = r.json()
        if not res.get("ok"):
            log.warning("sendDocument failed: %s", res.get("description"))
        return res
    except Exception as e:
        log.warning("sendDocument error: %s", e)
        return {"ok": False}


def download_file(file_id, max_bytes=8 * 1024 * 1024):
    res = tg("getFile", file_id=file_id).get("result") or {}
    if not res.get("file_path") or (res.get("file_size") or 0) > max_bytes:
        return None
    try:
        r = http.get(f"{FILE_API}/{res['file_path']}", timeout=60)
        return r.content if r.ok else None
    except Exception:
        log.warning("photo download failed")
        return None


# ================================================================= helpers
ARABIC = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")
AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
ROLE_WORDS = re.compile(r"represent|\brep\b|pharmacist|manager|supervisor|specialist|assistant|sales|promoter|"
                        r"accountant|engineer|technician|agent|officer|executive|coordinator|cashier|intern|"
                        r"marketing|nurse|doctor|dentist|chemist|analyst|consultant|seller|trainee", re.I)
DURATION_HINT = re.compile(r"\d|month|year|week|present|now|current", re.I)


def esc(t):
    return str(t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def clean(text):
    return " ".join((text or "").replace("‏", "").replace("‎", "").split())


def split_list(text):
    parts = re.split(r"[,\n،;]+", text or "")
    return [clean(p).strip(" .-•*") for p in parts if clean(p).strip(" .-•*")]


TYPOS = {
    "unversity": "University", "univercity": "University", "universty": "University", "univesity": "University",
    "uinversity": "University", "universiti": "University", "collage": "College", "colledge": "College",
    "engeneering": "Engineering", "enginering": "Engineering", "engineerin": "Engineering", "engineeing": "Engineering",
    "pharmcy": "Pharmacy", "pharmasy": "Pharmacy", "pharmacey": "Pharmacy", "managment": "Management",
    "buisness": "Business", "bussiness": "Business", "busines": "Business", "adminstration": "Administration",
    "administation": "Administration", "represntative": "Representative", "representitive": "Representative",
    "representive": "Representative", "bachelors": "Bachelor's", "bachlor": "Bachelor", "bachelore": "Bachelor",
    "marketting": "Marketing", "medecal": "Medical", "medicl": "Medical", "biologi": "Biology", "chemestry": "Chemistry",
}
SMALL_WORDS = {"of", "in", "and", "for", "the", "at", "to", "on", "&"}
LANGUAGES = {"arabic", "english", "turkish", "kurdish", "persian", "farsi", "french", "german", "spanish", "russian",
             "italian", "chinese", "hindi", "urdu", "japanese", "korean", "armenian", "turkmen", "syriac", "hebrew"}


def tidy(text):
    """Tidies English the applicant typed: fixes common typos and capitalizes lowercase words."""
    words = []
    for i, word in enumerate(clean(text).split(" ")):
        core = word.rstrip(".,;:")
        tail = word[len(core):]
        if core.lower() in TYPOS:
            core = TYPOS[core.lower()]
        elif core.islower() and (i == 0 or core not in SMALL_WORDS):
            core = "-".join(p[:1].upper() + p[1:] for p in core.split("-"))
        words.append(core + tail)
    return " ".join(words)


def parse_language(item):
    item = clean(item)
    m = re.match(r"^([A-Za-z ]+?)\s*(?:[:\-–(]\s*)([A-Za-z ]+?)\)?$", item)
    return f"{m.group(1).strip().title()}: {m.group(2).strip().title()}" if m else tidy(item)


def parse_job(text):
    text = clean(text)
    parts = [p.strip() for p in re.split(r"\s*\|\s*", text) if p.strip()]
    if len(parts) < 2:  # people often use dashes instead of |
        parts = [p.strip() for p in re.split(r"\s+[-–—]\s+", text) if p.strip()]
    if not parts or len(parts[0]) < 2:
        return None
    job = {"company": parts[0], "title": "", "location": "", "duration": "", "duties": []}
    rest = parts[1:]
    if rest and DURATION_HINT.search(rest[-1]) and not ROLE_WORDS.search(rest[-1]):
        job["duration"] = rest.pop()
    if len(rest) >= 2:
        job["title"], job["location"] = rest[0], ", ".join(rest[1:])
    elif rest:
        job["title" if ROLE_WORDS.search(rest[0]) else "location"] = rest[0]
    job["duration"] = re.sub(r"(\d)\s*(months?|years?|weeks?)", r"\1 \2", job["duration"], flags=re.I)
    for k in ("company", "title", "location"):
        job[k] = tidy(job[k])
    job["title"] = re.sub(r"(Representative|Specialist|Supervisor|Manager|Pharmacist)s\b", r"\1", job["title"])
    return job


def user_handle(user, chat_id):
    user = user or {}
    who = f"@{user['username']}" if user.get("username") else esc(user.get("first_name", "مستخدم"))
    return f"{who} #u{chat_id}"


def tag(text):
    return re.sub(r"[^\w]+", "_", text or "", flags=re.UNICODE).strip("_")


# ============================================================ main menu
def show_menu(chat_id):
    sess = sessions.get(chat_id)
    menu = MENU
    if sess and sess["mode"] == "cv":
        label = "▶️ أكمل الدفع" if sess["step"] == "payment" else "▶️ أكمل السي في اللي بديته"
        menu = [[{"text": label, "callback_data": "m:resume"}]] + MENU
    send(chat_id, WELCOME, inline=menu)


def start_cv(chat_id, user):
    new_session(chat_id, "cv", "form", user)
    price = f"\n💰 كلفة الخدمة: <b>{PRICE_IQD:,} دينار</b>، تدفعها بالأخير." if PAID else ""
    free_text = "🤖 وإذا تحب، اكتب معلوماتك <b>براحتك بدون كليشة</b>، وأنا أفهمها.\n\n" if ai.enabled() else ""
    send(chat_id, "📝 <b>إنشاء سيرة ذاتية</b>\n\n"
                  "1️⃣ اضغط زر <b>📋 انسخ الكليشة</b> اللي تحت\n"
                  "2️⃣ الصقها برسالة جديدة، وعبّيها <b>بالعربي عادي</b>. الخانة اللي ما عندك اتركها فارغة\n"
                  "3️⃣ دزها برسالة وحدة، وأنا أرتب كلشي وأحوّله للإنگليزي ✨\n\n"
                  f"{free_text}"
                  "💼 <b>الخبرات:</b> كل شغل بسطر، مثل:\n<b>شركة النور - مندوب طبي - بغداد - سنتين</b>"
                  f"{price}", remove_keyboard=True)
    steps_btn = {"text": "🗣️ أفضّل سؤال سؤال", "callback_data": "u:steps"}
    buttons = [[steps_btn]]
    if len(TEMPLATE) <= 256:  # Telegram's limit for a copy button
        buttons = [[{"text": "📋 انسخ الكليشة", "copy_text": {"text": TEMPLATE}}], [steps_btn]]
    res = send(chat_id, esc(TEMPLATE), inline=buttons)
    if not res.get("ok") and len(buttons) > 1:  # an old Telegram app without copy buttons: long-press to copy
        send(chat_id, esc(TEMPLATE), inline=[[steps_btn]])


def start_steps(chat_id, sess):
    sess["step"], sess["fill"] = "name", False
    send(chat_id, "تمام 👍 راح أسألك سؤال سؤال. جاوب بالعربي عادي.\n❌ للإلغاء اكتب /cancel", remove_keyboard=True)
    ask(chat_id, "name")


# ============================================================ one-message form
def missing_steps(d):
    """After the one-message form: the questions that still need an answer, in order."""
    steps = []
    if not d.get("full_name"):
        steps.append("name")
    if not d.get("job_title"):
        steps.append("job_title")
    if not d.get("phone"):
        steps.append("phone")
    if not d.get("city"):
        steps.append("city")
    if not d.get("degree_level"):
        steps.append("degree_level")
    if d.get("degree_level") != HIGH_SCHOOL:
        if not d.get("specialty"):
            steps.append("specialty")
        if not d.get("university"):
            steps.append("university")
    if not d.get("edu_status"):
        steps.append("edu_status")
    if not d.get("english"):
        steps.append("english")
    if is_field_role(d):
        if d.get("survey") is None:
            steps.append("survey")
        if not d.get("licence"):
            steps.append("licence")
    if not d.get("_photo_asked"):
        steps.append("photo")
    return steps


def apply_cv_data(d, r):
    """Puts AI / form results (ai.SCHEMA shape) into the session data, English for the CV, Arabic for the summary."""
    def txt(key):
        v = r.get(key)
        return clean(v) if isinstance(v, str) and clean(v) and clean(v).lower() not in ("null", "none", "n/a") else ""

    def lst(key):
        return [clean(x) for x in (r.get(key) or []) if isinstance(x, str) and clean(x)]

    def en(key, limit=90):  # English only; anything still Arabic gets spelled in Latin letters
        v = txt(key)
        return tidy(transliterate(v) if has_ar(v) else v)[:limit]

    ar = d.setdefault("_ar", {})
    if en("full_name_en"):
        d["full_name"] = nice_name(en("full_name_en", 60))
        ar["name"] = txt("full_name_ar")
    if txt("age"):
        age = re.sub(r"\D", "", txt("age").translate(AR_DIGITS))
        if age and 15 <= int(age) <= 70:
            d["age"] = age
    if txt("phone"):
        digits = re.sub(r"[^\d+]", "", txt("phone").translate(AR_DIGITS))
        if 8 <= len(re.sub(r"\D", "", digits)) <= 15:
            d["phone"] = format_phone(digits)
    if re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", txt("email")):
        d["email"] = txt("email").lower()
    for key, field, limit in (("city", "city", 60), ("job_title", "job_title", 60), ("university", "university", 90),
                              ("products", "products", 120), ("territory", "territory", 120)):
        if en(f"{key}_en"):
            d[field] = en(f"{key}_en", limit)
            ar[field] = txt(f"{key}_ar")
    if d.get("job_title"):
        d["job_title"] = re.sub(r"(Representative|Specialist|Supervisor|Manager|Pharmacist)s\b", r"\1", d["job_title"])
    level = next((v for _, v in DEGREE_BUTTONS if v.lower() == txt("degree_level").lower()), None)
    if level:
        d["degree_level"] = level
        ar["degree_level"] = next(a for a, v in DEGREE_BUTTONS if v == level)
    if level == HIGH_SCHOOL:
        d.update(specialty="High School", degree=HIGH_SCHOOL, university="")
    elif en("specialty_en"):
        d["specialty"] = en("specialty_en", 80)
        ar["specialty"] = txt("specialty_ar")
        d["degree"] = f"{d.get('degree_level') or BACHELOR} in {d['specialty']}"
    status = txt("edu_status")
    if re.fullmatch(r"(19[7-9]\d|20[0-3]\d)", status.translate(AR_DIGITS)):
        d["edu_status"] = status.translate(AR_DIGITS)
    elif status and not has_ar(status):
        d["edu_status"] = tidy(status)[:40]
    if d.get("edu_status"):
        ar["edu_status"] = (f"متخرج سنة {d['edu_status']}" if d["edu_status"].isdigit()
                            else txt("edu_status_ar"))
    jobs = []
    for job in (r.get("experiences") or [])[:MAX_JOBS]:
        if not isinstance(job, dict):
            continue
        j = {"company": "", "title": "", "location": "", "duration": "", "duties": [], "_ar": {}}
        for k in ("company", "title", "location", "duration"):
            v = clean(job.get(f"{k}_en") or "")
            v = transliterate(v) if has_ar(v) else v
            j[k] = (v if k == "duration" else tidy(v))[:80]
            j["_ar"][k] = clean(job.get(f"{k}_ar") or "")
        if not j["company"]:
            continue
        j["title"] = re.sub(r"(Representative|Specialist|Supervisor|Manager|Pharmacist)s\b", r"\1", j["title"])
        j["duties"] = [x for x in (clean(v) for v in job.get("duties_en") or []) if x and not has_ar(x)][:5]
        j["_ar"]["duties"] = [clean(v) for v in job.get("duties_ar") or [] if clean(v)][:5]
        jobs.append(j)
    d["experiences"], d["has_experience"] = jobs, bool(jobs)
    customers = [c for c in lst("customers_en") if c in {e for _, _, e in CUSTOMER_TYPES}]
    if customers:
        d["customers"] = customers
        ar["customers"] = [a for _, a, e in CUSTOMER_TYPES if e in customers]
    if isinstance(r.get("survey"), bool):
        d["survey"] = r["survey"]
    if txt("licence") in ("car", "licence", "none"):
        d["licence"] = txt("licence")
    skills = [tidy(s)[:40] for s in lst("skills_en") if not has_ar(s)][:12]
    if skills:
        d["skills"], ar["skills"] = skills, lst("skills_ar")
    level = next((v for _, v in ENGLISH_LEVELS if v.lower() == txt("english_level").lower()), None)
    if level:
        d["english"] = level
        ar["english"] = next(a for a, v in ENGLISH_LEVELS if v == level)
    others = [tidy(x)[:30] for x in lst("other_languages_en") if not has_ar(x) and x.lower() not in ("arabic", "english")]
    if others:
        d["other_languages"] = others[:4]
    courses = [x[:90] for x in lst("courses_en") if not has_ar(x)][:8]
    if courses:
        d["courses"], ar["courses"] = courses, lst("courses_ar")
    for key in ("profile_en", "objective_en"):
        if txt(key) and not has_ar(txt(key)):
            d[key] = txt(key)[:600]


def handle_form(chat_id, sess, msg):
    d = sess["data"]
    text = (msg.get("text") or msg.get("caption") or "").strip()
    if msg.get("photo") and not text:
        send(chat_id, "📸 الصورة راح أطلبها بعدين. هسه دز معلوماتك كتابة برسالة وحدة 🙏")
        return
    if len(clean(text)) < 15:
        send(chat_id, "اكتب معلوماتك كلها برسالة وحدة حتى أرتبها 🙏\nأو اضغط <b>🗣️ أفضّل سؤال سؤال</b> فوگ.")
        return
    send(chat_id, "⏳ لحظات، دا أقرأ معلوماتك وأرتبها...", remove_keyboard=True)
    tg("sendChatAction", chat_id=chat_id, action="typing")
    data, how = ai.read_cv(text) if ai.enabled() else (None, "off")
    if data:
        d["_ai"] = True
        log.info("form read by AI (%s)", how)
    else:
        if ai.enabled():
            log.warning("AI unavailable (%s); using the form reader", how)
        fields = parse_form(text)
        if not fields:
            send(chat_id, "ما گدرت أفهم الرسالة 😅\nرجاءً انسخ الكليشة فوگ وعبّيها، "
                          "أو اضغط <b>🗣️ أفضّل سؤال سؤال</b>.",
                 inline=[[{"text": "🗣️ أفضّل سؤال سؤال", "callback_data": "u:steps"}]])
            return
        data = convert_form(fields)
        d["_mt"] = True
    apply_cv_data(d, data)
    sess["fill"], sess["fill_total"] = True, len(missing_steps(d))
    got = sum(1 for k in ("full_name", "job_title", "phone", "city", "degree", "edu_status") if d.get(k))
    jobs = len(d.get("experiences") or [])
    send(chat_id, f"✅ رتّبت معلوماتك" + (f"، ولگيت {jobs} خبرة" if jobs else "") + ".\n"
                  + ("باقي كم سؤال صغير 👇" if missing_steps(d) != ["photo"] or got < 6 else "باقي الصورة 👇"))
    go_next(chat_id, sess)





# ========================================================== CV questions
def progress(sess, step):
    if sess.get("fill"):
        total = sess.get("fill_total") or 1
        left = len(missing_steps(sess["data"]))
        return f"<b>سؤال {max(1, total - left + 1)} من {total}</b>\n" if step in missing_steps(sess["data"]) else ""
    steps = cv_steps(sess["data"])
    return f"<b>السؤال {steps.index(step) + 1} من {len(steps)}</b>\n" if step in steps else ""


def rows_of(labels, n):
    return [labels[i:i + n] for i in range(0, len(labels), n)]


KEYBOARDS = {
    "job_title": rows_of([ar for ar, _ in TITLE_BUTTONS], 2),
    "job_role": rows_of([ar for ar, _ in TITLE_BUTTONS], 2),
    "city": rows_of([ar for ar, _ in CITY_BUTTONS], 3),
    "degree_level": rows_of([ar for ar, _ in DEGREE_BUTTONS], 3),
    "edu_status": rows_of([ar for ar, _ in STAGE_BUTTONS], 2),
    "job_duration": rows_of([ar for ar, _ in DURATION_BUTTONS], 3),
    "english": rows_of([ar for ar, _ in ENGLISH_LEVELS], 3),
    "has_exp": [[YES_EXP], [NO_EXP]], "more": [[ADD_JOB], [DONE_JOBS]], "survey": [[YES_SURVEY, NO_SURVEY]],
    "licence": [[LIC_CAR], [LIC_ONLY], [LIC_NONE]], "name_ok": [[NAME_OK], [NAME_FIX]],
}


def multi_keyboard(prefix, d):
    _, options, sel_key, extra_key, empty_label = MULTI[prefix]
    selected = d.get(sel_key) or []
    rows, row = [], []
    for key, ar, _ in options:
        row.append({"text": ("✅ " if key in selected else "▫️ ") + ar, "callback_data": f"{prefix}:{key}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    anything = selected or (extra_key and d.get(extra_key))
    rows.append([{"text": "✔️ تم" if anything else empty_label, "callback_data": f"{prefix}:done"}])
    return rows


def ask(chat_id, step):
    sess = sessions.get(chat_id) or {}
    d = sess.get("data") or {}
    question, optional = QUESTIONS[step]
    if step == "name_ok":
        question = f"اسمك بالإنگليزي راح ينكتب هيچ:\n\n<b>{esc(d.get('full_name'))}</b>\n\nصحيح؟"
    if sess.get("mode") == "cv":
        jobs = len(d.get("experiences") or [])
        if step == "job_company" and jobs:
            question = f"💼 <b>الشغل رقم {jobs + 1}</b>\n" + question
        else:
            question = progress(sess, step) + question
    prefix = next((p for p, spec in MULTI.items() if spec[0] == step), None)
    if prefix:
        _, _, sel_key, extra_key, _ = MULTI[prefix]
        d[sel_key] = []
        d["_typed_" + prefix] = []
        if extra_key:
            d[extra_key] = []
        res = send(chat_id, question, inline=multi_keyboard(prefix, d))
        d["_multi_mid"] = (res.get("result") or {}).get("message_id")
    elif step in KEYBOARDS:
        send(chat_id, question, keyboard=KEYBOARDS[step] + ([[SKIP]] if optional else []))
    elif optional:
        send(chat_id, question, keyboard=[[SKIP]])
    else:
        send(chat_id, question, remove_keyboard=True)


def go_next(chat_id, sess):
    d = sess["data"]
    editing = sess.get("editing")
    if sess.get("fill") and not editing and sess["step"] != "confirm":
        if sess["step"] == "name" and d.get("_name_check"):
            nxt = "name_ok"
        else:
            todo = missing_steps(d)
            nxt = todo[0] if todo else "confirm"
        sess["step"] = nxt
        if nxt == "confirm":
            show_summary(chat_id, sess)
        else:
            ask(chat_id, nxt)
        return
    nxt = next_step(sess, sess["step"])
    if editing == "job_title" and is_field_role(d) and ("survey" not in d or "licence" not in d):
        sess["editing"], nxt = "field_extra", "survey"  # new sales role: ask the field questions too
    elif editing and nxt not in EDIT_CONTINUES.get(editing, ()):
        sess["editing"] = None
        nxt = "confirm"
    sess["step"] = nxt
    if nxt == "confirm":
        show_summary(chat_id, sess)
    else:
        ask(chat_id, nxt)


def refresh_languages(d):
    langs = ["Arabic: Native"]
    if d.get("english") and d["english"] != "none":
        langs.append(f"English: {d['english']}")
    for lang in d.get("other_languages") or []:
        if lang.split(":")[0].strip().lower() not in ("arabic", "english"):
            langs.append(lang)
    d["languages"] = langs[:6]


def line(label, ar, en):
    """'label: Arabic' and under it the English that goes on the CV (one line if they are the same)."""
    ar = "، ".join(ar) if isinstance(ar, list) else (ar or "")
    en = ", ".join(en) if isinstance(en, list) else (en or "")
    if ar and has_ar(ar):
        return f"{label} {esc(ar)}\n        🇬🇧 {esc(en)}"
    return f"{label} {esc(en)}"


def summary_text(sess):
    d = sess["data"]
    refresh_languages(d)
    ar = d.get("_ar") or {}
    lines = ["📋 <b>راجع معلوماتك</b>",
             "اللي كتبته، وتحته 🇬🇧 شلون راح ينكتب بالسي في 👇\n",
             line("👤 <b>الاسم:</b>", ar.get("name"), d.get("full_name")),
             line("🎯 <b>الوظيفة:</b>", ar.get("job_title"), d.get("job_title")),
             f"📞 <b>الهاتف:</b> {esc(d.get('phone'))}"]
    if d.get("email"):
        lines.append(f"✉️ <b>الإيميل:</b> {esc(d['email'])}")
    lines.append(line("📍 <b>السكن:</b>", ar.get("city"), d.get("city")))
    degree_ar = " - ".join(x for x in (ar.get("degree_level"), ar.get("specialty")) if x)
    lines.append(line("🎓 <b>الشهادة:</b>", degree_ar, d.get("degree")))
    if d.get("university"):
        lines.append(line("🏫 <b>الجامعة:</b>", ar.get("university"), d.get("university")))
    lines.append(line("📅 <b>الدراسة:</b>", ar.get("edu_status"), edu_status_text(d)))
    if d.get("has_experience"):
        for n, e in enumerate(d.get("experiences") or [], 1):
            jar = e.get("_ar") or {}
            lines.append(line(f"💼 <b>الشغل {n}:</b>", jar.get("company"), e.get("company")))
            details_ar = " - ".join(jar.get(k) or e.get(k) for k in ("title", "location", "duration")
                                    if jar.get(k) or e.get(k))
            details_en = " | ".join(e.get(k) for k in ("title", "location", "duration") if e.get(k))
            if details_en:
                lines.append(line("      🔹", details_ar, details_en))
            duties_ar = [x for x in jar.get("duties") or [] if has_ar(x)]
            if duties_ar:
                lines.append(f"      📝 {esc('، '.join(duties_ar))[:300]}")
            for duty in (e.get("duties") or [])[:4]:
                lines.append(f"        🇬🇧 • {esc(duty)}")
            if not e.get("duties"):
                lines.append("      • <i>راح أكتب مهام قوية مناسبة تلقائياً</i>")
        if is_field_role(d):
            if d.get("customers"):
                lines.append(line("🩺 <b>الزبائن:</b>", ar.get("customers"), d["customers"]))
            if d.get("products"):
                lines.append(line("💊 <b>المنتجات:</b>", ar.get("products"), d["products"]))
            if d.get("territory"):
                lines.append(line("🗺️ <b>المناطق:</b>", ar.get("territory"), d["territory"]))
    else:
        lines.append("💼 <b>الخبرة:</b> بدون خبرة سابقة")
    if is_field_role(d):
        lines.append("📊 <b>مسح ميداني:</b> " + ("نعم" if d.get("survey") else "لا"))
        lines.append("🚗 <b>السياقة:</b> " + {"car": "إجازة سوق + سيارة", "licence": "إجازة سوق بدون سيارة"}.get(
            d.get("licence"), "بدون إجازة سوق"))
    if d.get("skills"):
        lines.append(line("💪 <b>المهارات:</b>", ar.get("skills"), d["skills"]))
    else:
        lines.append("💪 <b>المهارات:</b> <i>راح أختارلك أقوى المهارات تلقائياً</i>")
    langs_ar = ["العربية (لغة أم)"]
    if d.get("english") and d["english"] != "none":
        langs_ar.append(f"الإنگليزية ({ar.get('english') or d['english']})")
    other_ar = ar.get("other_languages")
    if not other_ar:
        names = {en.lower(): a for _, a, en in OTHER_LANGS}
        other_ar = [names.get(x.split(":")[0].strip().lower(), x) for x in d.get("other_languages") or []]
    langs_ar += other_ar
    lines.append(line("🗣️ <b>اللغات:</b>", langs_ar, d.get("languages")))
    if d.get("courses"):
        lines.append(line("📜 <b>الدورات:</b>", ar.get("courses"), d["courses"]))
    lines.append("📸 <b>الصورة:</b> " + ("موجودة" if sess.get("photo") else "بدون صورة"))
    if d.get("age"):
        lines.insert(5, f"🎂 <b>العمر:</b> {esc(d['age'])}")
    if d.get("_ai"):
        lines.append("\n🤖 <i>رتّبت معلوماتك وكتبتها بالإنگليزي بالذكاء الاصطناعي. "
                     "تأكد منها، وإذا شي مو مضبوط اضغط ✏️ تعديل.</i>")
    elif d.get("_mt"):
        lines.append("\n🔤 <i>بعض أجوبتك انترجمت تلقائياً. إذا شي مو مضبوط اضغط ✏️ تعديل.</i>")
    text = ""
    for item in lines:  # Telegram allows 4096 characters; cut whole lines so no HTML tag is left open
        if len(text) + len(item) > 3900:
            text += "\n…"
            break
        text += item + "\n"
    return text.strip()


def show_summary(chat_id, sess):
    send(chat_id, summary_text(sess), inline=[
        [{"text": "✅ تأكيد وإرسال", "callback_data": "u:send"}],
        [{"text": "✏️ تعديل معلومة", "callback_data": "u:edit"},
         {"text": "🔄 أعيد من البداية", "callback_data": "u:restart"}],
    ])


def show_edit_menu(chat_id, sess):
    field = is_field_role(sess["data"])
    fields = [(lbl, g) for lbl, g in EDIT_FIELDS if field or g not in ("survey", "licence")]
    rows = [[{"text": lbl, "callback_data": f"e:{g}"} for lbl, g in fields[i:i + 3]] for i in range(0, len(fields), 3)]
    rows.append([{"text": "↩️ رجوع للملخص", "callback_data": "e:back"}])
    send(chat_id, "✏️ شنو المعلومة اللي تريد تعدلها؟", inline=rows)


def start_edit(chat_id, sess, group):
    d = sess["data"]
    sess["editing"] = group
    if group == "exp":
        d.update(experiences=[], customers=[], products="", territory="")
        d.pop("has_experience", None)
    sess["step"] = EDIT_START.get(group, group)
    ask(chat_id, sess["step"])


def remember(d, key, value):
    """Keeps the applicant's own words so the summary can show Arabic and English side by side."""
    d.setdefault("_ar", {})[key] = value


def note(d, how):
    """Marks the application when machine translation or transliteration was used (the admin is told)."""
    if how in ("mt", "translit", "failed"):
        d["_mt"] = True


def pick(options, text):
    """Arabic button label (or the English value itself) -> English value."""
    key = norm(text)
    for ar, en in options:
        if text in (ar, en) or key == norm(ar) or text.lower() == en.lower():
            return en
    return None


ORDINALS = {"اولى": "1st", "الاولى": "1st", "اول": "1st", "1": "1st", "ثانيه": "2nd", "الثانيه": "2nd", "ثاني": "2nd",
            "2": "2nd", "ثالثه": "3rd", "الثالثه": "3rd", "ثالث": "3rd", "3": "3rd", "رابعه": "4th", "الرابعه": "4th",
            "رابع": "4th", "4": "4th", "خامسه": "5th", "الخامسه": "5th", "خامس": "5th", "5": "5th", "سادسه": "6th",
            "السادسه": "6th", "سادس": "6th", "6": "6th", "اخيره": "Final", "الاخيره": "Final", "اخير": "Final"}


def stage_from_text(text):
    if not has_ar(text):
        return tidy(text)[:40] if re.search(r"student|year|stage", text, re.I) else None
    words = norm(text).split()
    if not any(w in ("طالب", "طالبه", "مرحله", "المرحله", "سنه", "صف", "كورس") for w in words):
        return None
    stage = next((ORDINALS[w] for w in words if w in ORDINALS), None)
    return f"{stage}-Year Student" if stage else "University Student"


def translate_lines(d, raw, limit, width):
    out = []
    lines = [clean(x).strip(" -•*") for x in raw.splitlines() if clean(x).strip(" -•*")]
    if len(lines) == 1 and len(split_list(raw)) > 1:  # all on one line, separated by commas
        lines = split_list(raw)
    for line in lines[:limit]:
        en, how = sentence_en(line)
        note(d, how)
        if en:
            out.append(en[:width])
    return out


def add_extras(chat_id, sess, raw, prefix):
    """Free-typed skills / languages while the multi-select buttons are showing."""
    d = sess["data"]
    _, _, _, extra_key, _ = MULTI[prefix]
    added = []
    items = split_list(raw)
    if prefix == "l":  # "عربي انكليزي تركي" written with spaces only
        items = [w for item in items for w in (item.split() if all(language_en(x)[1] == "dict" or not has_ar(x)
                                                                     for x in item.split()) else [item])]
    for item in items[:6]:
        en, how = (language_en if prefix == "l" else skill_en)(item)
        if prefix == "l" and en.lower() in ("arabic", "english"):
            continue  # already covered by the other questions
        note(d, how)
        added.append(tidy(en)[:40])
        d.setdefault("_typed_" + prefix, []).append(item)
    d.setdefault(extra_key, []).extend(added)
    if d.get("_multi_mid"):
        tg("editMessageReplyMarkup", chat_id=chat_id, message_id=d["_multi_mid"],
           reply_markup={"inline_keyboard": multi_keyboard(prefix, d)})
    if added:
        send(chat_id, f"✅ انضافت: {esc(', '.join(added))}\nكمّل من الأزرار فوگ، وبعدها اضغط <b>✔️ تم</b> 👆")
    else:
        send(chat_id, "👍 تمام. اختار من الأزرار فوگ إذا عندك غيرها، وبعدها اضغط <b>✔️ تم</b> 👆")


def handle_cv_answer(chat_id, sess, msg):
    step, d = sess["step"], sess["data"]
    raw = (msg.get("text") or "").replace("‏", "").replace("‎", "")
    text = clean(raw)
    skipped = text in (SKIP, "/skip", "skip", "Skip", "تخطي")

    if step == "confirm":
        send(chat_id, "اضغط <b>✅ تأكيد وإرسال</b> أو <b>✏️ تعديل</b> تحت الملخص 👆")
        return
    if step == "payment":
        handle_payment_proof(chat_id, sess, msg)
        return
    if step == "form":
        handle_form(chat_id, sess, msg)
        return
    if step == "photo":
        d["_photo_asked"] = True
        if not skipped:
            if msg.get("photo"):
                sess["photo"] = download_file(msg["photo"][-1]["file_id"])
            elif (msg.get("document") or {}).get("mime_type", "").startswith("image/"):
                sess["photo"] = download_file(msg["document"]["file_id"])
            else:
                send(chat_id, "دز <b>صورة</b> أو اضغط تخطي", keyboard=[[SKIP]])
                return
        go_next(chat_id, sess)
        return
    if not text:
        send(chat_id, "اكتب الجواب كنص رجاءً 🙏")
        return
    if step == "customers":
        send(chat_id, "اختار من الأزرار بالرسالة اللي فوگ، وبعدها اضغط <b>✔️ تم</b> 👆")
        return
    if step in ("skills", "other_langs"):
        add_extras(chat_id, sess, raw, "s" if step == "skills" else "l")
        return

    if skipped:
        if not QUESTIONS[step][1]:
            send(chat_id, "هذا السؤال ضروري، ما ينطي تخطيه 🙏")
            return
        if step in ("email", "products", "territory"):
            d[step] = ""
        elif step == "courses":
            d["courses"] = []
        go_next(chat_id, sess)  # skipped duties -> the CV adds strong standard duties
        return

    if has_ar(text):
        tg("sendChatAction", chat_id=chat_id, action="typing")
    job = (d.get("experiences") or [{}])[-1]

    if step in ("name", "name_en"):
        if len(text.split()) < 2 or len(text) > 60 or re.search(r"[\d@#/]", text) or (step == "name_en" and has_ar(text)):
            send(chat_id, "⚠️ اكتب اسمك الكامل (اسمين على الأقل)" +
                 (" بالحروف الإنگليزية، مثل <b>Ali Hassan Kareem</b>" if step == "name_en"
                  else "، مثل <b>علي حسن كريم</b>"))
            return
        if has_ar(text):
            d["full_name"], _ = name_en(text)
            d["_name_check"] = True
        else:
            d["full_name"], d["_name_check"] = nice_name(text), False
        remember(d, "name", text if has_ar(text) else "")
    elif step == "name_ok":
        if text == NAME_FIX:
            sess["step"] = "name_en"
            ask(chat_id, "name_en")
            return
        if text != NAME_OK:
            send(chat_id, "اختار من الأزرار 👇", keyboard=KEYBOARDS["name_ok"])
            return
    elif step in ("job_title", "job_role"):
        en = pick(TITLE_BUTTONS, text)
        if not en:
            if len(text) > 60:
                send(chat_id, "⚠️ اكتب المسمى الوظيفي بشكل مختصر")
                return
            en, how = title_en(text)
            note(d, how)
            en = tidy(en)
        en = re.sub(r"(Representative|Specialist|Supervisor|Manager|Pharmacist)s\b", r"\1", en)
        if step == "job_title":
            d["job_title"] = en
            remember(d, "job_title", text)
        else:
            job["title"] = en
            job.setdefault("_ar", {})["title"] = text
    elif step == "phone":
        phone = re.sub(r"[^\d+\- ]", "", text.translate(AR_DIGITS)).strip(" -")
        if not 8 <= len(re.sub(r"\D", "", phone)) <= 15 or "+" in phone[1:]:
            send(chat_id, "⚠️ الرقم مو صحيح، اكتبه مثل <b>07701234567</b>")
            return
        d["phone"] = format_phone(phone)
    elif step == "email":
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", text) or has_ar(text):
            send(chat_id, "⚠️ الإيميل مو صحيح، مثال: <b>name@gmail.com</b>\nأو اضغط تخطي", keyboard=[[SKIP]])
            return
        d["email"] = text.lower()
    elif step == "city":
        en = pick(CITY_BUTTONS, text)
        if not en:
            en, how = place_en(text)
            note(d, how)
        d["city"] = tidy(en)[:60]
        remember(d, "city", text)
    elif step == "degree_level":
        en = pick(DEGREE_BUTTONS, text) or next((v for ar, v in DEGREE_BUTTONS if norm(ar) in norm(text)), None)
        if not en:
            send(chat_id, "اختار شهادتك من الأزرار 👇", keyboard=KEYBOARDS["degree_level"])
            return
        d["degree_level"] = en
        remember(d, "degree_level", next((ar for ar, v in DEGREE_BUTTONS if v == en), text))
        if en == HIGH_SCHOOL:
            d.update(specialty="High School", degree=HIGH_SCHOOL, university="")
        elif d.get("specialty") and d["specialty"] != "High School":
            d["degree"] = f"{en} in {d['specialty']}"
    elif step == "specialty":
        en, how = specialty_en(text)
        note(d, how)
        d["specialty"] = tidy(en)[:80]
        remember(d, "specialty", text)
        level = d.get("degree_level") or "Bachelor's Degree"
        d["degree"] = f"{level} in {d['specialty']}"
    elif step == "university":
        en, how = university_en(text)
        note(d, how)
        d["university"] = tidy(en)[:90]
        remember(d, "university", text)
    elif step == "edu_status":
        val = text.translate(AR_DIGITS)
        remember(d, "edu_status", f"متخرج سنة {val}" if re.fullmatch(r"\d{4}", val) else text)
        if re.fullmatch(r"(19[7-9]\d|20[0-3]\d)", val):
            d["edu_status"] = val
        else:
            stage = pick(STAGE_BUTTONS, text) or stage_from_text(val)
            if not stage:
                send(chat_id, "⚠️ اكتب سنة التخرج 4 أرقام مثل <b>2021</b>، أو اختار مرحلتك من الأزرار",
                     keyboard=KEYBOARDS["edu_status"])
                return
            d["edu_status"] = stage
    elif step == "has_exp":
        if text not in (YES_EXP, NO_EXP):
            send(chat_id, "اختار من الأزرار 👇", keyboard=KEYBOARDS["has_exp"])
            return
        d["has_experience"] = text == YES_EXP
        if not d["has_experience"]:
            d.update(experiences=[], customers=[], products="", territory="")
    elif step == "job_company":
        parsed = parse_job(raw) if "|" in raw and not has_ar(raw) else None  # "Company | Title | City | Duration"
        if parsed and parsed.get("title"):
            d.setdefault("experiences", []).append(parsed)
            sess["step"] = "job_duration"  # next question: duties
        else:
            en, how = company_en(text)
            note(d, how)
            d.setdefault("experiences", []).append({"company": tidy(en)[:80], "title": "", "location": "", "duration": "",
                                                    "duties": [], "_ar": {"company": text}})
    elif step == "job_place":
        en, how = place_en(text)
        note(d, how)
        job["location"] = tidy(en)[:80]
        job.setdefault("_ar", {})["location"] = text
    elif step == "job_duration":
        en = pick(DURATION_BUTTONS, text)
        if not en:
            en, how = duration_en(text)
            note(d, how)
        job["duration"] = en[:40]
        job.setdefault("_ar", {})["duration"] = text
    elif step == "duties":
        job["duties"] = translate_lines(d, raw, 5, 170)
        job.setdefault("_ar", {})["duties"] = [clean(x) for x in raw.splitlines() if clean(x)][:5]
    elif step == "more":
        if text not in (ADD_JOB, DONE_JOBS):
            send(chat_id, "اختار من الأزرار 👇", keyboard=KEYBOARDS["more"])
            return
        d["_add_more"] = text == ADD_JOB
    elif step == "products":
        en, how = products_en(text)
        note(d, how)
        d["products"] = tidy(en)[:120]
        remember(d, "products", text)
    elif step == "territory":
        en, how = place_en(text)
        note(d, how)
        d["territory"] = tidy(en)[:120]
        remember(d, "territory", text)
    elif step == "survey":
        if text not in (YES_SURVEY, NO_SURVEY):
            send(chat_id, "اختار من الأزرار 👇", keyboard=KEYBOARDS["survey"])
            return
        d["survey"] = text == YES_SURVEY
    elif step == "licence":
        choice = {LIC_CAR: "car", LIC_ONLY: "licence", LIC_NONE: "none"}.get(text)
        if not choice:
            send(chat_id, "اختار من الأزرار 👇", keyboard=KEYBOARDS["licence"])
            return
        d["licence"] = choice
    elif step == "english":
        en = pick(ENGLISH_LEVELS, text)
        if not en:
            send(chat_id, "اختار من الأزرار 👇", keyboard=KEYBOARDS["english"])
            return
        d["english"] = en
        remember(d, "english", text)
    elif step == "courses":
        d["courses"] = [tidy(c) for c in translate_lines(d, raw, 8, 90)]
        remember(d, "courses", [clean(x) for x in raw.splitlines() if clean(x)][:8])
    go_next(chat_id, sess)


def latinize(value):
    """Last safety net before the PDF: nothing Arabic may reach it (the CV fonts are Latin only)."""
    if isinstance(value, str):
        return transliterate(value) if has_ar(value) else value
    if isinstance(value, list):
        return [latinize(v) for v in value]
    if isinstance(value, dict):
        return {k: v if (k == "tg_user" or k.startswith("_")) else latinize(v) for k, v in value.items()}
    return value


# =============================================================== finishing
def cv_caption(d, chat_id, status):
    jobs = len(d.get("experiences") or []) if d.get("has_experience") else 0
    tags = [f"#{tag(d.get('city')) or 'Iraq'}", f"#{tag(d.get('job_title'))}",
            "#experienced" if jobs else "#no_experience"]
    if is_student(d):
        tags.append("#student")
    if is_field_role(d) and d.get("licence") in ("car", "licence"):
        tags.append("#car" if d["licence"] == "car" else "#licence")
    if is_field_role(d) and d.get("survey"):
        tags.append("#survey")
    mt_note = (["🤖 <i>مكتوب بالذكاء الاصطناعي، راجع النص قبل الموافقة</i>"] if d.get("_ai") else
               ["🔤 <i>بيه أجوبة مترجمة تلقائياً، راجع النص قبل الموافقة</i>"] if d.get("_mt") else [])
    return "\n".join([status] + mt_note + [
        f"👤 {esc(d.get('full_name'))}",
        f"🎯 {esc(d.get('job_title'))}",
        f"📞 {esc(d.get('phone'))}",
        f"📍 {esc(d.get('city'))}",
        f"🎓 {esc(d.get('degree'))} ({esc(edu_status_text(d))})",
        f"💼 {f'{jobs} شغل سابق' if jobs else 'بدون خبرة'}",
        f"💬 {user_handle(d.get('tg_user'), chat_id)}",
        "",
        " ".join(tags + ["#cv"]),
    ])


def payment_message():
    name = f"الاسم: <b>{esc(QI_CARD_NAME)}</b>\n" if QI_CARD_NAME else ""
    return (
        "شكراً لإكمال معلوماتك 🤍\n\n"
        "خدمتنا تشمل إعداد <b>سيرة ذاتية احترافية بصيغة PDF</b>، مع إدراجك ضمن المرشحين للفرص الوظيفية "
        "المناسبة حسب مؤهلاتك ومتطلبات الشركات.\n\n"
        f"💰 تكلفة الخدمة: <b>{PRICE_IQD:,} دينار عراقي</b>\n\n"
        f"💳 حوّل على بطاقة <b>Qi Card</b>:\n{name}رقم البطاقة: <code>{esc(QI_CARD_NUMBER)}</code>\n\n"
        "📸 بعدها دز <b>صورة تأكيد الدفع</b> هنا.\n"
        "بعد التحقق من الدفع، نرسلك سيرتك الذاتية.\n\n"
        "<i>ملاحظة: ترشيح السيرة الذاتية يعتمد على توفر الفرص ومدى توافق مؤهلاتك مع متطلبات الوظيفة، "
        "ولا يضمن الحصول على مقابلة أو وظيفة.</i>"
    )


def finish_cv(chat_id, sess):
    d = sess["data"]
    refresh_languages(d)
    send(chat_id, "⏳ لحظات، جاي أجهز السي في مالتك...", remove_keyboard=True)
    tg("sendChatAction", chat_id=chat_id, action="upload_document")
    try:
        pdf = build_cv(latinize(d), sess.get("photo"))
    except Exception:
        log.exception("pdf build failed")
        send(chat_id, "صار خطأ بتجهيز الملف 😔 جرب مرة ثانية من /start")
        sessions.pop(chat_id, None)
        return
    filename = "CV_" + re.sub(r"[^A-Za-z0-9]+", "_", d.get("full_name", "cv")).strip("_") + ".pdf"

    if not ADMIN_CHAT_ID:
        sessions.pop(chat_id, None)
        send_pdf(chat_id, pdf, filename, "🎉 هذا السي في مالتك جاهز! بالتوفيق 🤍")
        return

    if PAID:
        caption = cv_caption(d, chat_id, f"⏳ <b>بانتظار إثبات الدفع ({PRICE_IQD:,} د.ع)</b>\n"
                                          "⚠️ لا توافق إلا بعد ما تتأكد من وصول المبلغ بتطبيق Qi Card")
        buttons = [[{"text": "✅ الدفع وصل، أرسل السي في", "callback_data": f"ok:{chat_id}"}],
                   [{"text": "❌ الدفع ما وصل", "callback_data": f"no:{chat_id}"}]]
    else:
        caption = cv_caption(d, chat_id, "🆕 <b>سي في جديد للمراجعة</b>")
        buttons = [[{"text": "✅ موافقة وإرسال", "callback_data": f"ok:{chat_id}"},
                    {"text": "❌ رفض", "callback_data": f"no:{chat_id}"}]]
    res = send_pdf(ADMIN_CHAT_ID, pdf, filename, caption, inline=buttons)
    if not res.get("ok"):
        log.error("could not deliver CV to admin")
        sessions.pop(chat_id, None)
        send(chat_id, "صار خطأ بالإرسال 😔 جرب مرة ثانية بعد شوية من /start")
        return
    if PAID:
        sess["mode"], sess["step"] = "cv", "payment"
        sess["admin_msg"] = res["result"]["message_id"]
        send(chat_id, payment_message())
    else:
        sessions.pop(chat_id, None)
        send(chat_id, "✅ وصلتنا معلوماتك!\n\nالسي في مالتك هسه <b>قيد المراجعة</b>، "
                      "وراح يوصلك هنا أول ما يجهز 🤍")


def forward_to_admin(chat_id, msg, header, reply_to=None):
    """Copies a user's photo/file/text to the admin with a header that carries #u<id> for replies."""
    if msg.get("text"):
        return send(ADMIN_CHAT_ID, f"{header}\n\n{esc(msg['text'])}", reply_to=reply_to)
    params = {"chat_id": ADMIN_CHAT_ID, "from_chat_id": chat_id, "message_id": msg["message_id"],
              "caption": header + (f"\n\n{esc(msg['caption'])}" if msg.get("caption") else ""), "parse_mode": "HTML"}
    if reply_to:
        params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    return tg("copyMessage", **params)


def handle_payment_proof(chat_id, sess, msg):
    is_image = msg.get("photo") or (msg.get("document") or {}).get("mime_type", "").startswith(("image/", "application/pdf"))
    if not is_image:
        send(chat_id, "📸 دز <b>صورة تأكيد الدفع</b> هنا.\nإذا عندك سؤال، اكتب /start واختار التواصل مع الإدارة.")
        return
    d = sess["data"] if sess else {}
    name = f" — {esc(d['full_name'])}" if d.get("full_name") else ""
    res = forward_to_admin(chat_id, msg, f"🧾 <b>إثبات دفع</b>{name}\n{user_handle(d.get('tg_user'), chat_id)}\n"
                                         "⚠️ تأكد من وصول المبلغ بتطبيق Qi Card قبل الموافقة",
                           reply_to=sess.get("admin_msg") if sess else None)
    sessions.pop(chat_id, None)
    if res.get("ok"):
        send(chat_id, "✅ استلمنا صورة الدفع.\nراح نتأكد من وصول المبلغ ونرسلك سيرتك الذاتية هنا 🤍")
    else:
        send(chat_id, "صار خطأ بالإرسال 😔 دز الصورة مرة ثانية")


# ================================================================= job ad
def ad_text(d):
    city = tag(d.get("ad_city"))
    footer = f"\n\n📝 تريد سيرة ذاتية احترافية؟ @{BOT_USERNAME}" if BOT_USERNAME else ""
    return (f"📢 <b>فرصة عمل: {esc(d['ad_title'])}</b>\n\n"
            f"🏢 <b>الشركة:</b> {esc(d['ad_company'])}\n"
            f"📍 <b>مكان العمل:</b> {esc(d['ad_city'])}\n\n"
            f"✅ <b>المتطلبات:</b>\n{esc(d['ad_req'])}\n\n"
            f"📋 <b>المهام:</b>\n{esc(d['ad_resp'])}\n\n"
            f"📞 <b>للتقديم:</b> {esc(d['ad_contact'])}"
            f"{footer}\n\n#وظائف" + (f" #{city}" if city else ""))


def handle_ad_answer(chat_id, sess, msg):
    step, d = sess["step"], sess["data"]
    text = (msg.get("text") or "").strip()
    if step == "ad_confirm":
        send(chat_id, "اضغط زر من الأزرار تحت الإعلان 👆")
        return
    if not text or text.startswith("/"):
        send(chat_id, "اكتب الجواب كنص رجاءً 🙏")
        return
    d[step] = text[:900 if step in ("ad_req", "ad_resp") else 150]
    i = AD_ORDER.index(step)
    if i + 1 < len(AD_ORDER):
        sess["step"] = AD_ORDER[i + 1]
        ask(chat_id, sess["step"])
        return
    sess["step"] = "ad_confirm"
    send(chat_id, "👀 <b>هذا شكل إعلانك:</b>")
    send(chat_id, ad_text(d), inline=[
        [{"text": "✅ إرسال للمراجعة", "callback_data": "a:send"}],
        [{"text": "🔄 إعادة", "callback_data": "a:restart"}, {"text": "❌ إلغاء", "callback_data": "a:cancel"}],
    ])


def submit_ad(chat_id, sess):
    sessions.pop(chat_id, None)
    if not ADMIN_CHAT_ID:
        send(chat_id, "عذراً، الخدمة مو متوفرة هسه 🙏")
        return
    preview = send(ADMIN_CHAT_ID, ad_text(sess["data"]))
    pid = (preview.get("result") or {}).get("message_id")
    send(ADMIN_CHAT_ID, f"📢 <b>إعلان وظيفة جديد للمراجعة</b>\nمن: {user_handle(sess['data'].get('tg_user'), chat_id)}",
         reply_to=pid, inline=[[{"text": "✅ نشر بالقناة", "callback_data": f"adok:{chat_id}"},
                                {"text": "❌ رفض", "callback_data": f"adno:{chat_id}"}]])
    send(chat_id, "✅ وصل إعلانك للإدارة.\nراح نبلغك هنا بعد المراجعة 🤍")


# ============================================================ callbacks
def handle_callback(cq):
    data = cq.get("data", "")
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    mid = msg.get("message_id")
    user = cq.get("from") or {}
    sess = sessions.get(chat_id)
    tg("answerCallbackQuery", callback_query_id=cq["id"])

    def drop_buttons():
        tg("editMessageReplyMarkup", chat_id=chat_id, message_id=mid)

    # ---- main menu
    if data.startswith("m:"):
        drop_buttons()
        if data == "m:resume" and sess and sess["mode"] == "cv":
            if sess["step"] == "form":
                start_cv(chat_id, user)
            elif sess["step"] == "payment":
                send(chat_id, payment_message())
            elif sess["step"] == "confirm":
                show_summary(chat_id, sess)
            else:
                ask(chat_id, sess["step"])
        elif data == "m:cv":
            start_cv(chat_id, user)
        elif data == "m:ad":
            new_session(chat_id, "ad", "ad_company", user)
            ask(chat_id, "ad_company")
        elif data == "m:contact":
            if not ADMIN_CHAT_ID:
                send(chat_id, "عذراً، الخدمة مو متوفرة هسه 🙏")
            else:
                new_session(chat_id, "contact", "contact_msg", user)
                ask(chat_id, "contact_msg")
        return

    # ---- applicant buttons (need a live session)
    if data[:2] in ("u:", "e:", "a:") or data[:2] in tuple(f"{p}:" for p in MULTI):
        if not sess:
            drop_buttons()
            send(chat_id, "⌛ هذا الزر من رسالة قديمة.\nاكتب /start إذا تريد تبدي من جديد.")
            return
        if data == "u:send" and sess["step"] == "confirm":
            drop_buttons()
            finish_cv(chat_id, sess)
        elif data == "u:edit" and sess["step"] == "confirm":
            drop_buttons()
            show_edit_menu(chat_id, sess)
        elif data == "u:restart":
            drop_buttons()
            start_cv(chat_id, user)
        elif data == "u:steps" and sess["step"] == "form":
            drop_buttons()
            start_steps(chat_id, sess)
        elif data.startswith("e:") and sess["step"] == "confirm":
            drop_buttons()
            if data == "e:back":
                show_summary(chat_id, sess)
            else:
                start_edit(chat_id, sess, data[2:])
        elif data[0] in MULTI and sess["step"] == MULTI[data[0]][0]:
            prefix, key = data.split(":", 1)
            _, options, sel_key, extra_key, _ = MULTI[prefix]
            d = sess["data"]
            selected = d.setdefault(sel_key, [])
            if key == "done":
                chosen = [en for k, _, en in options if k in selected]
                shown = [ar for k, ar, _ in options if k in selected] + ((d.get(extra_key) or []) if extra_key else [])
                tg("editMessageText", chat_id=chat_id, message_id=mid,
                   text=(msg.get("text") or "") + "\n\n✅ " + ("، ".join(shown) if shown else "تم"))
                ar_chosen = [ar for k, ar, _ in options if k in selected] + (d.get("_typed_" + prefix) or [])
                if prefix == "c":
                    d["customers"] = chosen
                    remember(d, "customers", ar_chosen)
                elif prefix == "s":
                    d["skills"] = chosen + d.get("skills_extra", [])
                    remember(d, "skills", ar_chosen)
                else:
                    d["other_languages"] = chosen + d.get("langs_extra", [])
                    remember(d, "other_languages", ar_chosen)
                go_next(chat_id, sess)
            elif any(k == key for k, _, _ in options):
                selected.remove(key) if key in selected else selected.append(key)
                tg("editMessageReplyMarkup", chat_id=chat_id, message_id=mid,
                   reply_markup={"inline_keyboard": multi_keyboard(prefix, d)})
        elif data == "a:send" and sess["step"] == "ad_confirm":
            drop_buttons()
            submit_ad(chat_id, sess)
        elif data == "a:restart":
            drop_buttons()
            new_session(chat_id, "ad", "ad_company", user)
            ask(chat_id, "ad_company")
        elif data == "a:cancel":
            drop_buttons()
            show_menu(chat_id)
        return

    # ---- admin buttons
    action, _, target = data.partition(":")
    if not (ADMIN_CHAT_ID and str(chat_id) == str(ADMIN_CHAT_ID) and target.lstrip("-").isdigit()):
        return
    target = int(target)
    if action in ("ok", "no"):
        caption = msg.get("caption") or ""
        drop_buttons()  # first, so a double tap can't send twice
        if action == "ok":
            res = tg("sendDocument", chat_id=target, document=(msg.get("document") or {}).get("file_id"),
                     caption="🎉 السي في مالتك جاهز! بالتوفيق بالتقديم 🤍")
            status = "✅ انرسل للشخص" if res.get("ok") else "⚠️ ما گدرت أدزه (يمكن الشخص حاظر البوت)"
        else:
            text = ("❌ ما گدرنا نتأكد من وصول الدفع.\nإذا حوّلت المبلغ، اكتب /start واختار التواصل مع الإدارة."
                    if PAID else "عذراً، نحتاج نراجع معلوماتك 🙏 تگدر تعيد التقديم من /start أو راح نتواصل وياك.")
            send(target, text)
            status = "❌ مرفوض"
        tg("editMessageCaption", chat_id=chat_id, message_id=mid, caption=f"{caption}\n\n{status}"[:1024])
    elif action in ("adok", "adno"):
        original = (msg.get("text") or "").split("\n\n")[0]
        drop_buttons()
        if action == "adno":
            send(target, "عذراً، إعلانك ما انقبل للنشر 🙏 للاستفسار اكتب /start واختار التواصل مع الإدارة.")
            tg("editMessageText", chat_id=chat_id, message_id=mid, text=f"{original}\n\n❌ مرفوض")
            return
        preview_id = (msg.get("reply_to_message") or {}).get("message_id")
        res = {"ok": False, "description": "CHANNEL_ID مو محدد"}
        if CHANNEL_ID and preview_id:
            res = tg("copyMessage", chat_id=CHANNEL_ID, from_chat_id=chat_id, message_id=preview_id)
        if res.get("ok"):
            send(target, "🎉 انتشر إعلانك بالقناة. بالتوفيق!")
            tg("editMessageText", chat_id=chat_id, message_id=mid, text=f"{original}\n\n✅ انتشر بالقناة")
        else:
            tg("editMessageText", chat_id=chat_id, message_id=mid,
               text=f"{original}\n\n⚠️ ما گدرت أنشر: تأكد إن CHANNEL_ID صحيح والبوت مشرف بالقناة.",
               reply_markup={"inline_keyboard": [[{"text": "🔁 جرب مرة ثانية", "callback_data": f"adok:{target}"}]]})


# ============================================================== messages
def handle_admin_reply(msg):
    """The admin replies to any bot message that carries #u<id> -> the reply goes to that person."""
    replied = msg.get("reply_to_message") or {}
    found = re.search(r"#u(\d+)", (replied.get("text") or "") + " " + (replied.get("caption") or ""))
    if not found:
        return False
    target = int(found.group(1))
    if msg.get("text"):
        res = send(target, f"📩 <b>رد الإدارة:</b>\n\n{esc(msg['text'])}")
    else:
        res = tg("copyMessage", chat_id=target, from_chat_id=msg["chat"]["id"], message_id=msg["message_id"])
    send(msg["chat"]["id"], "✅ انرسل الرد" if res.get("ok") else "⚠️ ما انرسل (يمكن الشخص حاظر البوت)",
         reply_to=msg["message_id"])
    return True


def handle_update(upd):
    if "callback_query" in upd:
        handle_callback(upd["callback_query"])
        return
    msg = upd.get("message")
    if not msg:
        return
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    is_admin = bool(ADMIN_CHAT_ID) and str(chat_id) == str(ADMIN_CHAT_ID)
    if chat.get("type") != "private" and not is_admin:
        return
    text = (msg.get("text") or "").strip()
    cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""

    if cmd == "/myid":
        send(chat_id, f"🆔 رقم المحادثة مالتك:\n<code>{chat_id}</code>")
        return
    if cmd == "/status" and is_admin:
        tg("sendChatAction", chat_id=chat_id, action="typing")
        send(chat_id, "🔧 <b>حالة البوت</b>\n"
                      f"💳 الدفع: {'مفعّل' if PAID else 'مجاني'}\n"
                      f"📢 القناة: {esc(CHANNEL_ID) or 'ما محددة'}\n"
                      f"🤖 الذكاء الاصطناعي: {esc(ai.ping())}")
        return
    if cmd in ("/start", "/menu"):
        show_menu(chat_id)
        return
    if cmd == "/cv":
        start_cv(chat_id, msg.get("from") or {})
        return
    if cmd == "/cancel":
        sessions.pop(chat_id, None)
        send(chat_id, "تم الإلغاء ✋", remove_keyboard=True)
        show_menu(chat_id)
        return
    if is_admin and not cmd and msg.get("reply_to_message") and handle_admin_reply(msg):
        return

    sess = sessions.get(chat_id)
    if sess:
        sess["t"] = time.time()
    if cmd and cmd != "/skip":
        send(chat_id, "ما أعرف هذا الأمر 🤔 اكتب /start")
    elif not sess:
        if PAID and (msg.get("photo") or msg.get("document")):
            handle_payment_proof(chat_id, None, msg)  # e.g. the server restarted while they were paying
        else:
            send(chat_id, "أهلاً 👋 اكتب /start حتى تشوف القائمة", remove_keyboard=True)
    elif sess["mode"] == "cv":
        handle_cv_answer(chat_id, sess, msg)
    elif sess["mode"] == "ad":
        handle_ad_answer(chat_id, sess, msg)
    elif sess["mode"] == "contact":
        res = forward_to_admin(chat_id, msg, f"💬 <b>رسالة من</b> {user_handle(sess['data'].get('tg_user'), chat_id)}\n"
                                             "<i>(رد على هذي الرسالة حتى يوصل ردك للشخص)</i>")
        sessions.pop(chat_id, None)
        send(chat_id, "✅ وصلت رسالتك للإدارة، وراح يردون عليك هنا قريباً 🤍" if res.get("ok")
             else "صار خطأ بالإرسال 😔 جرب مرة ثانية من /start")


# =================================================================== web
@app.get("/")
def health():
    return "CV bot is running ✅"


@app.get("/health")
def health_details():
    """Public self-check (no secrets shown): version, token, webhook, settings. Re-links the webhook if needed."""
    lines = [f"version: {(env('RENDER_GIT_COMMIT') or 'unknown')[:7]}"]
    me = tg("getMe")
    token_ok = bool(me.get("ok"))
    lines.append("bot token: " + (f"OK @{(me.get('result') or {}).get('username')}" if token_ok
                                   else f"PROBLEM ({me.get('description') or 'no answer from Telegram'})"))
    if token_ok:
        expected = f"{PUBLIC_URL}/webhook/{SECRET}"
        wh = tg("getWebhookInfo").get("result") or {}
        url = wh.get("url") or ""
        relinked = False
        if url != expected and PUBLIC_URL:
            relinked = bool(tg("setWebhook", url=expected, secret_token=SECRET, max_connections=10,
                               allowed_updates=["message", "callback_query"]).get("ok"))
        host = re.sub(r"^https?://([^/]+).*$", r"\1", url) if url else "none"
        lines.append(f"webhook: {'OK' if url == expected else 'WRONG'} (points to {host})"
                     + (" -> fixed now" if relinked else ""))
        lines.append(f"waiting updates: {wh.get('pending_update_count', 0)}")
        if wh.get("last_error_message"):
            ago = int((time.time() - (wh.get("last_error_date") or time.time())) / 60)
            lines.append(f"last delivery error: {wh['last_error_message']} ({ago} min ago)")
    lines += [f"public url: {PUBLIC_URL or 'MISSING'}",
              f"admin id: {'set' if ADMIN_CHAT_ID else 'MISSING'}",
              f"payment: {'on' if PAID else 'off (free)'}",
              f"channel: {'set' if CHANNEL_ID else 'not set'}",
              f"ai key: {'set' if ai.enabled() else 'not set'}"]
    if request.args.get("ai") == "1":
        lines.append("ai test: " + ai.ping())
    return ("<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
            "<title>CV bot check</title><pre style='font-size:16px'>" + esc("\n".join(lines)) + "</pre>")


last_sync = [0.0]


@app.get("/sync")
def sync_deploy():
    """Deploys the newest GitHub commit when this server runs an older one, so updates go live without
    the dashboard. Safe to leave public: it can only deploy the latest public commit, at most every 4 minutes."""
    hook = env("DEPLOY_HOOK")
    running = env("RENDER_GIT_COMMIT")
    repo = env("RENDER_GIT_REPO_SLUG") or "aliamjedqasim-cell/cv-bot"
    branch = env("RENDER_GIT_BRANCH") or "main"
    try:
        latest = http.get(f"https://api.github.com/repos/{repo}/commits/{branch}", timeout=15,
                          headers={"Accept": "application/vnd.github+json"}).json().get("sha") or ""
    except Exception as e:
        log.warning("github check failed: %s", e)
        latest = ""
    lines = [f"running: {running[:7] or 'unknown'}", f"latest on GitHub: {latest[:7] or 'unknown'}"]
    if not latest or not running:
        lines.append("result: could not compare versions")
    elif latest == running:
        lines.append("result: up to date")
    elif not hook.startswith("https://api.render.com/deploy/"):
        lines.append("result: DEPLOY_HOOK is not set on Render")
    elif time.time() - last_sync[0] < 240:
        lines.append("result: deploy already started, the new version is live in about 2 minutes")
    else:
        try:
            ok = http.post(hook, timeout=20).ok
        except Exception:
            ok = False
        if ok:
            last_sync[0] = time.time()
        lines.append("result: deploy started, live in about 2 minutes" if ok else "result: deploy hook failed")
    return ("<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
            "<title>CV bot update</title><pre style='font-size:16px'>" + esc("\n".join(lines)) + "</pre>")


workers = ThreadPoolExecutor(max_workers=6)
chat_locks = {}


def update_chat_id(upd):
    msg = upd.get("message") or (upd.get("callback_query") or {}).get("message") or {}
    return (msg.get("chat") or {}).get("id")


def process(upd):
    chat_id = update_chat_id(upd)
    with lock:
        chat_lock = chat_locks.setdefault(chat_id, threading.Lock())
    try:
        with chat_lock:  # a person's taps/messages are handled in order (no double CVs)
            handle_update(upd)
    except Exception:
        log.exception("update failed")


@app.post(f"/webhook/{SECRET}")
def webhook():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != SECRET:
        abort(403)
    upd = request.get_json(silent=True) or {}
    with lock:
        if upd.get("update_id") in seen_updates:  # Telegram re-sends updates if we were slow
            return "ok"
        seen_updates.append(upd.get("update_id"))
        cleanup()
        if len(chat_locks) > 5000:
            chat_locks.clear()
    if env("SYNC_UPDATES") == "1":  # used by the tests
        process(upd)
    else:
        workers.submit(process, upd)
    return "ok"


def register_webhook():
    global BOT_USERNAME
    if not TOKEN or not PUBLIC_URL:
        log.warning("BOT_TOKEN or public URL missing; webhook not registered")
        return
    for attempt in range(5):
        res = tg("setWebhook", url=f"{PUBLIC_URL}/webhook/{SECRET}", secret_token=SECRET,
                 allowed_updates=["message", "callback_query"], max_connections=10)
        if res.get("ok"):
            log.info("webhook registered (paid mode: %s)", PAID)
            BOT_USERNAME = ((tg("getMe").get("result") or {}).get("username")) or ""
            tg("setMyCommands", commands=[
                {"command": "start", "description": "القائمة الرئيسية"},
                {"command": "cv", "description": "إنشاء سيرة ذاتية"},
                {"command": "cancel", "description": "إلغاء"},
            ])
            return
        time.sleep(3 * (attempt + 1))


if env("DISABLE_WEBHOOK_SETUP") != "1":
    threading.Thread(target=register_webhook, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
