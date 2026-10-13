"""Shared by the voice step (GitHub Actions) and the video renderer: reads a lesson script and splits the
narration into sentences in one fixed way, so every sentence finds its audio file."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VOICE = "ar-SA-HamedNeural"   # Modern Standard Arabic, male: the narrator and the coach in the mock interview
RATE = "-4%"
# the people in a mock interview (dialogue segments), each with a fallback in case a voice is not offered
VOICES = {"narrator": [VOICE], "coach": [VOICE],
          "interviewer": ["ar-SA-ZariyahNeural", "ar-AE-FatimaNeural", "ar-EG-SalmaNeural"],
          "candidate": ["ar-IQ-BasselNeural", "ar-AE-HamdanNeural", "ar-EG-ShakirNeural"]}


def sentences(text):
    parts = re.split(r"(?<=[.!؟?])\s+", " ".join(str(text or "").split()))
    return [p.strip() for p in parts if p.strip()]


def load(path):
    return json.loads(Path(path).read_text("utf-8"))


def audio_key(role, text):
    """The narrator's sentences are keyed by their text alone (as before); the other voices by role too."""
    return text if role in ("narrator", "coach") else f"{role}|{text}"


def all_lines(lesson):
    """Every sentence to be spoken, as (role, sentence)."""
    out = []
    for seg in lesson["segments"]:
        out += [("narrator", s) for s in sentences(seg.get("say"))]
        for point in seg.get("points") or []:
            out += [("narrator", s) for s in sentences(point.get("say"))]
        for line in seg.get("lines") or []:
            out += [(line["who"], s) for s in sentences(line.get("say"))]
    return out


def all_sentences(lesson):
    return [s for _, s in all_lines(lesson)]


def lessons():
    return sorted((ROOT / "scripts").glob("*.json"))
