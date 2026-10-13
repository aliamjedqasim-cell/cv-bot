"""Runs on GitHub Actions (it has internet): makes one MP3 per narration sentence with a natural Arabic voice.
Files are named by a hash of voice + speed + text, so only new or changed sentences are generated again."""
import asyncio
import hashlib
import json
import sys

import edge_tts

from lesson import RATE, ROOT, VOICE, all_sentences, lessons, load


def key(text):
    return hashlib.sha1(f"{VOICE}|{RATE}|{text}".encode("utf-8")).hexdigest()[:14]


async def make(text, path, tries=4):
    for attempt in range(tries):
        try:
            await edge_tts.Communicate(text, VOICE, rate=RATE).save(str(path))
            if path.stat().st_size > 1000:
                return True
        except Exception as e:
            print("retry", attempt, e, file=sys.stderr)
            await asyncio.sleep(2 + attempt * 3)
    return False


async def main():
    failed = 0
    for script in lessons():
        lesson = load(script)
        out = ROOT / "audio" / script.stem
        out.mkdir(parents=True, exist_ok=True)
        manifest = {}
        for text in all_sentences(lesson):
            path = out / f"{key(text)}.mp3"
            if not path.exists() and not await make(text, path):
                failed += 1
                continue
            manifest[text] = path.name
        (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), "utf-8")
        used = set(manifest.values())
        for old in out.glob("*.mp3"):
            if old.name not in used:
                old.unlink()
        print(script.stem, len(manifest), "sentences")
    if failed:
        print("failed sentences:", failed, file=sys.stderr)
        sys.exit(1)


asyncio.run(main())
