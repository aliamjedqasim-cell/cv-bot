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
MAX_JOBS = 6
ENGLISH_ONLY = "⚠️ رجاءً اكتب الجواب <b>باللغة الإنگليزية</b> حتى يطلع السي في احترافي."

JOB_TITLES = [
    ["Medical Representative", "Medical Sales Representative"],
    ["Sales Representative", "Sales Supervisor"],
    ["Marketing Specialist", "Pharmacist"],
    ["Laboratory Specialist"],
]
STUDENT_STAGES = [["2nd-Year Student", "3rd-Year Student"], ["4th-Year Student", "Final-Year Student"]]
CUSTOMER_TYPES = [("doc", "Doctors", "الأطباء"), ("cli", "Clinics", "العيادات"), ("pha", "Pharmacies", "الصيدليات"),
                  ("hos", "Hospitals", "المستشفيات"), ("shp", "Shops & Markets", "المحلات والأسواق")]

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
    "name": ("اسمك الثلاثي بالإنگليزي\nمثال: <code>Ali Hassan Kareem</code>", False),
    "job_title": ("شنو الوظيفة اللي تريد تقدم عليها؟\nاختار من الأزرار، أو اكتبها بالإنگليزي", False),
    "phone": ("رقم هاتفك الفعّال\nمثال: <code>07701234567</code>", False),
    "email": ("الإيميل\nمثال: <code>ali.hassan@gmail.com</code>\nإذا ما عندك اضغط تخطي", True),
    "city": ("المدينة اللي تسكن بيها بالإنگليزي\nمثال: <code>Baghdad</code>", False),
    "university": ("اسم الجامعة أو الكلية أو المعهد بالإنگليزي\nمثال: <code>University of Baghdad</code>", False),
    "degree": ("الشهادة والتخصص بالإنگليزي\nمثال: <code>Bachelor's Degree in Pharmacy</code>", False),
    "edu_status": ("إذا متخرج اكتب <b>سنة التخرج</b>، مثال: <code>2021</code>\n"
                   "وإذا بعدك طالب اختار مرحلتك من الأزرار 👇", False),
    "has_exp": ("عندك خبرة شغل سابقة؟ (حتى لو كانت شهر واحد)", False),
    "job": ("اكتب الشغل بهذا الشكل:\n<code>Company | Job Title | City, Area | Duration</code>\n\n"
            "مثال:\n<code>Pioneer Pharma | Medical Representative | Baghdad, Al-Mansour | 2 years</code>", False),
    "duties": ("شنو كانت مهامك بهذا الشغل؟ اكتب 2 أو 3 مهام، <b>كل مهمة بسطر</b>\n\n"
               "مثال:\n<code>Visiting doctors and pharmacies in Al-Mansour area\n"
               "Achieving monthly sales targets</code>\n\n"
               "إذا تريد أكتبلك مهام قوية مناسبة جاهزة اضغط تخطي", True),
    "more": ("تريد تضيف شغل ثاني؟", False),
    "customers": ("منو الزبائن اللي تعاملت وياهم بشغلك؟\nاختار كل اللي ينطبق، وبعدها اضغط <b>✔️ تم</b>", True),
    "products": ("شنو المنتجات أو التخصصات اللي سوّقتها؟ بالإنگليزي\n"
                 "مثال: <code>Cardiology, Antibiotics, Dermal Fillers</code>\nإذا ما ينطبق اضغط تخطي", True),
    "territory": ("شنو المدن أو المناطق اللي غطيتها بشغلك؟ بالإنگليزي\n"
                  "مثال: <code>Baghdad (Karkh, Mansour), Babylon</code>\nإذا ما ينطبق اضغط تخطي", True),
    "survey": ("سويت قبل <b>مسح ميداني (Market Survey)</b> للأطباء أو العيادات أو الصيدليات بمدينتك؟", False),
    "licence": ("عندك إجازة سوق وسيارة؟", False),
    "skills": ("اكتب مهاراتك بالإنگليزي وافصل بينها بفاصلة\n"
               "مثال: <code>Teamwork, Time Management, Negotiation Skills</code>\n\n"
               "إذا تريد أختارلك 12 مهارة قوية مناسبة لوظيفتك اضغط تخطي", True),
    "languages": ("اللغات ومستواك بيها\nمثال: <code>Arabic: Native, English: Good</code>", True),
    "courses": ("الدورات والشهادات، كل وحدة بسطر\nمثال: <code>Professional Selling Skills - 2023</code>\n\n"
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
    ("المدينة", "city"), ("الجامعة", "university"), ("الشهادة", "degree"), ("التخرج/المرحلة", "edu_status"),
    ("الخبرة", "exp"), ("المسح الميداني", "survey"), ("إجازة السوق", "licence"),
    ("المهارات", "skills"), ("اللغات", "languages"), ("الدورات", "courses"), ("الصورة", "photo"),
]
EXP_GROUP = {"has_exp", "job", "duties", "more", "customers", "products", "territory"}
EDIT_CONTINUES = {"exp": EXP_GROUP, "field_extra": {"survey", "licence"}}
AD_ORDER = ["ad_company", "ad_title", "ad_city", "ad_req", "ad_resp", "ad_contact"]

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
    """The questions this applicant gets, in order (used for 'question X of N')."""
    field = is_field_role(d) if d.get("job_title") else True
    exp = d.get("has_experience", True)
    steps = ["name", "job_title", "phone", "email", "city", "university", "degree", "edu_status", "has_exp"]
    if exp:
        steps.append("job")
        if field:
            steps += ["customers", "products", "territory"]
    if field:
        steps += ["survey", "licence"]
    return steps + ["skills", "languages", "courses", "photo"]


def next_step(sess, step):
    d = sess["data"]
    field = is_field_role(d)
    jobs = len(d.get("experiences") or [])
    after_jobs = "customers" if field else "skills"
    if step == "has_exp":
        return "job" if d.get("has_experience") else ("survey" if field else "skills")
    if step == "job":
        return "duties"
    if step == "duties":
        return "more" if jobs < MAX_JOBS else after_jobs
    if step == "more":
        return "job" if d.pop("_add_more", False) else after_jobs
    if step == "territory":
        return "survey"
    if step == "licence":
        return "skills"
    order = ["name", "job_title", "phone", "email", "city", "university", "degree", "edu_status", "has_exp",
             None, "customers", "products", "territory", None, "survey", "licence", None,
             "skills", "languages", "courses", "photo"]
    i = order.index(step)
    return next((s for s in order[i + 1:] if s), "confirm")


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


def parse_languages(raw):
    items = []
    for part in split_list(raw):
        words = part.lower().split()
        if len(words) > 1 and all(w in LANGUAGES for w in words):  # "Arabic English Turkish"
            items += [w.title() for w in words]
        else:
            items.append(parse_language(part))
    return [x[:40] for x in items][:6] or ["Arabic: Native"]


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
    new_session(chat_id, "cv", "name", user)
    price = (f"\n💰 كلفة الخدمة: <b>{PRICE_IQD:,} دينار</b>، تدفعها بعد ما تخلص الأسئلة."
             if PAID else "")
    send(chat_id, "📝 <b>إنشاء سيرة ذاتية</b>\n\n"
                  "راح أسألك أسئلة قليلة، سؤال بعد سؤال.\n"
                  "🔤 <b>جاوب على الأسئلة باللغة الإنگليزية</b> حتى يطلع السي في احترافي.\n"
                  f"✏️ بالأخير تگدر تراجع معلوماتك وتعدّلها.{price}\n"
                  "❌ للإلغاء بأي وقت اكتب /cancel", remove_keyboard=True)
    ask(chat_id, "name")


# ========================================================== CV questions
def progress(sess, step):
    steps = cv_steps(sess["data"])
    return f"<b>السؤال {steps.index(step) + 1} من {len(steps)}</b>\n" if step in steps else ""


def customers_keyboard(selected):
    rows = [[{"text": ("✅ " if key in selected else "▫️ ") + ar, "callback_data": f"c:{key}"}]
            for key, _, ar in CUSTOMER_TYPES]
    rows.append([{"text": "✔️ تم" if selected else "⏭️ تخطي", "callback_data": "c:done"}])
    return rows


def ask(chat_id, step):
    sess = sessions.get(chat_id) or {}
    question, optional = QUESTIONS[step]
    if sess.get("mode") == "cv":
        jobs = len(sess["data"].get("experiences") or [])
        if step == "job":
            question = (f"💼 <b>الشغل رقم {jobs + 1}</b>\n" if jobs else progress(sess, step)) + question
        elif step not in ("duties", "more"):
            question = progress(sess, step) + question
    keyboards = {
        "job_title": JOB_TITLES, "edu_status": STUDENT_STAGES, "has_exp": [[YES_EXP], [NO_EXP]],
        "more": [[ADD_JOB], [DONE_JOBS]], "survey": [[YES_SURVEY, NO_SURVEY]],
        "licence": [[LIC_CAR], [LIC_ONLY], [LIC_NONE]],
    }
    if step == "customers":
        sess["data"]["customers"] = []
        send(chat_id, question, inline=customers_keyboard([]))
    elif step in keyboards:
        send(chat_id, question, keyboard=keyboards[step])
    elif optional:
        send(chat_id, question, keyboard=[[SKIP]])
    else:
        send(chat_id, question, remove_keyboard=True)


def go_next(chat_id, sess):
    nxt = next_step(sess, sess["step"])
    editing = sess.get("editing")
    d = sess["data"]
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


def summary_text(sess):
    d = sess["data"]
    lines = ["📋 <b>راجع معلوماتك قبل الإرسال:</b>\n",
             f"👤 {esc(d.get('full_name'))}", f"🎯 {esc(d.get('job_title'))}", f"📞 {esc(d.get('phone'))}"]
    if d.get("email"):
        lines.append(f"✉️ {esc(d['email'])}")
    lines += [f"📍 {esc(d.get('city'))}",
              f"🎓 {esc(d.get('university'))} | {esc(d.get('degree'))} | {esc(edu_status_text(d))}"]
    if d.get("has_experience"):
        for e in d.get("experiences") or []:
            bits = " | ".join(esc(x) for x in (e.get("title"), e.get("location"), e.get("duration")) if x)
            lines.append(f"💼 {esc(e['company'])}" + (f" — {bits}" if bits else ""))
        if is_field_role(d):
            if d.get("customers"):
                lines.append(f"🩺 {esc(', '.join(d['customers']))}")
            if d.get("products"):
                lines.append(f"💊 {esc(d['products'])}")
            if d.get("territory"):
                lines.append(f"🗺️ {esc(d['territory'])}")
    else:
        lines.append("💼 بدون خبرة سابقة")
    if is_field_role(d):
        lines.append("📊 مسح ميداني: " + ("نعم" if d.get("survey") else "لا"))
        lines.append({"car": "🚗 إجازة سوق + سيارة", "licence": "🪪 إجازة سوق بدون سيارة"}.get(
            d.get("licence"), "🚫 بدون إجازة سوق"))
    lines.append(f"💪 {esc(', '.join(d.get('skills') or []))}" if d.get("skills") else "💪 مهارات قوية مختارة تلقائياً")
    lines.append(f"🗣️ {esc(', '.join(d.get('languages') or []))}")
    if d.get("courses"):
        lines.append(f"📜 {esc(', '.join(d['courses']))}")
    lines.append("📸 مع صورة" if sess.get("photo") else "📸 بدون صورة")
    return "\n".join(lines)


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
        sess["step"] = "has_exp"
    else:
        sess["step"] = group
    ask(chat_id, sess["step"])


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
    if step == "customers":
        send(chat_id, "اختار من الأزرار بالرسالة اللي فوگ، وبعدها اضغط <b>✔️ تم</b> 👆")
        return
    if step == "photo":
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

    if skipped:
        if not QUESTIONS[step][1]:
            send(chat_id, "هذا السؤال ضروري، ما ينطي تخطيه 🙏")
            return
        defaults = {"skills": [], "languages": ["Arabic: Native"], "email": "", "courses": [],
                    "products": "", "territory": ""}
        if step in defaults:
            d[step] = defaults[step]
        go_next(chat_id, sess)  # skipped duties -> the CV adds strong standard duties
        return

    # ---- answers given with buttons or numbers (Arabic allowed)
    if step == "phone":
        phone = re.sub(r"[^\d+\- ]", "", text.translate(AR_DIGITS)).strip(" -")
        if not 8 <= len(re.sub(r"\D", "", phone)) <= 15 or "+" in phone[1:]:
            send(chat_id, "⚠️ الرقم مو صحيح، اكتبه مثل <code>07701234567</code>")
            return
        d["phone"] = format_phone(phone)
    elif step == "has_exp":
        if text not in (YES_EXP, NO_EXP):
            send(chat_id, "اختار من الأزرار 👇", keyboard=[[YES_EXP], [NO_EXP]])
            return
        d["has_experience"] = text == YES_EXP
        if not d["has_experience"]:
            d.update(experiences=[], customers=[], products="", territory="")
    elif step == "more":
        if text not in (ADD_JOB, DONE_JOBS):
            send(chat_id, "اختار من الأزرار 👇", keyboard=[[ADD_JOB], [DONE_JOBS]])
            return
        d["_add_more"] = text == ADD_JOB
    elif step == "survey":
        if text not in (YES_SURVEY, NO_SURVEY):
            send(chat_id, "اختار من الأزرار 👇", keyboard=[[YES_SURVEY, NO_SURVEY]])
            return
        d["survey"] = text == YES_SURVEY
    elif step == "licence":
        choice = {LIC_CAR: "car", LIC_ONLY: "licence", LIC_NONE: "none"}.get(text)
        if not choice:
            send(chat_id, "اختار من الأزرار 👇", keyboard=[[LIC_CAR], [LIC_ONLY], [LIC_NONE]])
            return
        d["licence"] = choice
    elif step == "edu_status":
        val = text.translate(AR_DIGITS)
        if re.fullmatch(r"(19[7-9]\d|20[0-3]\d)", val):
            d["edu_status"] = val
        elif not ARABIC.search(raw) and re.search(r"student|year|stage", val, re.I):
            d["edu_status"] = tidy(val)[:40]
        else:
            send(chat_id, "⚠️ اكتب سنة التخرج 4 أرقام مثل <code>2021</code>، أو اختار مرحلتك من الأزرار",
                 keyboard=STUDENT_STAGES)
            return

    # ---- everything below is printed on the CV, so it must be English
    elif ARABIC.search(raw):
        send(chat_id, ENGLISH_ONLY)
        return
    elif step == "name":
        if len(text.split()) < 2 or not re.fullmatch(r"[A-Za-z .'\-]+", text):
            send(chat_id, "⚠️ اكتب اسمك الكامل بالإنگليزي (اسمين على الأقل)، مثل <code>Ali Hassan Kareem</code>")
            return
        d["full_name"] = nice_name(text)
    elif step == "job_title":
        if not re.fullmatch(r"[A-Za-z &/\-,.()]+", text) or len(text) > 50:
            send(chat_id, "⚠️ اختار من الأزرار أو اكتب اسم الوظيفة بالإنگليزي", keyboard=JOB_TITLES)
            return
        d["job_title"] = tidy(text)
    elif step == "email":
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", text):
            send(chat_id, "⚠️ الإيميل مو صحيح، مثال: <code>name@gmail.com</code>\nأو اضغط تخطي", keyboard=[[SKIP]])
            return
        d["email"] = text.lower()
    elif step in ("city", "university", "degree", "territory", "products"):
        d[step] = tidy(text)[:120]
    elif step == "job":
        job = parse_job(raw)
        if not job:
            send(chat_id, "⚠️ ما گدرت أفهمها. اكتبها بهذا الشكل:\n"
                          "<code>Company | Job Title | City, Area | Duration</code>")
            return
        d.setdefault("experiences", []).append(job)
    elif step == "duties":
        duties = [clean(x).strip(" -•*") for x in raw.splitlines() if clean(x).strip(" -•*")]
        if len(duties) == 1 and len(split_list(raw)) > 1:  # all on one line, separated by commas
            duties = split_list(raw)
        d["experiences"][-1]["duties"] = [x[:160] for x in duties][:5]
    elif step == "skills":
        d["skills"] = [tidy(s)[:40] for s in split_list(raw)][:12]
    elif step == "languages":
        d["languages"] = parse_languages(raw)
    elif step == "courses":
        d["courses"] = [clean(c)[:90] for c in raw.splitlines() if clean(c)][:8]
    go_next(chat_id, sess)


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
    return "\n".join([
        status,
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
    send(chat_id, "⏳ لحظات، جاي أجهز السي في مالتك...", remove_keyboard=True)
    tg("sendChatAction", chat_id=chat_id, action="upload_document")
    try:
        pdf = build_cv(d, sess.get("photo"))
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
            if sess["step"] == "payment":
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
    if data[:2] in ("u:", "e:", "c:", "a:"):
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
        elif data.startswith("e:") and sess["step"] == "confirm":
            drop_buttons()
            if data == "e:back":
                show_summary(chat_id, sess)
            else:
                start_edit(chat_id, sess, data[2:])
        elif data.startswith("c:") and sess["step"] == "customers":
            chosen = sess["data"].setdefault("customers", [])
            if data == "c:done":
                drop_buttons()
                go_next(chat_id, sess)
                return
            label = next((en for key, en, _ in CUSTOMER_TYPES if key == data[2:]), None)
            if label:
                chosen.remove(label) if label in chosen else chosen.append(label)
                keys = [k for k, en, _ in CUSTOMER_TYPES if en in chosen]
                tg("editMessageReplyMarkup", chat_id=chat_id, message_id=mid,
                   reply_markup={"inline_keyboard": customers_keyboard(keys)})
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
