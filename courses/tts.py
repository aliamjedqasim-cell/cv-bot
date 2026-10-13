"""Runs on GitHub Actions (it has internet): makes one MP3 per narration sentence with a natural Arabic voice.
Files are named by a hash of voice + speed + text, so only new or changed sentences are generated again."""
import asyncio
import hashlib
import json
import sys

import edge_tts

from lesson import RATE, ROOT, VOICES, all_lines, audio_key, lessons, load


def key(voice, text):
    return hashlib.sha1(f"{voice}|{RATE}|{text}".encode("utf-8")).hexdigest()[:14]


async def make(text, voice, path, tries=4):
    for attempt in range(tries):
        try:
            await edge_tts.Communicate(text, voice, rate=RATE).save(str(path))
            if path.exists() and path.stat().st_size > 1000:
                return True
        except Exception as e:
            print("retry", voice, attempt, e, file=sys.stderr)
            await asyncio.sleep(2 + attempt * 3)
    path.unlink(missing_ok=True)
    return False


dead, used = set(), {}  # voices that gave nothing this run; role -> voice actually used


async def speak(role, text, out):
    """Tries the role's voices in order (skipping ones that already failed); returns the file name, or None."""
    voices = VOICES.get(role, VOICES["narrator"])
    for voice in voices:
        if voice in dead and voice != voices[-1]:
            continue
        path = out / f"{key(voice, text)}.mp3"
        if path.exists() or await make(text, voice, path, tries=4 if voice == voices[-1] else 2):
            used[role] = voice
            return path.name
        dead.add(voice)
    return None


async def main():
    failed = 0
    for script in lessons():
        lesson = load(script)
        out = ROOT / "audio" / script.stem
        out.mkdir(parents=True, exist_ok=True)
        old_path = out / "manifest.json"
        old = json.loads(old_path.read_text("utf-8")) if old_path.exists() else {}
        manifest = {}
        for role, text in all_lines(lesson):
            k = audio_key(role, text)
            name = old.get(k) if old.get(k) and (out / old[k]).exists() else await speak(role, text, out)
            if not name:
                failed += 1
                continue
            manifest[k] = name
        (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), "utf-8")
        keep = set(manifest.values())
        for stale in out.glob("*.mp3"):
            if stale.name not in keep:
                stale.unlink()
        print(script.stem, len(manifest), "sentences")
    if used:
        print("::notice title=voices::" + ", ".join(f"{r}={v}" for r, v in sorted(used.items())))
    if failed:
        print("failed sentences:", failed, file=sys.stderr)
        sys.exit(1)


asyncio.run(main())
