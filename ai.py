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

import requests

log = logging.getLogger("cvbot.ai")
http = requests.Session()
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
    "experiences": {"type": "array", "items": JOB},
    "customers_en": _list(), "products_en": _text(), "products_ar": _text(),
    "territory_en": _text(), "territory_ar": _text(),
    "survey": {"type": ["boolean", "null"]}, "licence": _text(),
    "skills_en": _list(), "skills_ar": _list(), "english_level": _text(),
    "other_languages_en": _list(), "courses_en": _list(), "courses_ar": _list(),
    "profile_en": _text(), "objective_en": _text(),
})

SYSTEM = """You are an expert Iraqi recruiter and professional CV writer. You receive a job seeker's message, usually in Iraqi Arabic (sometimes mixed with English, sometimes a filled-in form, sometimes free text). Turn it into structured data for a polished, professional English CV for the Iraqi medical and sales job market.

TRUTH RULES (most important):
- Use only facts the person stated. Never invent employers, job titles, dates, durations, degrees, universities, languages, certificates, numbers, sales figures, targets achieved or awards.
- If something is not mentioned, use null (or [] for lists). Do not guess.

WRITING RULES:
- All *_en fields: correct, polished, professional English that presents the candidate in the strongest truthful light.
- Names of people: transliterate with common Iraqi English spelling (محمد Mohammed, علي Ali, حسين Hussein, جاسم Jasim, كاظم Kadhim, عبد الكريم Abdulkareem, الربيعي Al-Rubaie). Never translate the meaning of a name.
- Companies and places: keep known brands in their English form (بيونير Pioneer, اسيا سيل Asiacell, الحكمة Hikma); transliterate Arabic proper names (صيدلية النور Al-Noor Pharmacy); use standard spellings for Iraqi places (الكرخ Karkh, المنصور Al-Mansour, العمارة Amarah, الناصرية Nasiriyah).
- job_title_en: the position they want, as a standard English job title (e.g. Medical Representative, Medical Sales Representative, Sales Representative, Pharmacist).
- degree_level: exactly one of "Bachelor's Degree", "Diploma", "Master's Degree", "PhD", "High School Diploma", or null. specialty_en: the field only (e.g. Pharmacy, Civil Engineering, Medical Laboratory Sciences).
- edu_status: the 4-digit graduation year if graduated, or "1st-Year Student" ... "Final-Year Student" if still studying, else null.
- experiences: one item per job. duties_en: 2-4 strong bullet points starting with action verbs, built from what they described and what that job title normally involves; if they described nothing, write 3 typical, realistic responsibilities for that job title. Never add numbers or achievements they did not state. duration_en like "2 years", "6 months" or "2021 - Present".
- customers_en: only from Doctors, Pharmacies, Clinics, Hospitals, Shops & Markets, Companies, as stated.
- licence: "car" (driving licence and a car), "licence" (licence, no car), "none" (said no), or null if not mentioned. survey: true if they did a field/market survey, false if they said no, null if not mentioned.
- skills_en: 9 to 12 skills: first the ones they mentioned (translated), then fitting professional skills for the job they want (communication, negotiation, etc.).
- english_level: exactly one of "Fluent", "Very Good", "Good", "Intermediate", "Basic", "none", or null. other_languages_en: languages other than Arabic and English.
- profile_en: 2-3 confident sentences, without "I", e.g. "Highly motivated Medical Sales Representative with ...". Mention companies or years only if stated.
- objective_en: 1-2 sentences of career objective for the job they want.
- age: digits only if stated (convert a birth year to an age in 2026), else null.
- *_ar fields: copy the person's own words for that item (short), or null. skills_ar / courses_ar / duties_ar: their own words as a list.

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


def _body(model, text, minimal=False):
    body = {"model": model, "temperature": 0.3, "max_completion_tokens": 3500,
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": text}]}
    if minimal:
        body["messages"][0]["content"] += "\nJSON keys: " + ", ".join(SCHEMA["properties"]) + \
            ". experiences items keys: " + ", ".join(JOB["properties"]) + "."
        body["response_format"] = {"type": "json_object"}
        return body
    if "gpt-oss" in model or "qwen" in model:
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": "cv_data", "strict": True, "schema": SCHEMA}}
    else:
        return _body(model, text, minimal=True)
    if "gpt-oss" in model:
        body["reasoning_effort"] = "low"
        body["include_reasoning"] = False
    elif "qwen" in model:
        body["reasoning_effort"] = "none"
        body["reasoning_format"] = "hidden"
    return body


def read_cv(text):
    """Returns (data dict, model name) or (None, reason)."""
    key = api_key()
    if not key:
        return None, "no key"
    text = (text or "")[:3500]
    last = "failed"
    for model in models():
        for minimal in (False, True):  # if a model refuses the advanced options, retry plainly once
            try:
                r = http.post(GROQ_URL, json=_body(model, text, minimal),
                              headers={"Authorization": f"Bearer {key}"}, timeout=45)
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


def ping():
    """Small check for the admin /status command."""
    if not enabled():
        return "❌ ما مفعّل (ماكو GROQ_API_KEY)"
    data, how = read_cv("الاسم: علي حسن كريم\nالمحافظة: بغداد\nالوظيفة: مندوب مبيعات")
    if data:
        return f"✅ شغال ({how}) — {data.get('full_name_en')}, {data.get('city_en')}"
    return f"⚠️ ما اشتغل: {how}"
