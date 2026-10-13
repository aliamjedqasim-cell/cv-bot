"""Shared by the voice step (GitHub Actions) and the video renderer: reads a lesson script and splits the
narration into sentences in one fixed way, so every sentence finds its audio file."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VOICE = "ar-SA-HamedNeural"   # Modern Standard Arabic, male
RATE = "-4%"


def sentences(text):
    parts = re.split(r"(?<=[.!؟?])\s+", " ".join(str(text or "").split()))
    return [p.strip() for p in parts if p.strip()]


def load(path):
    return json.loads(Path(path).read_text("utf-8"))


def all_sentences(lesson):
    out = []
    for seg in lesson["segments"]:
        out += sentences(seg.get("say"))
        for point in seg.get("points") or []:
            out += sentences(point.get("say"))
    return out


def lessons():
    return sorted((ROOT / "scripts").glob("*.json"))
