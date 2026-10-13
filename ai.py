"""Reads an applicant's free-form (usually Arabic) message with AI and returns structured CV data.

Uses the free tier of Groq (https://console.groq.com). Set GROQ_API_KEY in Render -> Environment.
Several models are tried in order; each has its own free daily limit, so when one is used up the
next one answers. If every model fails, read_cv() returns None and the bot falls back to its
own template reader.
"""
import json
import logging
import os
import re

import time

import requests

log = logging.getLogger("cvbot.ai")
http = requests.Session()
TOTAL_SECONDS = 45
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODELS = "openai/gpt-oss-120b,qwen/qwen3.8-27b,openai/gpt-oss-20b,llama-3.3-70b-versatile"


def api_key():
    key = os.environ.get("GROQ_API_KEY", "").strip()
    return "" if key.lower() in ("", "0", "-", "none") else key


def enabled():
    return bool(api_key())


def models():
    return [m.strip() for m in (os.environ.get("GROQ_MODELS") or DEFAULT_MODELS).split(",") if m.strip()]


# ------------------------------------------------------------------ schema
def _text():
    return {"type": ["string", "null"]}


def _list():
    return {"type": "array", "items": {"type": "string"}}


def _obj(props):
    return {"type": "object", "additionalProperties": False, "properties": props, "required": list(props)}


JOB = _obj({
    "company_en": _text(), "company_ar": _text(), "title_en": _text(), "title_ar": _text(),
    "location_en": _text(), "location_ar": _text(), "duration_en": _text(), "duration_ar": _text(),
    "duties_en": _list(), "duties_ar": _list(),
})
SCHEMA = _obj({
    "full_name_en": _text(), "full_name_ar": _text(), "age": _text(), "phone": _text(), "email": _text(),
    "city_en": _text(), "city_ar": _text(), "job_title_en": _text(), "job_title_ar": _text(),
    "degree_level": _text(), "specialty_en": _text(), "specialty_ar": _text(),
    "university_en": _text(), "university_ar": _text(), "edu_status": _text(), "edu_status_ar": _text(),
    "has_experience": {"type": ["boolean", "null"]},
    "experiences": {"type": "array", "items": JOB},
    "customers_en": _list(), "products_en": _text(), "products_ar": _text(),
    "territory_en": _text(), "territory_ar": _text(),
    "survey": {"type": ["boolean", "null"]}, "licence": _text(),
    "achievements_en": _list(), "achievements_ar": _list(),
    "skills_en": _list(), "skills_ar": _list(), "english_level": _text(),
    "other_languages_en": _list(), "courses_en": _list(), "courses_ar": _list(),
    "profile_en": _text(), "objective_en": _text(),
})

SYSTEM = """You are an expert Iraqi recruiter and professional CV writer. You receive what a job seeker told a CV bot, usually in Iraqi Arabic (sometimes mixed with English, sometimes a filled-in form, sometimes free text, sometimes the text of an old CV, sometimes a voice message turned into text automatically, which can contain small hearing mistakes: read it sensibly). Turn it into structured data for a polished, professional English CV for the Iraqi medical and sales job market.

CONVERSATION RULES:
- The input can hold several messages in order, including the bot's questions and the person's answers. Combine everything.
- Later messages and corrections override earlier ones (e.g. "غير المحافظة للبصرة", "اسمي محمد مو علي", "احذف الخبرة الثانية").
- Short answers belong to the question just before them (e.g. bot asked for the graduation year, person wrote "2022").

TRUTH RULES (most important; a CV with a wrong fact gets the applicant rejected by HR):
- Use only facts the person stated. Never invent employers, job titles, dates, durations, degrees, specialties, universities, graduation years, languages, certificates, numbers, sales figures, targets achieved or awards.
- If something is not mentioned or is unclear, use null (or [] for lists). Do not guess.
- The field of study (specialty) is what they STUDIED, never their job: "مندوب طبي" / "شغل طبي" / "شركة أدوية" do NOT mean a medical degree. If the study field is not clearly stated, specialty = null.
- Graduation year: only a year they actually wrote for their studies. "طالب" / "مرحلة رابعة" = still studying, not graduated.
- Durations: exactly what they said ("سنتين" = "2 years", "من 2021 لحد الآن" = "2021 - Present"). Never shorten, round or merge.

WRITING RULES:
- All *_en fields: correct, polished, professional English with correct spelling and grammar, consistent Title Case for job titles and headings, consistent date style ("2021 - Present", "Mar 2022 - Jan 2024").
- Names of people: transliterate with common Iraqi English spelling (محمد Mohammed, علي Ali, حسين Hussein, جاسم Jasim, كاظم Kadhim, عبد الكريم Abdulkareem, الربيعي Al-Rubaie). Never translate the meaning of a name.
- Companies and places: keep known brands in their English form (بيونير Pioneer, اسيا سيل Asiacell, الحكمة Hikma); transliterate Arabic proper names (صيدلية النور Al-Noor Pharmacy); use standard spellings for Iraqi places (الكرخ Karkh, المنصور Al-Mansour, العمارة Amarah, الناصرية Nasiriyah).
- job_title_en: the position they want, as a standard English job title (e.g. Medical Representative, Medical Sales Representative, Sales Supervisor, Team Leader, Pharmacist). If they want a supervisory role and stated leading a team, use that level.
- degree_level: exactly one of "Bachelor's Degree", "Diploma", "Master's Degree", "PhD", "High School Diploma", or null. specialty_en: the field only (e.g. Pharmacy, Communications Engineering, Medical Laboratory Sciences).
- edu_status: the 4-digit graduation year if graduated, or "1st-Year Student" ... "Final-Year Student" if still studying, else null.
- has_experience: true if they mention any work experience, false if they say they have none (e.g. "ماكو خبرة", "ما اشتغلت", "خريج جديد بدون خبرة"), null if not mentioned.
- experiences: one item per POSITION, newest first. If they were promoted inside the same company (e.g. representative, then team leader, then supervisor), make one item per position with the same company and each position's own title and duration. location_en: the city plus the governorates/areas they covered, if stated. duties_en: 3-4 strong bullet points starting with action verbs, built from what they described (visit plans, doctor relationships, training representatives, following targets, territory management, reports...) and what that exact position normally involves; leadership duties only if they stated leading people. Never add numbers or results they did not state.
- achievements_en: ONLY concrete results they stated (e.g. target percentage, sales growth, new accounts or areas opened, number of reps trained), written as strong bullets with their real numbers. If none were stated: []. Never put normal duties here.
- customers_en: only from Doctors, Pharmacies, Clinics, Hospitals, Shops & Markets, Companies, as stated.
- licence: "car" (driving licence and a car), "licence" (licence, no car), "none" (said no), or null if not mentioned. survey: true if they did a field/market survey, false if they said no, null if not mentioned.
- skills_en: 9 to 12 skills. First job-specific skills supported by their experience or study (for medical sales e.g. "Medical Sales & Territory Management", "Doctor Relationship Management", "Sales Target Planning & Tracking", "Product Presentation", "Market & Competitor Analysis"; "Team Leadership & Coaching" only if they led people), then the skills they mentioned, then a few professional soft skills. No duplicates, consistent Title Case.
- english_level: exactly one of "Fluent", "Very Good", "Good", "Intermediate", "Basic", "none", or null. other_languages_en: languages other than Arabic and English.
- profile_en: a specific Professional Summary of 2-3 sentences, without "I": their real level and years/companies/territories/specialty as stated, what they are strongest at, and the role they are seeking. Avoid empty clichés ("highly motivated", "results-driven", "dynamic") unless backed by a stated fact.
- objective_en: one short sentence naming the role they want (used only if needed).
- age: digits only if stated (convert a birth year to an age in 2026), else null.
- *_ar fields: copy the person's own words for that item (short), or null. skills_ar / courses_ar / duties_ar / achievements_ar: their own words as a list.

Return only JSON that matches the schema."""


# ------------------------------------------------------------------- calls
def _parse(content):
    content = re.sub(r"<think>.*?</think>", "", content or "", flags=re.S).strip()
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(content[start:end + 1])
        return data if isinstance(data, dict) else None
    except ValueError:
        return None


def _body(model, text, minimal=False, system=None, schema=None, name="cv_data", max_tokens=3500):
    system, schema = system or SYSTEM, schema or SCHEMA
    body = {"model": model, "temperature": 0.3, "max_completion_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": text}]}
    if minimal:
        keys = ", ".join(schema["properties"])
        body["messages"][0]["content"] += f"\nReturn one JSON object with the keys: {keys}."
        if schema is SCHEMA:
            body["messages"][0]["content"] += " experiences items keys: " + ", ".join(JOB["properties"]) + "."
        body["response_format"] = {"type": "json_object"}
        return body
    if "gpt-oss" in model or "qwen" in model:
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": name, "strict": True, "schema": schema}}
    else:
        return _body(model, text, True, system, schema, name, max_tokens)
    if "gpt-oss" in model:
        body["reasoning_effort"] = "low"
        body["include_reasoning"] = False
    elif "qwen" in model:
        body["reasoning_effort"] = "none"
        body["reasoning_format"] = "hidden"
    return body


def _ask(text, system=None, schema=None, name="cv_data", max_tokens=3500, seconds=TOTAL_SECONDS):
    """One JSON answer from the first model that can give it. Returns (data, model) or (None, reason)."""
    key = api_key()
    if not key:
        return None, "no key"
    last = "failed"
    deadline = time.time() + seconds  # the applicant never waits long: after this, the bot's own reader takes over
    for model in models():
        for minimal in (False, True):  # if a model refuses the advanced options, retry plainly once
            left = deadline - time.time()
            if left < 5:
                return None, last + " (timeout)"
            try:
                r = http.post(GROQ_URL, json=_body(model, text, minimal, system, schema, name, max_tokens),
                              headers={"Authorization": f"Bearer {key}"}, timeout=(5, min(40, left)))
            except Exception as e:
                last = f"{model}: {e.__class__.__name__}"
                log.warning("groq %s error: %s", model, e)
                break
            if r.status_code == 429:
                last = f"{model}: daily limit"
                log.warning("groq %s rate limited", model)
                break
            if r.status_code in (401, 403):
                log.error("groq key rejected (%s)", r.status_code)
                return None, "key rejected"
            if r.status_code >= 400:
                last = f"{model}: HTTP {r.status_code}"
                log.warning("groq %s HTTP %s: %s", model, r.status_code, r.text[:300])
                if r.status_code == 400 and not minimal:
                    continue
                break
            try:
                content = r.json()["choices"][0]["message"].get("content")
            except Exception:
                content = None
            data = _parse(content)
            if data:
                return data, model
            last = f"{model}: bad JSON"
            if not minimal:
                continue
            break
    return None, last


def read_cv(text):
    """Returns (data dict, model name) or (None, reason)."""
    text = text or ""
    if len(text) > 7000:  # keep the first message (most facts) and the newest answers
        text = text[:4000] + "\n...\n" + text[-3000:]
    return _ask(text)


# ------------------------------------------------------- gifts for the buyer
EXTRAS_SCHEMA = _obj({
    "cover_letter_en": {"type": "string"},
    "interview": {"type": "array", "items": _obj({"q": {"type": "string"}, "a": {"type": "string"}})},
    "tips_ar": _list(),
})
EXTRAS_SYSTEM = """You help Iraqi job seekers in the medical and sales job market. You get a person's CV data as JSON (true facts only). Write:
1) cover_letter_en: the body of a professional English cover letter for the role in job_title: 3 short paragraphs, 160-220 words. Address no specific company (say "your company" / "your team"), no placeholders in brackets, no greeting line and no signature (they are added separately). Use only facts in the data; never invent numbers, employers or results. Specific and confident, not generic.
2) interview: the 8 most likely interview questions for this exact role in Iraqi companies (e.g. "عرّفنا بنفسك", why this job, handling a doctor's objection, targets, pressure, salary expectation), each question in Iraqi Arabic with a suggested answer in Iraqi Arabic (2-3 sentences) built from the person's real background. Never invent facts; if they lack experience, show how to answer honestly and positively.
3) tips_ar: 4 short practical interview tips in Iraqi Arabic (appearance, arriving early, bringing the CV, follow-up).
Return only JSON that matches the schema."""


def write_extras(cv_facts):
    """Cover letter + interview preparation for the buyer. Returns (data, model) or (None, reason)."""
    return _ask(json.dumps(cv_facts, ensure_ascii=False)[:6000], EXTRAS_SYSTEM, EXTRAS_SCHEMA, "cv_extras",
                max_tokens=4000, seconds=60)


STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
STT_MODELS = ("whisper-large-v3-turbo", "whisper-large-v3")
STT_HINT = ("سيرة ذاتية. اسمي، رقم هاتفي، بغداد، البصرة، خريج، بكالوريوس صيدلة، طالب مرحلة، جامعة، "
            "اشتغلت مندوب طبي، مندوب مبيعات، شركة، سنتين، أطباء، صيدليات، إنگليزي، إجازة سوق.")


def transcribe(audio, filename="voice.ogg"):
    """Voice message -> Arabic text with Groq's free Whisper. Returns (text, model) or (None, reason)."""
    key = api_key()
    if not key or not audio:
        return None, "no key" if not key else "no audio"
    last = "failed"
    for model in STT_MODELS:
        try:
            r = http.post(STT_URL, headers={"Authorization": f"Bearer {key}"}, timeout=(5, 40),
                          data={"model": model, "language": "ar", "temperature": "0", "prompt": STT_HINT,
                                "response_format": "json"},
                          files={"file": (filename, audio)})
        except Exception as e:
            last = f"{model}: {e.__class__.__name__}"
            log.warning("groq stt %s error: %s", model, e)
            continue
        if r.status_code in (401, 403):
            return None, "key rejected"
        if r.status_code >= 400:
            last = f"{model}: HTTP {r.status_code}"
            log.warning("groq stt %s HTTP %s: %s", model, r.status_code, r.text[:200])
            continue
        try:
            text = (r.json().get("text") or "").strip()
        except Exception:
            text = ""
        return (text, model) if text else (None, "empty")
    return None, last


def ping():
    """Small check for the admin /status command."""
    if not enabled():
        return "❌ ما مفعّل (ماكو GROQ_API_KEY)"
    data, how = read_cv("الاسم: علي حسن كريم\nالمحافظة: بغداد\nالوظيفة: مندوب مبيعات")
    if data:
        return f"✅ شغال ({how}) — {data.get('full_name_en')}, {data.get('city_en')}"
    return f"⚠️ ما اشتغل: {how}"
