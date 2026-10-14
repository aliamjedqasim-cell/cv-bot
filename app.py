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
import hmac
import io
import json
import logging
import os
import random
import re
import threading
import time
import zipfile
from collections import deque
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

import requests
from flask import Flask, abort, request

from cv_pdf import build_cv, edu_status_text, format_phone, is_field_role, is_student, nice_name
import ai
import extras
import photos
import translate
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

COMPANY = "مسار للتوظيف الطبي"
PAYMENT_WAIT = env("PAYMENT_WAIT", "خلال وقت قصير")  # e.g. "خلال ساعة"; shown after the payment photo
COMPANY_EN = "Masar Medical Careers"
PARTNER = env("PARTNER_NAME", "قناة مصطفى أمير")  # the channel we belong to (can be changed on Render)
BOT_NAME = "مسار للتوظيف الطبي | Masar"

MENU = [
    [{"text": "📝 سوّي سيرتك الذاتية CV", "callback_data": "m:cv"}],
    [{"text": "🎓 كورسات المقابلات والمندبة", "callback_data": "m:courses"}],
    [{"text": "🏢 للشركات: اطلب مندوبين أو موظفين", "callback_data": "m:ad"}],
    [{"text": "💬 تواصل ويانا", "callback_data": "m:contact"}],
]
WELCOME = (
    f"أهلاً وسهلاً بيك بـ <b>{COMPANY}</b> 🩺\n"
    f"شركة متخصصة بالتوظيف بالقطاع الطبي والمبيعات، تابعة لـ <b>{PARTNER}</b> 📢\n\n"
    "<b>شنو نقدّملك:</b>\n"
    "📝 <b>نصمملك سيرة ذاتية CV احترافية بالإنگليزي</b>، ويراجعها ويدققها فريقنا: "
    "احچي عن نفسك بالعربي، أو دز بصمة صوت 🎤\n"
    "🤝 <b>نرشّح السي في مالتك</b> للشركات اللي تطلب مندوبين وموظفين وخدمات\n"
    "🎓 <b>كورسات</b> بالمقابلات الوظيفية والمندبة الطبية\n"
    "🏢 وللشركات: <b>نوفرلكم مندوبين وموظفين</b>\n\n"
    "اختار شنو تحتاج 👇\n\n"
    "🔒 <i>معلوماتك تُستخدم فقط لإعداد سيرتك الذاتية وترشيحك لفرص عمل مناسبة، وما تنشر بشكل عام.</i>"
)
COURSE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "courses")
COURSE_PRICE = int(re.sub(r"\D", "", env("COURSE_PRICE_IQD")) or 0)  # one course; empty -> the admin tells the price
COURSES = {"interviews": "🎤 كورس المقابلات الوظيفية", "medrep": "💼 كورس المندبة الطبية والمبيعات"}


def course_lessons(course):
    """Lesson titles from courses/scripts/<course>-<n>.json, in order."""
    out = []
    for n in range(1, 20):
        path = os.path.join(COURSE_DIR, "scripts", f"{course}-{n}.json")
        if not os.path.exists(path):
            break
        try:
            with open(path, encoding="utf-8") as f:
                out.append(json.load(f).get("title") or f"الدرس {n}")
        except Exception:
            out.append(f"الدرس {n}")
    return out


def courses_text():
    lines = [f"🎓 <b>كورسات {COMPANY}</b>",
             "🎬 <b>فيديوهات تعليمية قصيرة</b> بشكل ريلز، بصوت وشرح وتصميم ديجيتال، "
             "كل درس 3 دقايق تقريباً، وتشوفها داخل البوت وترجعلها متى ما تريد.\n"]
    for key, title in COURSES.items():
        lessons = course_lessons(key)
        lines.append(f"<b>{title}</b> ({len(lessons)} دروس)")
        lines += [f"{i}. {esc(t)}" for i, t in enumerate(lessons, 1)]
        lines.append("")
    lines += ["✅ <b>شنو تحصل:</b>",
              "• شرح عملي بأمثلة من سوق العمل العراقي",
              "• 🎭 مقابلة تجريبية كاملة: تسمع الأسئلة والأجوبة بين مسؤولة التوظيف والمتقدم، ويا ملاحظات المدرب",
              "• خطوات واضحة تطبقها بالمقابلة وبالشغل الميداني",
              "• الدروس تبقى عندك بالبوت، تعيدها متى ما تريد", ""]
    lines.append(f"💰 سعر الكورس الواحد: <b>{COURSE_PRICE:,} دينار</b>" if COURSE_PRICE and PAID
                 else "💰 السعر وطريقة الدفع يبلغك بيهن أحد موظفينا بعد ما تختار.")
    lines.append("\nاختار الكورس 👇")
    return "\n".join(lines)


COURSES_TEXT = courses_text  # kept for older callers
COURSE_CHOICES = [("interviews", "🎤 كورس المقابلات"), ("medrep", "💼 كورس المندبة"), ("all", "📚 الكورسين")]
video_ids = {}  # (course, n) -> Telegram file_id after the first upload

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
    "job_role": ("شنو كان مسمّاك الوظيفي بهذا الشغل؟\nاختار أو اكتبه", False),
    "job_place": ("وين كان الشغل؟ (المدينة أو المنطقة)\nمثال: <b>بغداد - المنصور</b>", True),
    "job_duration": ("شگد اشتغلت بيه؟ ⏳\nاختار، أو اكتب مثل <b>8 أشهر</b> أو <b>2021 - حالياً</b>", True),
    "duties": ("شنو كانت مهامك بهذا الشغل؟ 📝\nاكتب كل مهمة بسطر، بالعربي عادي\n\n"
               "مثال:\n<b>زيارة الأطباء والصيدليات بمنطقة المنصور\nتحقيق التارگت الشهري</b>\n\n"
               "إذا تريد فريقنا يكتبلك مهام قوية مناسبة اضغط تخطي", True),
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
    "photo": ("📸 دز <b>صورتك الشخصية</b> (يفضّل رسمية وبخلفية هادئة).\n"
              "أعدّلها وأضبطها إلك، وتگدر تدز <b>أكثر من صورة</b> وتختار الأحلى، أو تخلي الأصلية.\n"
              "وإذا ما تريد صورة اضغط تخطي", True),
    # job ad
    "ad_company": ("🏢 <b>للشركات: طلب مندوبين أو موظفين</b>\n\n"
                   "تحتاجون مندوبين أو موظفين أو خدمات؟ اكتبولنا التفاصيل، ونرشحلكم أنسب المتقدمين من عدنا، "
                   "وننشر إعلانكم إذا تحبون ✅\n\nاسم الشركة أو الجهة", False),
    "ad_title": ("المسمى الوظيفي\nمثال: مندوب مبيعات طبي", False),
    "ad_city": ("مكان العمل (المدينة أو المنطقة)", False),
    "ad_req": ("المؤهلات والخبرة المطلوبة", False),
    "ad_resp": ("المهام الرئيسية للوظيفة", False),
    "ad_contact": ("طريقة التقديم أو التواصل\nمثال: رقم واتساب، إيميل، أو معرف تيليجرام", False),
    # contact
    "contact_msg": (f"💬 اكتب رسالتك لإدارة {COMPANY} هنا، وراح نرد عليك بهذا البوت", False),
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
stats = {"started": time.time(), "received": 0, "last_received": 0.0, "handled": 0, "failed": 0,
         "last_failure": "", "tg_failed": 0, "last_tg_failure": ""}  # shown on /health, no personal data


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
def tg(method, _timeout=30, **params):
    try:
        r = http.post(f"{API}/{method}", json=params, timeout=_timeout)
        res = r.json()
        if not res.get("ok"):
            log.warning("telegram %s failed: %s", method, res.get("description"))
            if method not in ("deleteMessage", "editMessageReplyMarkup", "answerCallbackQuery"):
                stats["tg_failed"] += 1
                stats["last_tg_failure"] = f"{method}: {str(res.get('description'))[:150]} ({time.strftime('%H:%M')} UTC)"
        return res
    except Exception as e:  # network hiccups must never crash the bot
        log.warning("telegram %s error: %s", method, e)
        stats["tg_failed"] += 1
        stats["last_tg_failure"] = f"{method}: {e.__class__.__name__} ({time.strftime('%H:%M')} UTC)"
        return {"ok": False}


def send(chat_id, text, keyboard=None, remove_keyboard=False, inline=None, reply_to=None, silent=False):
    params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if silent:
        params["disable_notification"] = True
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


def cv_intro():
    """The two messages that open a CV: instructions, then the form with a one-tap copy button."""
    price = f"\n💰 كلفة الخدمة: <b>{PRICE_IQD:,} دينار</b>، تدفعها بالأخير." if PAID else ""
    free_text = "✍️ وإذا تحب، اكتب معلوماتك <b>براحتك بدون كليشة</b>، وإحنا نرتبها.\n\n" if ai.enabled() else ""
    intro = ("📝 <b>إنشاء سيرة ذاتية</b>\n\n"
             "1️⃣ اضغط زر <b>📋 انسخ الكليشة</b> اللي تحت\n"
             "2️⃣ الصقها برسالة جديدة، وعبّيها <b>بالعربي عادي</b>. الخانة اللي ما عندك اتركها فارغة\n"
             "3️⃣ دزها برسالة وحدة، وفريقنا يرتب كلشي ويكتبه بالإنگليزي ✨\n\n"
             f"{free_text}"
             "💼 <b>الخبرات:</b> كل شغل بسطر، مثل:\n<b>شركة النور - مندوب طبي - بغداد - سنتين</b>"
             f"{price}")
    steps_btn = {"text": "🗣️ أفضّل سؤال سؤال", "callback_data": "u:steps"}
    buttons = [[steps_btn]]
    if len(TEMPLATE) <= 256:  # Telegram's limit for a copy button
        buttons = [[{"text": "📋 انسخ الكليشة", "copy_text": {"text": TEMPLATE}}], [steps_btn]]
    return intro, esc(TEMPLATE), buttons, [[steps_btn]]


TALK_INTRO = (
    "📝 <b>إنشاء سيرة ذاتية</b>\n\n"
    "احچيلي عن نفسك <b>برسالة وحدة، بالعربي عادي وبدون ترتيب</b>:\n"
    "👤 اسمك، رقم هاتفك، ومحافظتك\n"
    "🎯 الوظيفة اللي تريدها\n"
    "🎓 دراستك: الشهادة، التخصص، الجامعة، وسنة التخرج أو المرحلة\n"
    "💼 خبراتك إذا عندك: وين، شنو كان شغلك، وشگد\n"
    "➕ أي شي ثاني يفيد: مهارات، دورات، لغات، إجازة سوق...\n\n"
    "🎤 تگدر <b>تدز بصمة صوت</b> بدل الكتابة\n"
    "📄 أو دز <b>السي في القديم</b> مالتك (PDF أو Word)\n\n"
    "ونرتب كلشي ونكتبه بالإنگليزي باحتراف ✨، ونسألك <b>بس عن الناقص</b>.\n\n"
    "💬 <b>مثال:</b>\n<i>اني علي حسن كريم من بغداد الكرادة، رقمي 07701234567، خريج صيدلة جامعة بغداد 2022، "
    "اشتغلت سنتين مندوب علمي بشركة بيونير أزور الأطباء والصيدليات، أريد شغل مندوب طبي، الإنگليزي جيد وعندي سيارة</i>"
)


def start_cv(chat_id, user):
    if not ai.enabled():  # no AI key: the copyable form, read by form.py
        new_session(chat_id, "cv", "form", user)
        intro, form_text, buttons, plain_buttons = cv_intro()
        send(chat_id, intro, remove_keyboard=True)
        res = send(chat_id, form_text, inline=buttons)
        if not res.get("ok") and buttons != plain_buttons:  # an old Telegram app without copy buttons
            send(chat_id, form_text, inline=plain_buttons)
        return
    sess = new_session(chat_id, "cv", "talk", user)
    sess["talk"], sess["rounds"] = [], 0
    price = f"\n\n💰 كلفة الخدمة: <b>{PRICE_IQD:,} دينار</b>، تدفعها بالأخير." if PAID else ""
    send(chat_id, TALK_INTRO + price, remove_keyboard=True, inline=[
        [{"text": "📋 أفضّل أعبّي كليشة", "callback_data": "u:form"},
         {"text": "🗣️ أفضّل سؤال سؤال", "callback_data": "u:steps"}]])


def send_form(chat_id):
    _, form_text, buttons, plain_buttons = cv_intro()
    send(chat_id, "انسخ الكليشة، عبّيها، ودزها برسالة وحدة 👇 (الخانة اللي ما عندك اتركها فارغة)")
    buttons = [row for row in buttons if not any(b.get("callback_data") == "u:steps" for b in row)] or None
    res = send(chat_id, form_text, inline=buttons)
    if not res.get("ok"):
        send(chat_id, form_text)


def start_steps(chat_id, sess):
    sess["step"], sess["fill"] = "name", False
    send(chat_id, "تمام 👍 راح نسألك سؤال سؤال. جاوب بالعربي عادي.\n❌ للإلغاء اكتب /cancel", remove_keyboard=True)
    ask(chat_id, "name")


# ============================================================ one-message form
def missing_steps(d):
    """After the one-message form / AI conversation: the questions that still need an answer, in order.
    Things the AI conversation already asked once (d["_asked"]) are not asked again."""
    asked = set(d.get("_asked") or [])
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
        if not d.get("university") and "university" not in asked:
            steps.append("university")
    if not d.get("edu_status") and "edu_status" not in asked:
        steps.append("edu_status")
    if not d.get("english") and "english" not in asked:
        steps.append("english")
    if is_field_role(d):
        if d.get("survey") is None and not d.get("_talk"):
            steps.append("survey")
        if not d.get("licence") and "licence" not in asked:
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
    achievements = [x[:200] for x in lst("achievements_en") if not has_ar(x)][:5]
    if achievements:
        d["achievements"], ar["achievements"] = achievements, lst("achievements_ar")
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
        send(chat_id, "📸 الصورة نطلبها منك بعدين. هسه دز معلوماتك كتابة برسالة وحدة 🙏")
        return
    if len(clean(text)) < 15:
        send(chat_id, "اكتب معلوماتك كلها برسالة وحدة حتى نرتبها 🙏\nأو اضغط <b>🗣️ أفضّل سؤال سؤال</b> فوگ.")
        return
    send(chat_id, "⏳ لحظات، دا نقرأ معلوماتك...", remove_keyboard=True)
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
            send(chat_id, "ما گدرنا نقرأ الرسالة 😅\nرجاءً انسخ الكليشة فوگ وعبّيها، "
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





# ================================================================ photos
def receive_photo(chat_id, sess, file_id):
    """Edits the photo (face-centred crop, light correction) and shows it with its choices.
    Several photos can be sent: each one gets its own preview and buttons."""
    raw = download_file(file_id, max_bytes=15 * 1024 * 1024)
    try:
        edited, face = photos.edit_photo(raw) if raw else (None, False)
    except Exception:
        edited, face = None, False
    if not edited:
        send(chat_id, "ما گدرنا نفتح الصورة 😅 دز وحدة ثانية (صورة عادية، مو ملف)")
        return False
    options = sess.setdefault("photo_options", [])
    if len(options) >= 6:
        options.pop(0)
    options.append({"orig": raw, "edited": edited})
    n = len(options)
    if sess.get("step") == "talk":  # the conversation is not stopped: the edited one is used unless they change it
        sess["photo"], sess["data"]["_photo_asked"] = edited, True
    note = "قصّيتها على الوجه وضبطت الإضاءة والوضوح" if face else "ضبطت القص والإضاءة والوضوح"
    try:
        r = http.post(f"{API}/sendPhoto", timeout=60, data={
            "chat_id": str(chat_id), "parse_mode": "HTML",
            "caption": f"📸 <b>الصورة {n} بعد التعديل</b>: {note} ✨\n"
                       "إذا عندك صورة ثانية دزها هم، واختار اللي تعجبك 👇",
            "reply_markup": json.dumps({"inline_keyboard": [
                [{"text": f"✅ اعتمد الصورة {n}", "callback_data": f"p:e:{n - 1}"}],
                [{"text": "🖼️ الأصلية بدون تعديل", "callback_data": f"p:o:{n - 1}"}]]})},
            files={"photo": ("photo.jpg", edited, "image/jpeg")}).json()
        return bool(r.get("ok"))
    except Exception:
        return False


def choose_photo(chat_id, sess, kind, index):
    options = sess.get("photo_options") or []
    if not 0 <= index < len(options):
        send(chat_id, "⌛ هذي الصورة قديمة، دزها مرة ثانية 🙏")
        return
    sess["photo"] = options[index]["edited" if kind == "e" else "orig"]
    sess["data"]["_photo_asked"] = True
    send(chat_id, "✅ اعتمدت " + ("الصورة المعدّلة" if kind == "e" else "الصورة الأصلية") + " للسي في")
    if sess["step"] == "photo":
        go_next(chat_id, sess)
    elif sess["step"] == "confirm":
        show_summary(chat_id, sess)


# ======================================================= AI conversation
TALK_OPTIONAL = {  # asked only together with something else that is missing (never a question on its own)
    "achievements": "🏆 إنجازاتك بالشغل إذا عندك، بالأرقام الحقيقية: مثلاً حققت التارگت بنسبة كذا، فتحت مناطق أو صيدليات جديدة، دربت مندوبين، أو ترقيت لمشرف",
    "email": "✉️ إيميلك (الشركات تفضّل أكثر من وسيلة تواصل)",
}
TALK_ASK = {  # what is missing -> how the AI conversation asks for it (all in one message)
    "name": "👤 اسمك الثلاثي",
    "phone": "📞 رقم هاتفك",
    "job_title": "🎯 شنو الوظيفة اللي تريدها؟ (مثلاً مندوب طبي، صيدلاني، مندوب مبيعات)",
    "city": "📍 محافظتك ومنطقتك",
    "degree": "🎓 شهادتك وتخصصك (مثلاً بكالوريوس صيدلة، دبلوم تحليلات، أو سادس إعدادي)",
    "university": "🏛️ اسم الجامعة أو الكلية أو المعهد",
    "edu_status": "📅 سنة تخرجك، أو إذا بعدك طالب شگد مرحلتك",
    "experience": "💼 عندك خبرة شغل؟ إذا اي: وين، شنو كان شغلك، وشگد. وإذا ماكو اكتب <b>ماكو</b>",
    "english": "🇬🇧 مستوى الإنگليزي عندك (ممتاز، جيد جداً، جيد، متوسط، ضعيف)",
    "licence": "🚗 عندك إجازة سوق وسيارة؟",
}
ESSENTIAL = ("name", "phone", "job_title", "city", "degree")
CONFIRM_WORDS = {"تمام", "اوكي", "اوك", "ok", "okay", "صح", "صحيح", "كلشي صحيح", "كلشي تمام", "ارسل", "دز", "دزه",
                 "ارسله", "موافق", "تم", "اي", "نعم", "yes", "send", "تمام دزه", "تمام ارسل", "عاشت ايدك"}


def talk_missing(d, first_round):
    out = []
    if not d.get("full_name"):
        out.append("name")
    if not d.get("phone"):
        out.append("phone")
    if not d.get("job_title"):
        out.append("job_title")
    if not d.get("city"):
        out.append("city")
    if not d.get("degree_level") or (d["degree_level"] != HIGH_SCHOOL and not d.get("specialty")):
        out.append("degree")
    if first_round:
        if d.get("degree_level") != HIGH_SCHOOL and not d.get("university"):
            out.append("university")
        if not d.get("edu_status"):
            out.append("edu_status")
        if not d.get("experiences") and d.get("has_experience") is not False:
            out.append("experience")
        if not d.get("english"):
            out.append("english")
        if is_field_role(d) and not d.get("licence"):
            out.append("licence")
    return out


def conversation_text(sess):
    parts = []
    for turn in sess.get("talk") or []:
        if turn.get("q"):
            parts.append("سؤال البوت: " + re.sub(r"<[^>]+>", "", turn["q"]))
        kind = {"voice": "جواب الشخص (بصمة صوت مكتوبة آلياً)", "file": "نص السي في القديم اللي دزه الشخص",
                "edit": "تصحيح من الشخص على الملخص"}.get(turn.get("kind"), "جواب الشخص")
        parts.append(f"{kind}: {turn['a']}")
    return "\n\n".join(parts)


GENERIC_WORDS = {"جامعه", "الجامعه", "كليه", "الكليه", "معهد", "المعهد", "شركه", "الشركه", "مؤسسه", "مكتب",
                 "صيدليه", "مجموعه", "university", "college", "institute", "company", "co", "of", "the", "al",
                 "group", "pharmacy", "for", "and", "في", "من", "على", "او", "و", "ال"}
JOB_PHRASES = ("مندوب طبي", "مندوبه طبيه", "مندوب علمي", "مندوب مبيعات طبي", "مندوب مبيعات", "شغل طبي",
               "قطاع طبي", "القطاع الطبي", "مجال طبي", "المجال الطبي", "شركه ادويه", "شركات الادويه", "مستلزمات طبيه",
               "اجهزه طبيه", "مواد طبيه", "medical representative", "medical rep", "medical sales")
STUDY_WORDS = re.compile(r"كليه|جامعه|معهد|تخصص|خريج|طالب|بكالوريوس|بكلوريوس|دبلوم|دراس|شهاده|مرحله|college|university|degree|bachelor|diploma|faculty|graduat", re.I)


def _hay(text):
    return norm(text or "").lower()


def _said(hay, ar=None, en=None, exclude=()):
    """True if the applicant's own words contain this fact (whole phrase, or every distinctive word)."""
    for v in (ar, en):
        v = _hay(v)
        if not v:
            continue
        if v in hay:
            return True
        words = [w for w in re.findall(r"[\w']+", v) if w not in GENERIC_WORDS and w not in exclude and len(w) >= 2]
        if words and all(w in hay for w in words):
            return True
    return False


def ground_ai(data, said_text):
    """HR rejects a CV with a wrong degree, year or employer: anything important the AI returned that the
    applicant never said is removed (then the bot simply asks for it). Returns the dropped fields."""
    hay = _hay(said_text)
    digits = set(re.findall(r"\d+", hay))
    city_words = set(re.findall(r"[\w']+", _hay(data.get("city_ar")) + " " + _hay(data.get("city_en"))))
    dropped = []
    sp_ar, sp_en = data.get("specialty_ar"), data.get("specialty_en")
    if sp_ar or sp_en:
        # what they studied must be said apart from their job ("مندوب طبي" is not a medical degree)
        study_hay = hay
        for phrase in [data.get("job_title_ar")] + [j.get("title_ar") for j in data.get("experiences") or []
                                                    if isinstance(j, dict)] + list(JOB_PHRASES):
            phrase = _hay(phrase)
            if len(phrase) >= 3:
                study_hay = study_hay.replace(phrase, " ")
        if not _said(study_hay, sp_ar, sp_en):
            data["specialty_ar"] = data["specialty_en"] = None
            dropped.append("specialty")
    if (data.get("university_ar") or data.get("university_en")) and not _said(
            hay, data.get("university_ar"), data.get("university_en"), exclude=city_words):
        data["university_ar"] = data["university_en"] = None
        dropped.append("university")
    year = re.fullmatch(r"(19|20)\d{2}", str(data.get("edu_status") or "").strip())
    if year and year.group(0) not in digits:
        data["edu_status"] = None
        dropped.append("edu_status")
    jobs = []
    for j in data.get("experiences") or []:
        if not isinstance(j, dict):
            continue
        if not _said(hay, j.get("company_ar"), j.get("company_en"), exclude=city_words):
            dropped.append("company")
            continue
        j["duties_en"] = [x for x in j.get("duties_en") or [] if set(re.findall(r"\d+", str(x))) <= digits]
        jobs.append(j)
    data["experiences"] = jobs
    data["achievements_en"] = [x for x in data.get("achievements_en") or []
                               if isinstance(x, str) and set(re.findall(r"\d+", x)) <= digits]
    if dropped:
        log.info("dropped facts the applicant did not say: %s", dropped)
    return dropped


def said_text(sess):
    """Only the applicant's own words (not the bot's questions, which can repeat what the AI understood)."""
    return "\n".join(t["a"] for t in sess.get("talk") or [])


def merge_ai(d, data):
    """Puts a fresh AI reading of the whole conversation into the session; keeps what the AI left empty."""
    fresh = {"experiences": []}
    apply_cv_data(fresh, data)
    for k, v in fresh.items():
        if k == "_ar":
            d.setdefault("_ar", {}).update({kk: vv for kk, vv in v.items() if vv})
        elif k in ("experiences", "has_experience"):
            continue
        elif v not in (None, "", []):
            d[k] = v
    if fresh.get("degree_level") == HIGH_SCHOOL:
        d.update(specialty="High School", degree=HIGH_SCHOOL, university="")
    if fresh.get("experiences"):
        d["experiences"], d["has_experience"] = fresh["experiences"], True
    elif data.get("has_experience") is False:
        d["experiences"], d["has_experience"] = [], False
        d["customers"], d["products"], d["territory"] = [], "", ""
    d["_ai"], d["_talk"] = True, True


def understood_line(d):
    ar = d.get("_ar") or {}
    bits = [ar.get("job_title") or d.get("job_title"), ar.get("city") or d.get("city")]
    if d.get("degree_level") == HIGH_SCHOOL:
        bits.append("إعدادية")
    elif d.get("specialty"):
        bits.append(ar.get("specialty") or d["specialty"])
    jobs = len(d.get("experiences") or [])
    if jobs:
        bits.append(f"{jobs} خبرة" if jobs > 2 else ("خبرتين" if jobs == 2 else "خبرة وحدة"))
    return "، ".join(esc(b) for b in bits if b)


def first_name(d):
    ar_name = ((d.get("_ar") or {}).get("name") or "").split()
    return ar_name[0] if ar_name else (d.get("full_name") or "").split(" ")[0]


def file_text(doc, data):
    """Text of an old CV the applicant sent (PDF or Word)."""
    name = (doc.get("file_name") or "").lower()
    mime = doc.get("mime_type") or ""
    try:
        if mime == "application/pdf" or name.endswith(".pdf"):
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            return "\n".join((page.extract_text() or "") for page in reader.pages[:4])
        if name.endswith(".docx") or "wordprocessingml" in mime:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                xml = z.read("word/document.xml").decode("utf-8", "ignore")
            xml = re.sub(r"</w:p>|<w:br/>|<w:tab/>", "\n", xml)
            return re.sub(r"<[^>]+>", "", xml).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    except Exception as e:
        log.warning("old CV could not be read: %s", e)
    return ""


def voice_text(chat_id, msg):
    """Voice note / audio -> text (Groq Whisper). Returns None when there is no voice in the message."""
    media = msg.get("voice") or msg.get("audio") or msg.get("video_note")
    if not media:
        return None
    if not ai.enabled():
        send(chat_id, "🎤 البصمات ما مفعّلة هسه، اكتب جوابك رجاءً 🙏")
        return ""
    if (media.get("duration") or 0) > 300:
        send(chat_id, "🎤 البصمة طويلة هواية، خليها أقل من ٥ دقايق رجاءً 🙏")
        return ""
    tg("sendChatAction", chat_id=chat_id, action="typing")
    audio = download_file(media["file_id"], max_bytes=20 * 1024 * 1024)
    text, how = ai.transcribe(audio, "voice.ogg" if msg.get("voice") else (media.get("file_name") or "audio.mp3"))
    if not text:
        log.warning("voice not understood: %s", how)
        send(chat_id, "🎤 ما گدرنا نسمع البصمة زين 😅 عيدها بصوت أوضح، أو اكتب رجاءً.")
        return ""
    send(chat_id, "🎤 سمعتك: <i>" + esc(text[:350]) + ("..." if len(text) > 350 else "") + "</i>")
    return text


def handle_talk(chat_id, sess, msg, kind="text"):
    """The AI conversation: the applicant talks freely; the AI reads everything said so far; the bot asks
    once, in one message, for what is still missing, and then only essential gaps by buttons."""
    d = sess["data"]
    raw = (msg.get("text") or msg.get("caption") or "").replace("\u200f", "").replace("\u200e", "")
    text = "\n".join(clean(x) for x in raw.splitlines() if clean(x))  # keep the lines (forms, lists)
    doc = msg.get("document") or {}
    if msg.get("photo") or (doc.get("mime_type") or "").startswith("image/"):
        receive_photo(chat_id, sess, msg["photo"][-1]["file_id"] if msg.get("photo") else doc["file_id"])
        if not text:
            if not sess.get("talk") and not msg.get("media_group_id"):
                send(chat_id, "هسه احچيلي عن نفسك (كتابة أو بصمة 🎤) 👆")
            return
    elif doc:
        data = download_file(doc["file_id"], max_bytes=10 * 1024 * 1024)
        text = "\n".join(clean(x) for x in file_text(doc, data or b"").splitlines() if clean(x))[:6000]
        if len(text) < 30:
            send(chat_id, "📄 ما گدرنا نقرأ الملف 😅 دزه <b>PDF</b> أو <b>Word</b>، أو اكتب معلوماتك، أو دز بصمة 🎤")
            return
        kind = "file"
        send(chat_id, "📄 وصلني السي في القديم، دا أقرأه...")
    if len(text) < (2 if sess.get("talk") else 12):
        send(chat_id, "احچيلي عن نفسك أكثر 🙏 اسمك، دراستك، شغلك، والوظيفة اللي تريدها.\n"
                      "🎤 أو دز بصمة صوت، أسهل.")
        return
    sess.setdefault("talk", []).append({"q": sess.pop("last_q", None), "a": text[:4000], "kind": kind})
    if kind != "file":
        send(chat_id, "⏳ لحظات، دا نقرأ معلوماتك...", remove_keyboard=True)
    tg("sendChatAction", chat_id=chat_id, action="typing")
    data, how = ai.read_cv(conversation_text(sess))
    if not data:
        log.warning("AI conversation failed (%s); falling back", how)
        fields = parse_form(text)
        if fields:
            apply_cv_data(d, convert_form(fields))
            d["_mt"] = True
            send(chat_id, "✅ رتّبت معلوماتك. باقي كم سؤال صغير 👇")
        else:
            send(chat_id, "النظام عليه ضغط هسه 😅 راح نكمل وياك بأسئلة سريعة.")
        sess["fill"], sess["fill_total"] = True, len(missing_steps(d))
        sess["step"] = "talk_done"
        go_next(chat_id, sess)
        return
    log.info("conversation read by AI (%s)", how)
    ground_ai(data, said_text(sess))
    merge_ai(d, data)
    first_round = sess.get("rounds", 0) == 0
    todo = talk_missing(d, first_round)
    if todo and sess.get("rounds", 0) < 2:
        sess["rounds"] = sess.get("rounds", 0) + 1
        d["_asked"] = sorted(set(d.get("_asked") or []) | set(todo))
        name = first_name(d)
        got = understood_line(d)
        question = ((f"✅ تمام{' ' + esc(name) if name else ''}، وصلتنا معلوماتك" + (f": {got}" if got else "") + ".\n\n")
                    + ("باقي عندي بس هاي الشغلات، جاوب عليها <b>برسالة وحدة</b> (كتابة أو بصمة 🎤):\n\n" if len(todo) > 1
                       else "باقي شغلة وحدة 👇\n\n")
                    + "\n".join(TALK_ASK[k] for k in todo))
        optional = [k for k in ("achievements", "email") if first_round and k not in (d.get("_asked") or [])
                    and ((k == "achievements" and d.get("experiences") and not d.get("achievements"))
                         or (k == "email" and not d.get("email")))]
        if optional:
            d["_asked"] = sorted(set(d["_asked"]) | set(optional))
            question += "\n\n<b>وحتى يصير السي في أقوى (اختياري):</b>\n" + "\n".join(TALK_OPTIONAL[k] for k in optional)
        sess["last_q"] = question
        send(chat_id, question)
        return
    # the rest: only essential gaps, one by one with buttons; then the photo and the summary
    sess["fill"], sess["step"] = True, "talk_done"
    sess["fill_total"] = len(missing_steps(d))
    rest = missing_steps(d)
    if rest and rest != ["photo"]:
        send(chat_id, "✅ تمام، باقي كم شغلة صغيرة بالأزرار 👇")
    elif rest == ["photo"]:
        send(chat_id, "✅ تمام" + (f" {esc(first_name(d))}" if first_name(d) else "") + "، رتّبت كل معلوماتك 👌")
    go_next(chat_id, sess)


def handle_ai_edit(chat_id, sess, text, kind="edit"):
    """At the summary: a typed (or spoken) correction is applied by the AI; a short 'ok' sends the CV."""
    if norm(text).strip(" .!؟?") in {norm(w) for w in CONFIRM_WORDS}:
        finish_cv(chat_id, sess)
        return
    sess.setdefault("talk", []).append({"q": "راجع الملخص، إذا أكو شي غلط اكتب التصحيح", "a": text[:1500],
                                        "kind": "edit"})
    send(chat_id, "⏳ دا أعدّل...")
    tg("sendChatAction", chat_id=chat_id, action="typing")
    data, how = ai.read_cv(conversation_text(sess))
    if not data:
        sess["talk"].pop()
        send(chat_id, "ما گدرنا نعدّل هسه 😅 استخدم زر <b>✏️ تعديل معلومة</b> تحت الملخص.")
        return
    ground_ai(data, said_text(sess))
    merge_ai(sess["data"], data)
    send(chat_id, "✅ عدّلتها")
    show_summary(chat_id, sess)


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
                lines.append("      • <i>فريقنا يكتبلك مهام قوية مناسبة لوظيفتك</i>")
        if is_field_role(d):
            if d.get("customers"):
                lines.append(line("🩺 <b>الزبائن:</b>", ar.get("customers"), d["customers"]))
            if d.get("products"):
                lines.append(line("💊 <b>المنتجات:</b>", ar.get("products"), d["products"]))
            if d.get("territory"):
                lines.append(line("🗺️ <b>المناطق:</b>", ar.get("territory"), d["territory"]))
    else:
        lines.append("💼 <b>الخبرة:</b> بدون خبرة سابقة")
    if d.get("achievements"):
        lines.append(line("🏆 <b>الإنجازات:</b>", ar.get("achievements"), d["achievements"]))
    if is_field_role(d):
        if d.get("survey") is not None or not d.get("_talk"):
            lines.append("📊 <b>مسح ميداني:</b> " + ("نعم" if d.get("survey") else "لا"))
        if d.get("licence") or not d.get("_talk"):
            lines.append("🚗 <b>السياقة:</b> " + {"car": "إجازة سوق + سيارة", "licence": "إجازة سوق بدون سيارة"}.get(
                d.get("licence"), "بدون إجازة سوق"))
    if d.get("skills"):
        lines.append(line("💪 <b>المهارات:</b>", ar.get("skills"), d["skills"]))
    else:
        lines.append("💪 <b>المهارات:</b> <i>فريقنا يختارلك أقوى المهارات المناسبة لوظيفتك</i>")
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
    if d.get("_talk") and ai.enabled():
        lines.append("\n📋 <i>هاي معلوماتك مثل ما راح تنكتب بالسي في. راجعها زين، لأن فريقنا راح يشتغل عليها.</i>\n"
                     "✍️ إذا أكو شي غلط، <b>اكتب التصحيح بكلامك</b> هنا، مثل: <i>غيّر المحافظة للبصرة</i>، "
                     "أو دز بصمة 🎤. وإذا كلشي تمام اضغط <b>✅ تأكيد وإرسال للفريق</b>.")
    elif d.get("_ai"):
        lines.append("\n📋 <i>هاي معلوماتك مثل ما راح تنكتب بالسي في. "
                     "تأكد منها، وإذا شي مو مضبوط اضغط ✏️ تعديل.</i>")
    elif d.get("_mt"):
        lines.append("\n🔤 <i>راجع شلون انكتبت معلوماتك بالإنگليزي، وإذا شي مو مضبوط اضغط ✏️ تعديل.</i>")
    text = ""
    for item in lines:  # Telegram allows 4096 characters; cut whole lines so no HTML tag is left open
        if len(text) + len(item) > 3900:
            text += "\n…"
            break
        text += item + "\n"
    return text.strip()


def show_summary(chat_id, sess):
    send(chat_id, summary_text(sess), inline=[
        [{"text": "✅ تأكيد وإرسال للفريق", "callback_data": "u:send"}],
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
    kind = "text"
    if step not in ("photo", "payment"):
        spoken = voice_text(chat_id, msg)  # a voice note works as an answer anywhere
        if spoken is not None:
            if not spoken:
                return
            msg, kind = {**msg, "text": spoken}, "voice"
    if step == "talk":
        handle_talk(chat_id, sess, msg, kind)
        return
    raw = (msg.get("text") or "").replace("‏", "").replace("‎", "")
    text = clean(raw)
    skipped = text in (SKIP, "/skip", "skip", "Skip", "تخطي")

    if step == "confirm":
        if msg.get("photo") or ((msg.get("document") or {}).get("mime_type") or "").startswith("image/"):
            receive_photo(chat_id, sess, msg["photo"][-1]["file_id"] if msg.get("photo") else msg["document"]["file_id"])
            return
        if text and d.get("_talk") and ai.enabled():
            handle_ai_edit(chat_id, sess, text)
            return
        send(chat_id, "اضغط <b>✅ تأكيد وإرسال للفريق</b> أو <b>✏️ تعديل</b> تحت الملخص 👆")
        return
    if step == "payment":
        handle_payment_proof(chat_id, sess, msg)
        return
    if step == "form":
        handle_form(chat_id, sess, msg)
        return
    if step == "photo":
        if skipped:
            sess["photo"] = None
            d["_photo_asked"] = True
            go_next(chat_id, sess)
        elif msg.get("photo") or ((msg.get("document") or {}).get("mime_type") or "").startswith("image/"):
            receive_photo(chat_id, sess, msg["photo"][-1]["file_id"] if msg.get("photo") else msg["document"]["file_id"])
        elif sess.get("photo_options") and norm(text) in {norm(w) for w in CONFIRM_WORDS | {"كمل", "اكمل"}}:
            choose_photo(chat_id, sess, "e", len(sess["photo_options"]) - 1)
        else:
            send(chat_id, ("اختار من الأزرار تحت الصورة 👆، أو دز صورة ثانية، أو اضغط تخطي"
                           if sess.get("photo_options") else "دز <b>صورة</b> 📸 أو اضغط تخطي"), keyboard=[[SKIP]])
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
        f"خدمة <b>{COMPANY}</b> تشمل إعداد <b>سيرة ذاتية احترافية بصيغة PDF</b>، مع ترشيحك للشركات "
        "اللي تطلب مندوبين وموظفين حسب مؤهلاتك ومتطلباتها.\n\n"
        f"💰 كلفة الخدمة: <b>{PRICE_IQD:,} دينار عراقي</b>\n\n"
        f"{GIFT_TEASER}\n\n"
        f"💳 حوّل على بطاقة <b>Qi Card</b>:\n{name}رقم البطاقة: <code>{esc(QI_CARD_NUMBER)}</code>\n\n"
        "📸 بعدها دز <b>صورة تأكيد الدفع</b> هنا.\n"
        f"بعد تأكيد الدفع، فريقنا يجهز السي في مالتك ويراجعه ويدققه، ويوصلك ويا الهدايا هنا <b>{CV_WAIT}</b>.\n\n"
        "<i>ملاحظة: ترشيح السيرة الذاتية يعتمد على توفر الفرص ومدى توافق مؤهلاتك مع متطلبات الوظيفة، "
        "ولا يضمن الحصول على مقابلة أو وظيفة.</i>"
    )


GIFT_TEASER = (
    "🎁 <b>ويا السي في تستلم هدايا مجانية:</b>\n"
    "📄 <b>نسخة ATS</b> ثانية للتقديم على المواقع والشركات اللي تفرز السيفيات إلكترونياً\n"
    "✉️ <b>رسالة تغطية Cover Letter</b> بالإنگليزي مكتوبة إلك\n"
    "🎤 <b>تحضير للمقابلة</b>: أهم الأسئلة لوظيفتك ويا أجوبة مقترحة من خبرتك"
)
FALLBACK_INTERVIEW = [
    ("عرّفنا بنفسك", "ابدي باسمك ودراستك، بعدها خبرتك أو أقوى مهارة عندك، واختم بليش تريد هذي الوظيفة. خليها دقيقة وحدة."),
    ("ليش تريد تشتغل بهذي الوظيفة؟", "اربط جوابك بشي حقيقي عندك: حبك للتعامل ويا الناس، اهتمامك بالمجال الطبي، أو خبرتك السابقة."),
    ("شنو تعرف عن شركتنا ومنتجاتنا؟", "قبل المقابلة ابحث عن الشركة ومنتجاتها، واذكر شي واحد عجبك بيها."),
    ("شلون تتعامل ويا طبيب أو صيدلي رافض المنتج؟", "اسمع اعتراضه للآخر، اسأله عن السبب، وجاوب بفائدة المنتج اللي تهمه، وخلّي الباب مفتوح لزيارة ثانية."),
    ("شلون تحقق التارگت الشهري؟", "خطة زيارات أسبوعية، تركيز على الزبائن الأكثر تأثير، ومتابعة يومية للأرقام."),
    ("احچيلي عن موقف صعب واجهته وشلون حليته", "اختار موقف حقيقي: شنو صار، شنو سويت، وشنو كانت النتيجة."),
    ("شنو نقاط قوتك وضعفك؟", "اذكر قوة مرتبطة بالشغل، وضعف حقيقي بسيط ويا شلون دا تطوّر نفسك بيه."),
    ("شگد تتوقع راتبك؟", "اسأل عن مدى الراتب للوظيفة إذا تگدر، وإذا لا اذكر رقم معقول وگول إنك مرن حسب المميزات."),
]
FALLBACK_TIPS = ["البس رسمي ونظيف", "تعال قبل الموعد بـ10 دقايق", "جيب نسختين مطبوعة من السي في",
                 "بعد المقابلة دز رسالة شكر قصيرة للشركة"]
gifts_by_user = {}      # chat_id -> admin message ids of the gifts (backup when a button has none)
pending_payments = {}   # chat_id -> {"t": time the payment photo came, "msg": admin message id, "name": ...}


def gift_facts(d):
    """The true CV facts the AI writes the cover letter and interview answers from."""
    keep = ("full_name", "job_title", "city", "degree", "university", "edu_status", "profile_en", "skills",
            "achievements", "courses", "languages", "customers", "products", "territory", "licence")
    out = {k: d.get(k) for k in keep if d.get(k)}
    out["experiences"] = [{k: e.get(k) for k in ("company", "title", "location", "duration", "duties") if e.get(k)}
                          for e in d.get("experiences") or [] if e.get("company")]
    return latinize(out)


def interview_text(d, data):
    rows = [(x.get("q"), x.get("a")) for x in (data or {}).get("interview") or [] if isinstance(x, dict)]
    rows = [(q, a) for q, a in rows if q and a][:10] or FALLBACK_INTERVIEW
    tips = [t for t in (data or {}).get("tips_ar") or [] if isinstance(t, str) and t.strip()][:5] or FALLBACK_TIPS
    job = (d.get("_ar") or {}).get("job_title") or d.get("job_title") or ""
    text = f"🎁 <b>تحضير المقابلة</b>{' لوظيفة ' + esc(job) if job else ''} 🎤\n\n"
    for i, (q, a) in enumerate(rows, 1):
        item = f"<b>{i}. {esc(q)}</b>\n💡 {esc(a)}\n\n"
        if len(text) + len(item) > 3500:
            break
        text += item
    return text + "<b>نصايح سريعة:</b>\n" + "\n".join(f"• {esc(t)}" for t in tips)


def make_gifts(chat_id, d, reply_to):
    """ATS CV + cover letter (as one album) and the interview preparation, kept silently in the admin chat
    under the CV, so they reach the buyer on approval even if the server restarts in between."""
    try:
        data, how = ai.write_extras(gift_facts(d)) if ai.enabled() else (None, "off")
        letter = (data or {}).get("cover_letter_en") or ""
        letter = "" if has_ar(letter) else letter
        clean_d = latinize(d)
        base = re.sub(r"[^A-Za-z0-9]+", "_", d.get("full_name", "cv")).strip("_")
        ats, cover = extras.build_ats_cv(clean_d), extras.build_cover_letter(clean_d, letter)
        media = [{"type": "document", "media": "attach://ats",
                  "caption": "🎁 هدية: نسخة ATS من السي في، للتقديم على المواقع والشركات اللي تفرز إلكترونياً"},
                 {"type": "document", "media": "attach://cover",
                  "caption": "🎁 هدية: رسالة تغطية Cover Letter، ترفقها ويا السي في"}]
        r = http.post(f"{API}/sendMediaGroup", timeout=60, data={
            "chat_id": str(ADMIN_CHAT_ID), "media": json.dumps(media), "disable_notification": "true",
            "reply_parameters": json.dumps({"message_id": reply_to, "allow_sending_without_reply": True})},
            files={"ats": (f"ATS_CV_{base}.pdf", ats, "application/pdf"),
                   "cover": (f"Cover_Letter_{base}.pdf", cover, "application/pdf")}).json()
        ids = [m["message_id"] for m in r.get("result") or []]
        qa = send(ADMIN_CHAT_ID, interview_text(d, data), reply_to=reply_to, silent=True)
        if qa.get("ok"):
            ids.append(qa["result"]["message_id"])
        log.info("gifts ready (%s): %s", how, ids)
        return ids
    except Exception:
        log.exception("gifts failed")
        return []


SAMPLE_CV = {
    "full_name": "Ahmed Kareem Hassan", "job_title": "Medical Representative", "phone": "+964 770 000 0000",
    "email": "ahmed.sample@email.com", "city": "Baghdad", "degree_level": BACHELOR, "specialty": "Pharmacy",
    "degree": "Bachelor's Degree in Pharmacy", "university": "University of Baghdad", "edu_status": "2021",
    "has_experience": True, "customers": ["Doctors", "Pharmacies", "Private Clinics"], "territory": "Rusafa",
    "products": "Cardiology and Diabetes Products", "licence": "car",
    "experiences": [{"company": "Sample Pharma Co.", "title": "Medical Representative", "location": "Baghdad",
                     "duration": "2 years", "duties": [
                         "Visited 12-15 doctors and pharmacies daily to present cardiology and diabetes products.",
                         "Achieved 110% of the monthly sales target for three consecutive quarters.",
                         "Built strong relationships with key prescribers across the Rusafa territory."]},
                    {"company": "City Community Pharmacy", "title": "Pharmacist", "location": "Baghdad",
                     "duration": "1 year", "duties": [
                         "Advised patients on correct medicine use and dosing.",
                         "Managed stock levels and supplier orders."]}],
    "achievements": ["Ranked first among new representatives in sales growth (2023)."],
    "skills": ["Strong Communication Skills", "Negotiation Skills", "Product Presentation",
               "Territory Planning", "Customer Relationship Management", "Time Management"],
    "english": "Very Good", "courses": ["Professional Selling Skills", "Pharmaceutical Marketing Basics"],
    "profile_en": ("Pharmacist and Medical Representative with three years of experience in pharmacy practice and "
                   "field sales in Baghdad. Proven record of exceeding sales targets and building trusted "
                   "relationships with doctors and pharmacies."),
}
sample_cache = {}


def send_sample(chat_id):
    """Before payment: a finished CV of an invented person, as a sample of the team's work (not their own CV)."""
    caption = (f"🖼️ <b>نموذج من شغل فريق {COMPANY}</b> (لشخص افتراضي)\n"
               "السي في مالتك يتصمم بنفس المستوى: بمعلوماتك وصورتك، وصفحة وحدة مرتبة 👌")
    try:
        if sample_cache.get("id"):
            r = tg("sendPhoto", chat_id=chat_id, photo=sample_cache["id"], caption=caption, parse_mode="HTML")
            if r.get("ok"):
                return True
        if not sample_cache.get("img"):
            refresh_languages(SAMPLE_CV)
            sample_cache["img"] = extras.sample_image(build_cv(dict(SAMPLE_CV), extras.sample_photo()))
        if not sample_cache["img"]:
            return False
        r = http.post(f"{API}/sendPhoto", timeout=60, data={"chat_id": str(chat_id), "caption": caption, "parse_mode": "HTML"},
                      files={"photo": ("sample_cv.jpg", sample_cache["img"], "image/jpeg")}).json()
        if r.get("ok"):
            sample_cache["id"] = ((r.get("result") or {}).get("photo") or [{}])[-1].get("file_id")
        return bool(r.get("ok"))
    except Exception:
        log.exception("sample CV failed")
        return False


def send_preview(chat_id, pdf):
    """The teaser before payment: their own CV, top sharp, the rest blurred."""
    try:
        img = extras.preview_image(pdf)
    except Exception:
        img = None
    if not img:
        return False
    try:
        r = http.post(f"{API}/sendPhoto", timeout=60, data={
            "chat_id": str(chat_id), "parse_mode": "HTML",
            "caption": "👀 <b>هذي معاينة للمسودة الأولى من السي في مالتك</b>\n"
                       "بعد الدفع، فريقنا يراجعها ويدققها، والنسخة النهائية بجودة عالية توصلك هنا ✅"},
            files={"photo": ("preview.jpg", img, "image/jpeg")}).json()
        return bool(r.get("ok"))
    except Exception:
        return False


def remind_admin(chat_id, minutes):
    """While a buyer waits after paying, the admin gets a reminder (10 and 30 minutes)."""
    p = pending_payments.get(chat_id)
    if not p or not ADMIN_CHAT_ID:
        return
    what = p.get("what", "السي في")
    send(ADMIN_CHAT_ID, f"⏰ <b>تذكير:</b> {esc(p.get('name') or 'متقدم')} دافع وينتظر {what} من {minutes} دقيقة.\n"
                        "تأكد من التحويل بتطبيق Qi Card واضغط ✅ تحت رسالة الدفع مالته.", reply_to=p.get("msg"))


def finish_cv(chat_id, sess):
    d = sess["data"]
    refresh_languages(d)
    tg("sendChatAction", chat_id=chat_id, action="typing")
    try:
        pdf = build_cv(latinize(d), sess.get("photo"))
    except Exception:
        log.exception("pdf build failed")
        send(chat_id, "صار خطأ بتجهيز الملف 😔 جرّب مرة ثانية من /start")
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
        buttons = [[{"text": "✅ الدفع وصل، وافق", "callback_data": f"ok:{chat_id}"}],
                   [{"text": "❌ الدفع ما وصل", "callback_data": f"no:{chat_id}"}]]
    else:
        caption = cv_caption(d, chat_id, "🆕 <b>سي في جديد للمراجعة</b>")
        buttons = [[{"text": "✅ موافقة", "callback_data": f"ok:{chat_id}"},
                    {"text": "❌ رفض", "callback_data": f"no:{chat_id}"}]]
    res = send_pdf(ADMIN_CHAT_ID, pdf, filename, caption, inline=buttons)
    if not res.get("ok"):
        log.error("could not deliver CV to admin")
        sessions.pop(chat_id, None)
        send(chat_id, "صار خطأ بالإرسال 😔 جرّب مرة ثانية بعد شوية من /start")
        return
    admin_msg = res["result"]["message_id"]
    receipt = (f"✅ <b>استلمنا طلبك</b>\n🧾 رقم الطلب: <b>#M{admin_msg}</b>\n\n"
               f"👩‍💼 معلوماتك هسه عند فريق {COMPANY}. "
               + ("بعد تأكيد الدفع، موظفينا يجهزون السي في مالتك ويراجعونه ويدققونه، ويوصلك هنا."
                  if PAID else "موظفينا يراجعون طلبك، وبعدها يجهزون السي في مالتك ويوصلك هنا.")
               + "\n\n" + order_status(None, 1))
    send(chat_id, receipt, remove_keyboard=True)
    send_sample(chat_id)
    if PAID:
        sess["mode"], sess["step"] = "cv", "payment"
        sess["admin_msg"] = admin_msg
        send(chat_id, payment_message())
    else:
        sessions.pop(chat_id, None)
        send(chat_id, GIFT_TEASER)
    ids = make_gifts(chat_id, d, admin_msg)  # the buyer already has their message; this runs meanwhile
    if ids:
        gifts_by_user[chat_id] = ids
        if len(gifts_by_user) > 3000:
            gifts_by_user.pop(next(iter(gifts_by_user)))
        tag_ids = ":".join(str(i) for i in ids)
        rows = [[{"text": b["text"], "callback_data": b["callback_data"] + (f":{tag_ids}" if b["callback_data"].startswith("ok:") else "")}
                 for b in row] for row in buttons]
        tg("editMessageReplyMarkup", chat_id=ADMIN_CHAT_ID, message_id=admin_msg, reply_markup={"inline_keyboard": rows})


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
        send(chat_id, "📸 دز <b>صورة تأكيد الدفع</b> هنا.\nإذا عندك سؤال، اكتب /start واختار 💬 تواصل ويانا.")
        return
    d = sess["data"] if sess else {}
    name = f" — {esc(d['full_name'])}" if d.get("full_name") else ""
    res = forward_to_admin(chat_id, msg, f"🧾 <b>إثبات دفع</b>{name}\n{user_handle(d.get('tg_user'), chat_id)}\n"
                                         "⚠️ تأكد من وصول المبلغ بتطبيق Qi Card قبل الموافقة",
                           reply_to=sess.get("admin_msg") if sess else None)
    sessions.pop(chat_id, None)
    if res.get("ok"):
        pending_payments[chat_id] = {"t": time.time(), "msg": (res.get("result") or {}).get("message_id"),
                                     "name": d.get("full_name")}
        for minutes in (10, 30):
            timer = threading.Timer(minutes * 60, remind_admin, args=(chat_id, minutes))
            timer.daemon = True
            timer.start()
        send(chat_id, "✅ <b>استلمنا صورة الدفع، شكراً إلك 🤍</b>\n\n"
                      "👩‍💼 هسه موظف من فريقنا دا يتأكد من وصول المبلغ، وبعدها نبدي نجهز السي في مالتك "
                      "ونراجعه وندققه.\n\n"
                      "راح يوصلك هنا:\n📄 السي في مالتك\n🎁 ويا الهدايا: نسخة ATS، رسالة تغطية، وتحضير المقابلة\n\n"
                      + order_status(sess.get("admin_msg") if sess else None, 1)
                      + "\n\nما تحتاج تسوي شي ثاني، بس خلي إشعارات البوت مفتوحة 🔔")
    else:
        send(chat_id, "صار خطأ بالإرسال 😔 دز الصورة مرة ثانية")


# ======================================================= delivery (the admin decides when)
# After approval the CV waits with the admin: the buyer is told our team is preparing the final copy, and the
# admin picks when it goes out (now, in a few hours, or at the next opening time) with buttons under the CV.
# Waiting and scheduled CVs live in memory and in one pinned message in the admin chat, so a restart or deploy
# loses nothing.
def _minutes(name, default):
    raw = re.sub(r"\D", "", os.environ.get(name, ""))
    return int(raw) if raw else default


def _work_hours():
    m = [int(x) for x in re.findall(r"\d+", os.environ.get("WORK_HOURS", ""))]
    return (m[0], m[1]) if len(m) >= 2 and 0 <= m[0] < m[1] <= 24 else (10, 22)


DELIVERY_MODE = os.environ.get("DELIVERY_MODE", "manual").strip().lower()  # "now" = send right after approval
CV_WAIT = env("CV_WAIT", "خلال 24 ساعة")  # what the buyer is told about the waiting time
CV_DELAY = _minutes("CV_DELAY_MINUTES", 180) or 180  # the "in a few hours" button
REMIND_HOURS = _minutes("CV_REMIND_HOURS", 4)  # a waiting CV the admin has not decided on -> one reminder (0 = off)
WORK_START, WORK_END = _work_hours()  # e.g. WORK_HOURS=10-22, Baghdad time
BAGHDAD = timezone(timedelta(hours=3))
SCHEDULE_HEAD = "📌 جدول تسليم السي في"
# admin CV message id -> {"u": user, "t": send time (0 = waiting for the admin), "r": reminder time,
#                         "g": gift ids, "f": file id, "cap": admin caption, "n": name}
deliveries = {}
schedule = {"msg": None, "loaded": False, "text": ""}
schedule_lock = threading.RLock()
STAGES_PAID = ["استلام المعلومات", "تأكيد الدفع", "تجهيز ومراجعة السي في من فريقنا", "التسليم ويا الهدايا"]
STAGES_FREE = ["استلام المعلومات", "مراجعة وموافقة فريقنا", "تجهيز السي في", "التسليم ويا الهدايا"]


def order_status(order, stage):
    """The buyer's order tracker: done steps ✅, the current one ⏳, the rest ▫️."""
    stages = STAGES_PAID if PAID else STAGES_FREE
    rows = [("✅" if i < stage else "⏳" if i == stage else "▫️") + " " + name for i, name in enumerate(stages)]
    head = f"📋 <b>مراحل طلبك</b>" + (f" <b>#M{order}</b>" if order else "") + ":"
    return head + "\n" + "\n".join(rows)


def next_opening(now=None):
    """The next start of working hours (Baghdad), a few minutes after the hour."""
    local = datetime.fromtimestamp(now or time.time(), BAGHDAD)
    opening = local.replace(hour=WORK_START, minute=0, second=0, microsecond=0)
    if local >= opening:
        opening += timedelta(days=1)
    return opening.timestamp() + random.randint(5, 40) * 60


def in_work_hours(at):
    """at, or the next opening time if at falls outside working hours."""
    local = datetime.fromtimestamp(at, BAGHDAD)
    if WORK_START <= local.hour < WORK_END:
        return at
    return next_opening(at)


def delivery_time(now=None):
    """The 'in a few hours' button: about CV_DELAY minutes, moved to the next morning if that is after hours."""
    now = now or time.time()
    return in_work_hours(now + CV_DELAY * 60 * random.uniform(0.9, 1.1))


def hours_text(minutes):
    h = round(minutes / 60)
    return "ساعة" if h <= 1 else "ساعتين" if h == 2 else f"{h} ساعات" if h <= 10 else f"{h} ساعة"


def when_text(at, now=None):
    """How the buyer is told when the CV arrives (the team's estimate, never an exact minute)."""
    if not at:
        return CV_WAIT
    now = now or time.time()
    a, n = datetime.fromtimestamp(at, BAGHDAD), datetime.fromtimestamp(now, BAGHDAD)
    hours = (at - now) / 3600
    if hours < 0.75:
        return "خلال وقت قصير"
    if a.date() == n.date():
        if n.hour < WORK_START and a.hour < 12:
            return "اليوم الصبح إن شاء الله"
        return f"خلال {hours_text(hours * 60)} تقريباً" if hours < 10.5 else "اليوم إن شاء الله"
    if (a.date() - n.date()).days == 1:
        return "باچر الصبح إن شاء الله" if a.hour < 12 else "باچر إن شاء الله"
    return "بأقرب وقت إن شاء الله"


def clock(at, now=None):
    """Baghdad time for the admin: «اليوم 3:40 م», «باچر 10:15 ص»."""
    a, n = datetime.fromtimestamp(at, BAGHDAD), datetime.fromtimestamp(now or time.time(), BAGHDAD)
    days = (a.date() - n.date()).days
    day = "اليوم" if days == 0 else "باچر" if days == 1 else a.strftime("%d/%m")
    return f"{day} {a.hour % 12 or 12}:{a.minute:02d} {'ص' if a.hour < 12 else 'م'}"


def load_schedule():
    """After a restart: read the waiting and scheduled CVs back from the pinned message in the admin chat."""
    with schedule_lock:
        if schedule["loaded"] or not ADMIN_CHAT_ID:
            return
        r = tg("getChat", chat_id=ADMIN_CHAT_ID)
        if not r.get("ok"):
            return  # try again on the next round
        schedule["loaded"] = True
        pin = (r.get("result") or {}).get("pinned_message") or {}
        text = pin.get("text") or ""
        if not text.startswith(SCHEDULE_HEAD):
            return
        schedule["msg"] = pin.get("message_id")
        m = re.search(r"(\[.*\])\s*$", text, re.S)
        try:
            for row in json.loads(m.group(1)) if m else []:
                u, t, k, g = row[:4]
                deliveries.setdefault(int(k), {"u": int(u), "t": float(t), "g": [int(x) for x in g],
                                               "r": float(row[4]) if len(row) > 4 else 0})
        except Exception:
            log.exception("could not read the delivery schedule")
        log.info("delivery schedule loaded: %d waiting", len(deliveries))


def save_schedule():
    if not ADMIN_CHAT_ID:
        return
    with schedule_lock:
        load_schedule()  # never overwrite a list that was not read back yet
        items = sorted(deliveries.items(), key=lambda kv: (kv[1]["t"] or 9e12, kv[0]))
        state = json.dumps([[v["u"], int(v["t"]), k, v.get("g") or [], int(v.get("r") or 0)] for k, v in items],
                           separators=(",", ":"))
        rows = [f"• {esc(v.get('n') or 'متقدم')} ← "
                + (datetime.fromtimestamp(v["t"], BAGHDAD).strftime("%d/%m %H:%M") if v["t"] else "ينتظر قرارك")
                for k, v in items[:20]] + ([f"… و{len(items) - 20} غيرهم"] if len(items) > 20 else [])
        text = (f"<b>{SCHEDULE_HEAD}</b>\n<i>البوت يحدّث هاي الرسالة وحده ويرجعلها إذا انعاد تشغيله، خليها مثبتة.</i>\n\n"
                + ("\n".join(rows) if rows else "ماكو سي في بالانتظار ✅") + f"\n\n<code>{esc(state)}</code>")
        if text == schedule["text"]:
            return
        if schedule["msg"]:
            r = tg("editMessageText", chat_id=ADMIN_CHAT_ID, message_id=schedule["msg"], text=text, parse_mode="HTML")
            if r.get("ok") or "not modified" in str(r.get("description", "")):
                schedule["text"] = text
                return
        if not items:
            return
        r = send(ADMIN_CHAT_ID, text, silent=True)
        if r.get("ok"):
            schedule["msg"], schedule["text"] = r["result"]["message_id"], text
            tg("pinChatMessage", chat_id=ADMIN_CHAT_ID, message_id=schedule["msg"], disable_notification=True)


def waiting_delivery(chat_id):
    """(order number, job) of a CV this person is waiting for, or None."""
    return next(((k, v) for k, v in list(deliveries.items()) if v["u"] == chat_id), None)


def base_caption(caption):
    """The admin's CV caption without the last delivery line (so it can be rewritten)."""
    return re.split(r"\n(?:⏳ السي في ينتظر قرارك|⏰ يوصله)", caption or "")[0]


def delivery_buttons(target, gift_ids, scheduled):
    tail = f"{target}" + "".join(f":{i}" for i in gift_ids)
    if scheduled:
        rows = [[{"text": "📤 أرسله هسه", "callback_data": f"now:{tail}"[:64]},
                 {"text": "↩️ إلغاء الموعد", "callback_data": f"hold:{tail}"[:64]}]]
    else:
        rows = [[{"text": "📤 أرسله هسه", "callback_data": f"now:{tail}"[:64]}],
                [{"text": f"⏰ بعد {hours_text(CV_DELAY)}", "callback_data": f"later:{tail}"[:64]},
                 {"text": f"🌅 أول الدوام ({WORK_START % 12 or 12} {'الصبح' if WORK_START < 12 else 'العصر'})",
                  "callback_data": f"morn:{tail}"[:64]}]]
    return {"inline_keyboard": rows}


def show_delivery_choice(mid, job):
    """Rewrites the admin's CV caption: waiting for a decision, or the time it is set to go out."""
    line = (f"⏰ يوصله: {clock(job['t'])}" if job["t"] else
            "⏳ السي في ينتظر قرارك: اختار وقت الإرسال 👇")
    tg("editMessageCaption", chat_id=ADMIN_CHAT_ID, message_id=mid, caption=f"{job.get('cap') or ''}\n{line}"[:1024],
       reply_markup=delivery_buttons(job["u"], job.get("g") or [], bool(job["t"])))


def deliver_cv(mid, job=None, claimed=False):
    """Sends the final CV, the gifts and the candidates note. Returns True when the CV reached the person."""
    with schedule_lock:
        queued = deliveries.pop(mid, None) or {}
    job = {**queued, **{k: v for k, v in (job or {}).items() if v or k == "cap"}}  # what the admin's message carries wins
    if not job.get("u"):
        return False
    if not claimed:  # take the job on Telegram first: two copies of the server (during a deploy) can't both send it
        claim = tg("editMessageReplyMarkup", chat_id=ADMIN_CHAT_ID, message_id=mid)
        if not claim.get("ok") and "not modified" in str(claim.get("description", "")):
            save_schedule()
            return False
    target = job["u"]
    caption = (f"🎉 <b>السي في مالتك جاهز!</b>\nجهّزه فريقنا وراجعه، وهسه تگدر تقدّم بيه. بالتوفيق 🤍\n"
               f"— فريق {COMPANY}")
    res = (tg("sendDocument", chat_id=target, document=job["f"], caption=caption, parse_mode="HTML")
           if job.get("f") else {"ok": False})
    if not res.get("ok"):
        res = tg("copyMessage", chat_id=target, from_chat_id=ADMIN_CHAT_ID, message_id=mid,
                 caption=caption, parse_mode="HTML")
    status = (f"📤 انرسل للشخص ({clock(time.time())})" if res.get("ok")
              else "⚠️ ما گدرت أدزه (يمكن الشخص حاظر البوت)")
    gift_ids = sorted(set(job.get("g") or []))
    if res.get("ok") and gift_ids:
        send(target, "🎁 <b>وهاي هداياك ويا السي في</b> 👇")
        g = tg("copyMessages", chat_id=target, from_chat_id=ADMIN_CHAT_ID, message_ids=gift_ids)
        status += " + الهدايا 🎁" if g.get("ok") else " (الهدايا ما انرسلت)"
    if res.get("ok"):
        send(target, f"🤝 هسه السي في مالتك ضمن المرشحين عدنا بـ <b>{COMPANY}</b>، "
                     "ونرشحك للشركات اللي تطلب حسب مؤهلاتك.\n"
                     "🎓 وإذا تريد تتجهز أكثر، شوف كورسات المقابلات والمندبة من /start")
    if job.get("cap") is not None:
        tg("editMessageCaption", chat_id=ADMIN_CHAT_ID, message_id=mid, caption=f"{job['cap']}\n{status}"[:1024])
    else:
        send(ADMIN_CHAT_ID, status, reply_to=mid, silent=True)
    save_schedule()
    return bool(res.get("ok"))


def deliver_due(now=None):
    now = now or time.time()
    for mid, job in list(deliveries.items()):
        try:
            if job["t"] and job["t"] <= now:
                deliver_cv(mid)
            elif not job["t"] and job.get("r") and job["r"] <= now:
                job["r"] = 0
                send(ADMIN_CHAT_ID, f"⏰ <b>تذكير:</b> {esc(job.get('n') or 'متقدم')} ينتظر السي في مالته.\n"
                                    "اختار وقت الإرسال من الأزرار تحت السي في 👆", reply_to=mid)
                save_schedule()
        except Exception:
            log.exception("delivery failed")


def delivery_loop():
    while True:
        try:
            load_schedule()
            deliver_due()
        except Exception:
            log.exception("delivery loop")
        time.sleep(30)


def approve_cv(msg, target, gift_ids, pending):
    """The admin approved (payment checked): the person is told our team is preparing it; the CV then waits
    for the admin to pick when it goes out."""
    mid, caption = msg.get("message_id"), msg.get("caption") or ""
    name = (pending or {}).get("name") or (re.search(r"👤 (.+)", caption) or [None, None])[1]
    base = f"{base_caption(caption)}\n\n✅ {'الدفع مأكد' if PAID else 'تمت الموافقة'}"
    job = {"u": target, "t": 0, "g": gift_ids, "f": (msg.get("document") or {}).get("file_id"), "cap": base,
           "n": name, "r": in_work_hours(time.time() + REMIND_HOURS * 3600) if REMIND_HOURS else 0}
    if DELIVERY_MODE == "now":
        deliver_cv(mid, job, claimed=True)
        return
    head = ("✅ <b>تم تأكيد الدفع، شكراً إلك 🤍</b>" if PAID else
            "✅ <b>تمت الموافقة على طلبك من قبل فريقنا 🤍</b>")
    res = send(target, f"{head}\n\n👩‍💼 هسه السي في مالتك عند فريق {COMPANY}: دا نجهزه ونراجع المعلومات "
                       "والصياغة والتنسيق، ونجهز هداياك.\n"
                       f"⏰ يوصلك هنا <b>{CV_WAIT}</b> إن شاء الله.\n\n"
                       f"{order_status(mid, 2)}\n\nخلي إشعارات البوت مفتوحة 🔔")
    if not res.get("ok"):  # blocked the bot: nothing to wait for
        tg("editMessageCaption", chat_id=ADMIN_CHAT_ID, message_id=mid,
           caption=f"{base}\n⚠️ ما گدرت أوصله (يمكن الشخص حاظر البوت)"[:1024])
        return
    with schedule_lock:
        deliveries[mid] = job
    show_delivery_choice(mid, job)
    save_schedule()


def set_delivery(mid, msg, target, gift_ids, when):
    """Admin buttons: when = 'later' / 'morn' / 'hold' (cancel the time)."""
    with schedule_lock:
        job = deliveries.get(mid) or {"u": target, "g": gift_ids, "n": None}
        job.setdefault("g", gift_ids)
        job["g"] = job.get("g") or gift_ids
        job["f"] = job.get("f") or (msg.get("document") or {}).get("file_id")
        job["cap"] = job.get("cap") or base_caption(msg.get("caption") or "")
        job["t"] = delivery_time() if when == "later" else next_opening() if when == "morn" else 0
        job["r"] = 0
        deliveries[mid] = job
    show_delivery_choice(mid, job)
    save_schedule()


# ================================================================= job ad
def ad_text(d):
    city = tag(d.get("ad_city"))
    footer = f"\n\n📝 تريد سيرة ذاتية احترافية من {COMPANY}؟ @{BOT_USERNAME}" if BOT_USERNAME else ""
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
    send(chat_id, f"✅ وصل طلبكم لفريق {COMPANY}.\nراح يتواصل وياكم أحد موظفينا هنا، ونرشحلكم أنسب المتقدمين 🤍")


# ================================================================ courses
def course_sig(chat_id, course):
    """Proof that this person may watch this course, carried inside the lesson buttons (survives restarts)."""
    return hmac.new(SECRET.encode(), f"{chat_id}:{course}".encode(), hashlib.sha256).hexdigest()[:10]


def lesson_buttons(chat_id, course, n, total):
    sig = course_sig(chat_id, course)
    row = []
    if n < total:
        row.append({"text": f"▶️ الدرس {n + 1}", "callback_data": f"v:{course}:{n + 1}:{sig}"})
    row.append({"text": "📚 كل الدروس", "callback_data": f"v:{course}:0:{sig}"})
    return [row]


def send_lesson_list(chat_id, course):
    lessons = course_lessons(course)
    sig = course_sig(chat_id, course)
    send(chat_id, f"📚 <b>{COURSES[course]}</b>\nاختار الدرس 👇",
         inline=[[{"text": f"{i}. {t}", "callback_data": f"v:{course}:{i}:{sig}"}] for i, t in enumerate(lessons, 1)])


def send_lesson(chat_id, course, n):
    lessons = course_lessons(course)
    if not 1 <= n <= len(lessons):
        return False
    caption = f"🎓 <b>{COURSES[course]}</b>\nالدرس {n} من {len(lessons)}: <b>{esc(lessons[n - 1])}</b>"
    markup = {"inline_keyboard": lesson_buttons(chat_id, course, n, len(lessons))}
    tg("sendChatAction", chat_id=chat_id, action="upload_video")
    if (course, n) in video_ids:
        res = tg("sendVideo", chat_id=chat_id, video=video_ids[(course, n)], caption=caption, parse_mode="HTML",
                 supports_streaming=True, reply_markup=markup)
        if res.get("ok"):
            return True
    path = os.path.join(COURSE_DIR, "videos", f"{course}-{n}.mp4")
    if not os.path.exists(path):
        send(chat_id, f"🎬 الدرس {n} قيد التجهيز، راح يوصلك قريباً 🤍", inline=markup["inline_keyboard"])
        return True
    try:
        with open(path, "rb") as f:
            r = http.post(f"{API}/sendVideo", timeout=120, data={
                "chat_id": str(chat_id), "caption": caption, "parse_mode": "HTML", "supports_streaming": "true",
                "width": "720", "height": "1280", "reply_markup": json.dumps(markup)},
                files={"video": (f"{course}-{n}.mp4", f, "video/mp4")}).json()
    except Exception as e:
        log.warning("lesson upload failed: %s", e)
        return False
    if r.get("ok"):
        video_ids[(course, n)] = (r["result"].get("video") or r["result"].get("document") or {}).get("file_id")
        return True
    log.warning("sendVideo failed: %s", r.get("description"))
    return False


def unlock_course(chat_id, course):
    lessons = course_lessons(course)
    res = send(chat_id, f"🎉 <b>انفتحلك {COURSES[course]}</b>\n{len(lessons)} دروس فيديو، تشوفها هنا وترجعلها متى ما تريد.\n"
                        "هذا الدرس الأول 👇")
    if not res.get("ok"):
        return False
    send_lesson(chat_id, course, 1)
    return True


def start_course_purchase(chat_id, user, course):
    if course not in COURSES and course != "all":
        return
    label = "الكورسين" if course == "all" else COURSES[course]
    if not ADMIN_CHAT_ID:
        send(chat_id, "عذراً، الخدمة مو متوفرة هسه 🙏")
        return
    if COURSE_PRICE and PAID:
        price = COURSE_PRICE * (2 if course == "all" else 1)
        sess = new_session(chat_id, "course", "course_pay", user)
        sess["data"]["course"] = course
        name = f"الاسم: <b>{esc(QI_CARD_NAME)}</b>\n" if QI_CARD_NAME else ""
        send(chat_id, f"🎓 <b>{esc(label)}</b>\n\n💰 السعر: <b>{price:,} دينار عراقي</b>\n\n"
                      f"💳 حوّل على بطاقة <b>Qi Card</b>:\n{name}رقم البطاقة: <code>{esc(QI_CARD_NUMBER)}</code>\n\n"
                      "📸 بعدها دز <b>صورة تأكيد الدفع</b> هنا، وأول ما نتأكد ينفتحلك الكورس بالبوت مباشرةً.")
        return
    # no course price set yet: the admin arranges it and opens the course with one button
    send(ADMIN_CHAT_ID, f"🎓 <b>طلب اشتراك بالكورسات</b>\n👤 {user_handle(user, chat_id)}\n📚 {esc(label)}\n\n"
                        "<i>رد على هذي الرسالة حتى تبلغه بالسعر. بعد ما يدفع اضغط ✅ وينفتحله الكورس.</i>",
         inline=[[{"text": "✅ افتح الكورس له", "callback_data": f"kc:{chat_id}:{course}"},
                  {"text": "❌ رفض", "callback_data": f"kn:{chat_id}:{course}"}]])
    send(chat_id, f"✅ وصل طلبك لـ <b>{esc(label)}</b>.\nأحد موظفينا راح يبلغك هنا بالسعر وطريقة الدفع، "
                  "وأول ما تدفع ينفتحلك الكورس بالبوت 🤍")


def handle_course_payment(chat_id, sess, msg):
    is_image = msg.get("photo") or (msg.get("document") or {}).get("mime_type", "").startswith(("image/", "application/pdf"))
    if not is_image:
        send(chat_id, "📸 دز <b>صورة تأكيد الدفع</b> هنا حتى ينفتحلك الكورس.")
        return
    course = sess["data"].get("course")
    label = "الكورسين" if course == "all" else COURSES.get(course, "كورس")
    res = forward_to_admin(chat_id, msg, f"🧾 <b>دفع كورس</b>: {esc(label)}\n{user_handle(sess['data'].get('tg_user'), chat_id)}\n"
                                         "⚠️ تأكد من وصول المبلغ بتطبيق Qi Card قبل ما تفتح الكورس")
    sessions.pop(chat_id, None)
    if not res.get("ok"):
        send(chat_id, "صار خطأ بالإرسال 😔 دز الصورة مرة ثانية")
        return
    mid = (res.get("result") or {}).get("message_id")
    tg("editMessageReplyMarkup", chat_id=ADMIN_CHAT_ID, message_id=mid, reply_markup={"inline_keyboard": [
        [{"text": "✅ الدفع وصل، افتح الكورس", "callback_data": f"kc:{chat_id}:{course}"}],
        [{"text": "❌ الدفع ما وصل", "callback_data": f"kn:{chat_id}:{course}"}]]})
    pending_payments[chat_id] = {"t": time.time(), "msg": mid, "what": "الكورس",
                                 "name": (sess["data"].get("tg_user") or {}).get("first_name")}
    for minutes in (10, 30):
        timer = threading.Timer(minutes * 60, remind_admin, args=(chat_id, minutes))
        timer.daemon = True
        timer.start()
    send(chat_id, "✅ <b>استلمنا صورة الدفع، شكراً إلك 🤍</b>\n⏳ هسه موظف من فريقنا دا يتأكد من وصول المبلغ، "
                  f"وأول ما يتأكد ينفتحلك الكورس هنا {PAYMENT_WAIT} 🎓")


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
            elif sess["step"] == "talk":
                send(chat_id, sess.get("last_q") or TALK_INTRO)
            elif sess["step"] == "payment":
                send(chat_id, payment_message())
            elif sess["step"] == "confirm":
                show_summary(chat_id, sess)
            else:
                ask(chat_id, sess["step"])
        elif data == "m:cv":
            start_cv(chat_id, user)
        elif data == "m:courses":
            send(chat_id, courses_text(), inline=[[{"text": label, "callback_data": f"k:buy:{key}"}]
                                                  for key, label in COURSE_CHOICES]
                 + [[{"text": "↩️ القائمة", "callback_data": "k:menu"}]])
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

    # ---- courses (no session needed)
    if data.startswith("k:"):
        drop_buttons()
        if data == "k:menu":
            show_menu(chat_id)
        elif data.startswith("k:buy:"):
            start_course_purchase(chat_id, user, data[6:])
        return
    if data.startswith("v:"):  # a lesson: v:<course>:<n>:<signature>
        parts = data.split(":")
        if len(parts) == 4 and parts[1] in COURSES and parts[2].isdigit() and course_sig(chat_id, parts[1]) == parts[3]:
            if int(parts[2]) == 0:
                send_lesson_list(chat_id, parts[1])
            else:
                send_lesson(chat_id, parts[1], int(parts[2]))
        else:
            send(chat_id, "🔒 هذا الدرس للمشتركين بالكورس. تگدر تشترك من /start ثم 🎓 الكورسات")
        return

    # ---- applicant buttons (need a live session)
    if data[:2] in ("u:", "e:", "a:", "p:") or data[:2] in tuple(f"{p}:" for p in MULTI):
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
        elif data == "u:steps" and sess["step"] in ("form", "talk"):
            drop_buttons()
            start_steps(chat_id, sess)
        elif data.startswith("p:") and data.count(":") == 2 and data.split(":")[2].isdigit():
            drop_buttons()
            choose_photo(chat_id, sess, data.split(":")[1], int(data.split(":")[2]))
        elif data == "u:form" and sess["step"] == "talk":
            drop_buttons()
            send_form(chat_id)
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
    parts = data.split(":")
    action, target = parts[0], parts[1] if len(parts) > 1 else ""
    if not (ADMIN_CHAT_ID and str(chat_id) == str(ADMIN_CHAT_ID) and target.lstrip("-").isdigit()):
        return
    target = int(target)
    if action in ("ok", "no"):
        caption = msg.get("caption") or ""
        drop_buttons()  # first, so a double tap can't send twice
        pending = pending_payments.pop(target, None)
        if action == "ok":
            gift_ids = sorted(int(x) for x in parts[2:] if x.isdigit()) or gifts_by_user.get(target, [])
            approve_cv(msg, target, gift_ids, pending)
            return
        text = ("❌ ما گدرنا نتأكد من وصول الدفع.\nإذا حوّلت المبلغ، اكتب /start واختار 💬 تواصل ويانا."
                if PAID else "عذراً، نحتاج نراجع معلوماتك 🙏 تگدر تعيد التقديم من /start أو راح يتواصل وياك أحد موظفينا.")
        send(target, text)
        tg("editMessageCaption", chat_id=chat_id, message_id=mid, caption=f"{caption}\n\n❌ مرفوض"[:1024])
    elif action == "now":  # the admin sends the CV now
        drop_buttons()
        gift_ids = [int(x) for x in parts[2:] if x.isdigit()]
        job = {"u": target, "g": gift_ids, "f": (msg.get("document") or {}).get("file_id"),
               "cap": base_caption(msg.get("caption") or "")}
        deliver_cv(mid, job, claimed=True)
    elif action in ("later", "morn", "hold"):  # the admin picks a time (or cancels it)
        set_delivery(mid, msg, target, [int(x) for x in parts[2:] if x.isdigit()], action)
    elif action in ("kc", "kn"):
        course = parts[2] if len(parts) > 2 else ""
        drop_buttons()
        pending_payments.pop(target, None)
        base = msg.get("text") or msg.get("caption") or ""
        if action == "kc":
            keys = list(COURSES) if course == "all" else [course] if course in COURSES else []
            ok = all(unlock_course(target, k) for k in keys) if keys else False
            status = "✅ انفتح الكورس للشخص" if ok else "⚠️ ما گدرت أدزله (يمكن حاظر البوت)"
        else:
            send(target, "❌ ما گدرنا نتأكد من الدفع للكورس.\nإذا حوّلت المبلغ، اكتب /start واختار 💬 تواصل ويانا.")
            status = "❌ مرفوض"
        if msg.get("caption") is not None:
            tg("editMessageCaption", chat_id=chat_id, message_id=mid, caption=f"{base}\n\n{status}"[:1024])
        else:
            tg("editMessageText", chat_id=chat_id, message_id=mid, text=f"{base}\n\n{status}"[:4000])
    elif action in ("adok", "adno"):
        original = (msg.get("text") or "").split("\n\n")[0]
        drop_buttons()
        if action == "adno":
            send(target, "عذراً، إعلانك ما انقبل للنشر 🙏 للاستفسار اكتب /start واختار 💬 تواصل ويانا.")
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
        res = send(target, f"📩 <b>رد فريق {COMPANY}:</b>\n\n{esc(msg['text'])}")
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
    if cmd == "/setphoto" and is_admin:
        res = set_profile_photo()
        send(chat_id, "✅ تغيّرت صورة البوت" if res.get("ok") else f"⚠️ ما تغيّرت: {esc(res.get('description'))}")
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
        elif chat_id in pending_payments and not cmd:
            mins = int((time.time() - pending_payments[chat_id]["t"]) / 60)
            send(chat_id, f"⏳ موظفنا دا يتأكد من الدفع هسه (من {mins} دقيقة).\n"
                          "بعدها فريقنا يكمل تجهيز السي في مالتك، ويوصلك هنا ويا الهدايا 🤍")
        elif not cmd and waiting_delivery(chat_id):
            order, job = waiting_delivery(chat_id)
            send(chat_id, "⏳ السي في مالتك عند فريقنا هسه، دا نجهز النسخة النهائية.\n"
                          f"راح يوصلك هنا {when_text(job['t']) if job['t'] else 'قريباً إن شاء الله'} ويا الهدايا 🤍\n\n"
                          + order_status(order, 2))
        elif ai.enabled() and (len(clean(text)) >= 40 or msg.get("voice") or msg.get("audio")):
            # they just wrote (or said) their details without pressing anything: start the CV right away
            sess = new_session(chat_id, "cv", "talk", msg.get("from") or {})
            sess["talk"], sess["rounds"] = [], 0
            send(chat_id, "📝 تمام، راح نسويلك سيرة ذاتية من معلوماتك")
            handle_cv_answer(chat_id, sess, msg)
        else:
            show_menu(chat_id)
    elif sess["mode"] == "cv":
        handle_cv_answer(chat_id, sess, msg)
    elif sess["mode"] == "ad":
        handle_ad_answer(chat_id, sess, msg)
    elif sess["mode"] == "course":
        handle_course_payment(chat_id, sess, msg)
    elif sess["mode"] == "contact":
        res = forward_to_admin(chat_id, msg, f"💬 <b>رسالة من</b> {user_handle(sess['data'].get('tg_user'), chat_id)}\n"
                                             "<i>(رد على هذي الرسالة حتى يوصل ردك للشخص)</i>")
        sessions.pop(chat_id, None)
        send(chat_id, f"✅ وصلت رسالتك لفريق {COMPANY}، وراح يرد عليك أحد موظفينا هنا قريباً 🤍" if res.get("ok")
             else "صار خطأ بالإرسال 😔 جرّب مرة ثانية من /start")


# =================================================================== web
@app.get("/")
def health():
    stats["visits"] = stats.get("visits", 0) + 1
    stats["last_visit"] = time.time()
    return "CV bot is running ✅"


@app.get("/robots.txt")
def robots():
    return "User-agent: *\nAllow: /\n", 200, {"Content-Type": "text/plain"}


@app.get("/health")
def health_details():
    """Public self-check (no secrets shown): version, token, webhook, settings. Re-links the webhook if needed."""
    lines = [f"version: {(env('RENDER_GIT_COMMIT') or 'unknown')[:7]}"]
    t0 = time.time()
    me = tg("getMe", _timeout=10)
    tg_seconds = time.time() - t0
    token_ok = bool(me.get("ok"))
    lines.append("bot token: " + (f"OK @{(me.get('result') or {}).get('username')}" if token_ok
                                   else f"PROBLEM ({me.get('description') or 'no answer from Telegram'})")
                 + f" (Telegram answered in {tg_seconds:.1f}s)")
    if token_ok:
        expected = f"{PUBLIC_URL}/webhook/{SECRET}"
        wh = tg("getWebhookInfo", _timeout=10).get("result") or {}
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
              f"ai key: {'set' if ai.enabled() else 'not set'}",
              f"keep awake: {'on' if any(t.name == 'keep-awake' for t in threading.enumerate()) else 'off'}",
              f"bot picture/description: {stats.get('profile', 'not checked yet')}"]
    mins = lambda t: f"{int((time.time() - t) / 60)} min ago" if t else "never"
    lines += [f"server started: {mins(stats['started'])}",
              f"messages received: {stats['received']} (last: {mins(stats['last_received'])})",
              f"wake-up visits: {stats.get('visits', 0)} (last: {mins(stats.get('last_visit', 0))})",
              f"handled ok: {stats['handled']}, failed: {stats['failed']}"
              + (f" (last: {stats['last_failure']})" if stats["last_failure"] else ""),
              f"telegram send problems: {stats['tg_failed']}"
              + (f" (last: {stats['last_tg_failure']})" if stats["last_tg_failure"] else "")]
    if request.args.get("ai") == "1":
        lines.append("ai test: " + ai.ping())
    if request.args.get("test") == "1":
        lines += self_test()
    return ("<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
            "<title>CV bot check</title><pre style='font-size:16px'>" + esc("\n".join(lines)) + "</pre>")


SELF_TEST_TEXT = """الاسم الثلاثي: حسين علي جبار
العمر: 27
رقم الهاتف: 07801234567
الإيميل: hussein.ali@gmail.com
السكن: بغداد - زيونة
الوظيفة المطلوبة: مندوب طبي
الشهادة والتخصص: بكالوريوس علوم حياة
الجامعة أو المعهد: جامعة بغداد
سنة التخرج أو المرحلة: 2021
الخبرات: شركة بيونير - مندوب طبي - بغداد الرصافة - سنتين
المهام: زيارة الأطباء والصيدليات وعرض المنتجات وتحقيق التارگت الشهري
المنتجات: مضادات حيوية ومسكنات
المناطق: الرصافة
الزبائن: أطباء وصيدليات
المهارات: التواصل، الإقناع
مستوى الإنگليزي: جيد جدا
لغات ثانية:
الدورات: دورة مهارات البيع
إجازة سوق وسيارة؟ نعم
مسح ميداني؟ نعم"""
last_self_test = [0.0, []]


def self_test():
    """Checks the whole path on the live server: sending to the admin (silent, deleted at once),
    the AI reader and the PDF. Runs at most every 5 minutes; nothing is kept or sent to anyone else."""
    if time.time() - last_self_test[0] < 300:
        return ["self-test (cached):"] + last_self_test[1]
    out = []
    if ADMIN_CHAT_ID:
        res = tg("sendMessage", chat_id=ADMIN_CHAT_ID, text="🔧 فحص تلقائي", disable_notification=True)
        if res.get("ok"):
            tg("deleteMessage", chat_id=ADMIN_CHAT_ID, message_id=res["result"]["message_id"])
        out.append("send to admin: " + ("OK (silent, deleted)" if res.get("ok") else f"FAILED ({res.get('description')})"))
        intro, form_text, buttons, _ = cv_intro()
        checks = []
        for text, markup in ((TALK_INTRO, None), (intro, None), (form_text, {"inline_keyboard": buttons})):
            params = {"chat_id": ADMIN_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_notification": True}
            if markup:
                params["reply_markup"] = markup
            r = tg("sendMessage", **params)
            if r.get("ok"):
                tg("deleteMessage", chat_id=ADMIN_CHAT_ID, message_id=r["result"]["message_id"])
            checks.append("OK" if r.get("ok") else f"FAILED ({r.get('description')})")
        out.append(f"cv start messages: AI intro {checks[0]}, form intro {checks[1]}, form with copy button {checks[2]}")
    started = time.time()
    data, how = ai.read_cv(SELF_TEST_TEXT) if ai.enabled() else (None, "no key")
    used_ai = bool(data)
    out.append(f"ai reader: {'OK ' + how if data else 'not used (' + how + ')'} in {time.time() - started:.0f}s")
    if not data:
        data = convert_form(parse_form(SELF_TEST_TEXT) or {})
    if used_ai:
        started = time.time()
        convo = ("جواب الشخص: اني علي حسن كريم من بغداد، رقمي 07701234567، خريج صيدلة جامعة بغداد 2022، "
                 "أريد شغل مندوب طبي\n\nتصحيح من الشخص على الملخص: غيّر المحافظة للبصرة")
        edit, how2 = ai.read_cv(convo)
        city = (edit or {}).get("city_en") or ""
        out.append(f"ai correction: {'OK' if 'basra' in city.lower() else 'CHECK'} (city={city or how2}) "
                   f"in {time.time() - started:.0f}s")
        import wave
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x00\x00" * 16000)
        text, how3 = ai.transcribe(buf.getvalue(), "test.wav")
        out.append("voice to text: " + ("OK" if text or how3 == "empty" else f"FAILED ({how3})"))
    d = {"tg_user": {}, "experiences": []}
    try:
        apply_cv_data(d, data)
        if used_ai:
            d["_ai"] = True
        out.append(f"read: name={d.get('full_name')}, title={d.get('job_title')}, city={d.get('city')}, "
                   f"jobs={len(d.get('experiences') or [])}, still to ask={missing_steps(d)}")
        d["_photo_asked"] = True
        refresh_languages(d)
        pdf = build_cv(latinize(d), None)
        pages = len(re.findall(rb"/Type\s*/Page[^s]", pdf))
        out.append(f"cv pdf: {'OK' if pdf[:4] == b'%PDF' else 'BAD'} ({len(pdf) // 1024} KB, {pages} page)")
        refresh_languages(SAMPLE_CV)
        sample = extras.sample_image(build_cv(dict(SAMPLE_CV), extras.sample_photo()))
        out.append("sample CV image (before payment): " + (f"OK ({len(sample) // 1024} KB)" if sample else "FAILED (no PDF renderer)"))
        out.append("face detector for photos: " + ("ready" if photos.detector_ready() else "missing (uses a centred crop)"))
        vids = {k: sum(os.path.exists(os.path.join(COURSE_DIR, "videos", f"{k}-{n}.mp4"))
                       for n in range(1, len(course_lessons(k)) + 1)) for k in COURSES}
        out.append("course videos: " + ", ".join(f"{k} {v}/{len(course_lessons(k))}" for k, v in vids.items())
                   + f" | course price: {COURSE_PRICE or 'not set (admin request)'}")
        out.append(f"cv delivery: {'right after approval' if DELIVERY_MODE == 'now' else 'admin chooses (buttons)'}"
                   f", buyer told '{CV_WAIT}', later button {CV_DELAY} min, working hours {WORK_START}-{WORK_END} Baghdad"
                   f", waiting {len(deliveries)}, schedule {'pinned' if schedule['msg'] else 'loaded' if schedule['loaded'] else 'not read yet'}")
        ats, cover = extras.build_ats_cv(latinize(d)), extras.build_cover_letter(latinize(d), None)
        out.append(f"gift files: ATS CV {'OK' if ats[:4] == b'%PDF' else 'BAD'}, cover letter {'OK' if cover[:4] == b'%PDF' else 'BAD'}")
        if used_ai:
            started = time.time()
            gifts, how4 = ai.write_extras(gift_facts(d))
            qa_count = len((gifts or {}).get("interview") or [])
            out.append(f"gifts AI: {'OK ' + how4 if gifts else 'FAILED (' + how4 + ')'}, {qa_count} interview questions, "
                       f"letter {len((gifts or {}).get('cover_letter_en') or '')} chars, in {time.time() - started:.0f}s")
    except Exception as e:
        log.exception("self-test failed")
        out.append(f"cv pipeline: FAILED {e.__class__.__name__}: {str(e)[:150]}")
    last_self_test[0], last_self_test[1] = time.time(), out
    return ["self-test:"] + out


def latest_commit(repo, branch):
    """Newest commit on GitHub. The API allows few anonymous calls from Render's shared addresses,
    so git's own public endpoint (no such limit) is the fallback."""
    try:
        sha = http.get(f"https://api.github.com/repos/{repo}/commits/{branch}", timeout=15,
                       headers={"Accept": "application/vnd.github+json"}).json().get("sha") or ""
        if re.fullmatch(r"[0-9a-f]{40}", sha):
            return sha
    except Exception as e:
        log.warning("github api check failed: %s", e)
    try:
        refs = http.get(f"https://github.com/{repo}.git/info/refs?service=git-upload-pack", timeout=15).text
        m = re.search(r"([0-9a-f]{40}) refs/heads/" + re.escape(branch) + r"\b", refs)
        return m.group(1) if m else ""
    except Exception as e:
        log.warning("github refs check failed: %s", e)
        return ""


last_sync = [0.0]


@app.get("/sync")
def sync_deploy():
    """Deploys the newest GitHub commit when this server runs an older one, so updates go live without
    the dashboard. Safe to leave public: it can only deploy the latest public commit, at most every 4 minutes."""
    hook = env("DEPLOY_HOOK")
    running = env("RENDER_GIT_COMMIT")
    repo = env("RENDER_GIT_REPO_SLUG") or "aliamjedqasim-cell/cv-bot"
    branch = env("RENDER_GIT_BRANCH") or "main"
    latest = latest_commit(repo, branch)
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
        stats["handled"] += 1
    except Exception as e:
        log.exception("update failed")
        stats["failed"] += 1
        stats["last_failure"] = f"{e.__class__.__name__}: {str(e)[:150]} ({time.strftime('%H:%M')} UTC)"


@app.post(f"/webhook/{SECRET}")
def webhook():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != SECRET:
        abort(403)
    upd = request.get_json(silent=True) or {}
    with lock:
        if upd.get("update_id") in seen_updates:  # Telegram re-sends updates if we were slow
            return "ok"
        seen_updates.append(upd.get("update_id"))
        stats["received"] += 1
        stats["last_received"] = time.time()
        cleanup()
        if len(chat_locks) > 5000:
            chat_locks.clear()
    if env("SYNC_UPDATES") == "1":  # used by the tests
        process(upd)
    else:
        workers.submit(process, upd)
    return "ok"


SHORT_DESC = f"{COMPANY} 🩺 تابعين لـ{PARTNER} | سي في احترافي، ترشيح للشركات، وكورسات مقابلات ومندبة"
LONG_DESC = (f"🩺 {COMPANY} | {COMPANY_EN}\n"
             f"شركة متخصصة بالتوظيف بالقطاع الطبي والمبيعات، تابعة لـ{PARTNER} 📢\n\n"
             "📝 نصمملك سيرة ذاتية CV احترافية بالإنگليزي، يراجعها ويدققها فريقنا\n"
             "🤝 نرشّح السي في مالتك للشركات اللي تطلب مندوبين وموظفين وخدمات\n"
             "🎓 كورسات بالمقابلات الوظيفية والمندبة الطبية\n"
             "🏢 وللشركات: نوفرلكم مندوبين وموظفين\n\n"
             "احچي بالعربي أو دز بصمة صوت 🎤، واضغط Start 👇")
AVATAR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "avatar.jpg")


def set_profile_photo():
    try:
        with open(AVATAR, "rb") as f:
            r = http.post(f"{API}/setMyProfilePhoto", timeout=60,
                          data={"photo": json.dumps({"type": "static", "photo": "attach://avatar"})},
                          files={"avatar": ("avatar.jpg", f.read(), "image/jpeg")})
        return r.json()
    except Exception as e:
        return {"ok": False, "description": e.__class__.__name__}


def setup_profile():
    """The bot's picture and descriptions, set once (the short description doubles as the 'done' marker)."""
    current = (tg("getMyShortDescription").get("result") or {}).get("short_description", "")
    if current == SHORT_DESC:
        stats["profile"] = "set earlier"
        if (tg("getMyDescription").get("result") or {}).get("description", LONG_DESC) != LONG_DESC:
            tg("setMyDescription", description=LONG_DESC)  # only the text changed: keep the picture
            stats["profile"] = "description updated"
        return
    tg("removeMyProfilePhoto")  # drop the previous picture, then put the current one
    res = set_profile_photo()
    stats["profile"] = "picture set" if res.get("ok") else f"picture failed ({res.get('description')})"
    log.info("bot profile: %s", stats["profile"])
    tg("setMyName", name=BOT_NAME)
    tg("setMyDescription", description=LONG_DESC)
    tg("setMyShortDescription", short_description=SHORT_DESC)


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
            setup_profile()
            return
        time.sleep(3 * (attempt + 1))


def keep_awake():
    """Render's free plan sleeps after 15 idle minutes: the first reply then takes about a minute and
    half-filled CVs are lost. A light visit every 10 minutes keeps it awake (one service awake all month
    fits in the 750 free hours). Set KEEP_AWAKE=off on Render to stop it."""
    while True:
        time.sleep(600)
        try:
            requests.get(PUBLIC_URL + "/", timeout=30)
        except Exception as e:
            log.warning("keep-awake visit failed: %s", e)


def fresh_connections():
    """Render starts gunicorn with --preload: the app loads in a parent process that is then copied into
    the worker. Each process must open its own connections, or two processes could share one socket."""
    global http
    http = requests.Session()
    ai.http = requests.Session()
    translate.http = requests.Session()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=fresh_connections)

def watchdog():
    """If an outside connection ever hangs past its own timeout (a stuck process), restart this worker:
    gunicorn starts a fresh one in a second. A slow or unreachable Telegram does not count, only a hang."""
    while True:
        time.sleep(300)
        probe = threading.Thread(target=tg, args=("getMe",), kwargs={"_timeout": 20}, daemon=True)
        probe.start()
        probe.join(90)
        if probe.is_alive():
            log.critical("outside connections are stuck; restarting this worker")
            os._exit(1)


started_in = [None]


def start_background():
    """Background jobs: only in the process that answers requests, never in gunicorn's parent.
    (Render runs gunicorn with --preload; network activity before the fork could leave the worker stuck.)"""
    with lock:
        if started_in[0] == os.getpid():
            return
        started_in[0] = os.getpid()
    if env("DISABLE_WEBHOOK_SETUP") == "1":
        return
    threading.Thread(target=register_webhook, daemon=True, name="register-webhook").start()
    threading.Thread(target=watchdog, daemon=True, name="watchdog").start()
    threading.Thread(target=delivery_loop, daemon=True, name="deliveries").start()
    if PUBLIC_URL and os.environ.get("KEEP_AWAKE", "on").strip().lower() not in ("0", "off", "no", "false"):
        threading.Thread(target=keep_awake, daemon=True, name="keep-awake").start()


@app.before_request
def start_background_jobs():
    if started_in[0] != os.getpid():  # fallback when gunicorn.conf.py's post_fork did not run
        start_background()


if __name__ == "__main__":
    start_background()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
