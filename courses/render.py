"""Renders a lesson as a vertical (9:16) video: digital navy/teal design, points that appear with the
narration, subtitles, progress bar, Masar intro and outro. Needs ffmpeg and the narration MP3s that
GitHub Actions makes (courses/audio/<lesson>/). Usage: python render.py [lesson-stem ...]"""
import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from lesson import ROOT, lessons, load, sentences

W, H, FPS, RATE = 720, 1280, 30, 24000
NAVY, NAVY2 = (8, 24, 50), (3, 10, 26)
TEAL, MINT, WHITE, GREY = (0, 196, 170), (110, 232, 196), (255, 255, 255), (168, 184, 202)
FONTS = ROOT / "fonts"
LOGO = ROOT.parent / "assets" / "avatar.jpg"
AR_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")
PAUSE, SEG_PAUSE, ANIM = 0.28, 0.55, 0.4


def font(weight, size):
    return ImageFont.truetype(str(FONTS / f"Tajawal-{weight}.ttf"), size)


def ar(n):
    return str(n).translate(AR_DIGITS)


def ease(t):
    return 1 - (1 - max(0.0, min(1.0, t))) ** 3


def wrap(draw, text, f, max_w):
    lines, cur = [], ""
    for word in str(text).split():
        trial = (cur + " " + word).strip()
        if cur and draw.textlength(trial, font=f, direction="rtl") > max_w:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    return lines + ([cur] if cur else [])


# ------------------------------------------------------------- static layers
def background():
    small = Image.new("RGB", (90, 160))
    px = small.load()
    for y in range(160):
        for x in range(90):
            t = min(1, (y / 160) * 0.85 + (x / 90) * 0.15)
            px[x, y] = tuple(int(NAVY[i] + (NAVY2[i] - NAVY[i]) * t) for i in range(3))
    bg = small.resize((W, H), Image.BICUBIC).convert("RGBA")
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    g = ImageDraw.Draw(glow)
    g.ellipse((-260, -200, 380, 420), fill=TEAL + (70,))
    g.ellipse((420, 900, 1060, 1520), fill=(40, 90, 200, 60))
    bg = Image.alpha_composite(bg, glow.filter(ImageFilter.GaussianBlur(120)))
    grid = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(grid)
    for x in range(0, W, 48):
        d.line((x, 0, x, H), fill=(255, 255, 255, 9))
    for y in range(0, H, 48):
        d.line((0, y, W, y), fill=(255, 255, 255, 9))
    for i in range(26):  # small "data" dots
        x, y = (i * 137) % W, (i * 291 + 80) % H
        d.ellipse((x - 2, y - 2, x + 2, y + 2), fill=MINT + (60,))
    return Image.alpha_composite(bg, grid)


def logo(size):
    im = Image.open(LOGO).convert("RGB").resize((size, size), Image.LANCZOS)
    m = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(m).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(im, (0, 0), m.resize((size, size), Image.LANCZOS))
    return out


def header(lesson):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    layer.alpha_composite(logo(78), (W - 40 - 78, 40))
    d.text((W - 40 - 78 - 16, 48), "مسار للتوظيف الطبي", font=font("Bold", 30), fill=WHITE, anchor="ra", direction="rtl")
    d.text((W - 40 - 78 - 16, 88), lesson["course_title"], font=font("Regular", 23), fill=GREY, anchor="ra", direction="rtl")
    chip = f"الدرس {ar(lesson['number'])} من {ar(lesson['of'])}"
    f = font("Medium", 22)
    w = d.textlength(chip, font=f, direction="rtl")
    d.rounded_rectangle((40, 58, 40 + w + 34, 100), 21, outline=TEAL + (255,), width=2, fill=(0, 196, 170, 30))
    d.text((40 + (w + 34) / 2, 79), chip, font=f, fill=MINT, anchor="mm", direction="rtl")
    return layer


# ------------------------------------------------------------------ frames
def subtitle_box(img, text, progress):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    if text:
        size = 33
        while True:
            f = font("Medium", size)
            lines = wrap(d, text, f, W - 130)
            if len(lines) <= 3 or size <= 25:
                break
            size -= 2
        lines = lines[:4]
        lh = int(size * 1.45)
        box_h = lh * len(lines) + 36
        top = 1196 - box_h
        d.rounded_rectangle((40, top, W - 40, 1196), 26, fill=(0, 0, 0, 135), outline=(255, 255, 255, 30), width=1)
        for i, line in enumerate(lines):
            d.text((W / 2, top + 18 + i * lh), line, font=f, fill=WHITE, anchor="ma", direction="rtl")
    d.rounded_rectangle((40, 1226, W - 40, 1234), 4, fill=(255, 255, 255, 34))
    if progress > 0:
        d.rounded_rectangle((W - 40 - (W - 80) * min(1, progress), 1226, W - 40, 1234), 4, fill=TEAL + (255,))
    return Image.alpha_composite(img, layer)


def card(layer, top, text, num, active, alpha, done=False):
    d = ImageDraw.Draw(layer)
    a = int(255 * alpha)
    f = font("Bold", 38)
    lines = wrap(d, text, f, W - 80 - 130)[:2]
    hgt = 50 * len(lines) + 52
    fill = (0, 196, 170, int(46 * alpha)) if active else (255, 255, 255, int(16 * alpha))
    edge = TEAL + (a,) if active else (255, 255, 255, int(40 * alpha))
    d.rounded_rectangle((40, top, W - 40, top + hgt), 22, fill=fill, outline=edge, width=3 if active else 1)
    cx, cy = W - 40 - 50, top + hgt / 2
    d.ellipse((cx - 27, cy - 27, cx + 27, cy + 27), fill=(TEAL if active or done else (60, 80, 110)) + (a,))
    if done:
        d.line((cx - 12, cy + 1, cx - 3, cy + 10, cx + 13, cy - 9), fill=(3, 10, 26, a), width=6, joint="curve")
    else:
        d.text((cx, cy + 2), ar(num), font=font("ExtraBold", 30), fill=(3, 10, 26, a) if active else WHITE + (a,),
               anchor="mm")
    for i, line in enumerate(lines):
        d.text((W - 40 - 96, top + 24 + i * 50), line, font=f, fill=WHITE + (a,), anchor="ra", direction="rtl")
    return hgt


def frame(base, lesson, seg, revealed, active, sub, progress, anim=None, seg_no=0):
    """anim = (what, t): 'title' / 'heading' / 'point' fading in."""
    img = base.copy()
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    kind = seg["type"]
    if kind == "title":
        t = ease(anim[1]) if anim and anim[0] == "title" else 1
        lg = logo(230)
        lg.putalpha(lg.getchannel("A").point(lambda v: int(v * t)))
        layer.alpha_composite(lg, ((W - 230) // 2, int(300 - 30 * (1 - t))))
        f = font("ExtraBold", 66)
        lines = wrap(d, lesson["title"], f, W - 120)
        for i, line in enumerate(lines):
            d.text((W / 2, 600 + i * 84 + 30 * (1 - t)), line, font=f, fill=WHITE + (int(255 * t),), anchor="ma",
                   direction="rtl")
        y = 600 + len(lines) * 84 + 24
        d.rounded_rectangle((W / 2 - 120, y, W / 2 + 120, y + 6), 3, fill=TEAL + (int(255 * t),))
        d.text((W / 2, y + 30), lesson["course_title"], font=font("Medium", 30), fill=GREY + (int(255 * t),),
               anchor="ma", direction="rtl")
    elif kind == "outro":
        t = ease(anim[1]) if anim else 1
        lg = logo(260)
        lg.putalpha(lg.getchannel("A").point(lambda v: int(v * t)))
        layer.alpha_composite(lg, ((W - 260) // 2, 330))
        d.text((W / 2, 650), "نراك في الدرس القادم", font=font("ExtraBold", 54), fill=WHITE + (int(255 * t),),
               anchor="ma", direction="rtl")
        d.text((W / 2, 730), "مسار للتوظيف الطبي", font=font("Bold", 34), fill=MINT + (int(255 * t),), anchor="ma",
               direction="rtl")
    else:
        th = ease(anim[1]) if anim and anim[0] == "heading" else 1
        cx, cy = W - 40 - 34, 196 + 34
        if kind == "points":
            d.ellipse((cx - 34, cy - 34, cx + 34, cy + 34), fill=TEAL + (int(255 * th),))
            d.text((cx, cy + 2), ar(seg_no), font=font("ExtraBold", 38), fill=(3, 10, 26, int(255 * th)), anchor="mm")
            right = W - 40 - 84
        else:
            right = W - 40
        f = font("ExtraBold", 56)
        hl = wrap(d, seg["heading"], f, right - 40)[:2]
        for i, line in enumerate(hl):
            d.text((right, 196 + i * 66 + 16 * (1 - th)), line, font=f, fill=WHITE + (int(255 * th),), anchor="ra",
                   direction="rtl")
        y = 196 + len(hl) * 66 + 18
        d.rounded_rectangle((right - 90 * th, y, right, y + 6), 3, fill=TEAL + (255,))
        y += 46
        items = seg.get("points") or [{"text": x} for x in seg.get("items") or []]
        for i, item in enumerate(items[:revealed]):
            alpha, dy = 1, 0
            if anim and anim[0] == "point" and i == revealed - 1:
                alpha, dy = ease(anim[1]), 26 * (1 - ease(anim[1]))
            tmp = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            hgt = card(tmp, int(y + dy), item["text"], i + 1, kind == "points" and i == active, alpha,
                       done=kind == "summary")
            layer.alpha_composite(tmp)
            y += hgt + 22
    img = Image.alpha_composite(img, layer)
    return subtitle_box(img, sub, progress)


# ---------------------------------------------------------------- timeline
def pcm(mp3):
    return subprocess.run(["ffmpeg", "-v", "error", "-i", str(mp3), "-ar", str(RATE), "-ac", "1", "-f", "s16le", "-"],
                          capture_output=True, check=True).stdout


def build(stem):
    lesson = load(ROOT / "scripts" / f"{stem}.json")
    audio_dir = ROOT / "audio" / stem
    manifest = json.loads((audio_dir / "manifest.json").read_text("utf-8"))
    segs = lesson["segments"] + [{"type": "outro"}]
    events = []  # (kind, payload, seconds, audio bytes)

    def silence(sec):
        return b"\x00\x00" * int(RATE * sec)

    seg_no = 0
    for si, seg in enumerate(segs):
        if seg["type"] == "points":
            seg_no += 1
        if seg["type"] in ("title", "outro"):
            events.append(("anim", (si, 0, -1, "title" if seg["type"] == "title" else "outro", seg_no), 0.7, silence(0.7)))
            for s in sentences(seg.get("say")):
                data = pcm(audio_dir / manifest[s])
                events.append(("hold", (si, 0, -1, s, seg_no), len(data) / (2 * RATE) + PAUSE, data + silence(PAUSE)))
            if seg["type"] == "outro":
                events.append(("hold", (si, 0, -1, "", seg_no), 2.2, silence(2.2)))
            continue
        events.append(("anim", (si, 0, -1, "heading", seg_no), ANIM, silence(ANIM)))
        if seg["type"] == "summary":
            for i in range(len(seg["items"])):
                events.append(("anim", (si, i + 1, -1, "point", seg_no), 0.22, silence(0.22)))
            for s in sentences(seg.get("say")):
                data = pcm(audio_dir / manifest[s])
                events.append(("hold", (si, len(seg["items"]), -1, s, seg_no), len(data) / (2 * RATE) + PAUSE,
                               data + silence(PAUSE)))
        else:
            for s in sentences(seg.get("say")):
                data = pcm(audio_dir / manifest[s])
                events.append(("hold", (si, 0, -1, s, seg_no), len(data) / (2 * RATE) + PAUSE, data + silence(PAUSE)))
            for pi, point in enumerate(seg["points"]):
                events.append(("anim", (si, pi + 1, pi, "point", seg_no), ANIM, silence(ANIM)))
                for s in sentences(point.get("say")):
                    data = pcm(audio_dir / manifest[s])
                    events.append(("hold", (si, pi + 1, pi, s, seg_no), len(data) / (2 * RATE) + PAUSE,
                                   data + silence(PAUSE)))
        events.append(("hold", (si, len(seg.get("points") or seg.get("items") or []), -1, "", seg_no), SEG_PAUSE,
                       silence(SEG_PAUSE)))
    total = sum(e[2] for e in events)

    base = Image.alpha_composite(background(), header(lesson))
    tmp = Path(tempfile.mkdtemp(prefix=f"render_{stem}_"))
    lines, n, t, last_sub = [], 0, 0.0, ""
    for kind, (si, revealed, active, what, seg_no), sec, _ in events:
        seg = segs[si]
        if kind == "anim":
            steps = max(2, int(sec * FPS / 2))
            for k in range(steps):
                img = frame(base, lesson, seg, revealed, active if what == "point" else -1,
                            "" if what in ("title", "outro") else last_sub if seg["type"] == "summary" else "",
                            (t + sec * k / steps) / total, (what, (k + 1) / steps), seg_no)
                path = tmp / f"{n:05d}.png"
                img.convert("RGB").save(path, compress_level=1)
                lines += [f"file '{path}'", f"duration {sec / steps:.4f}"]
                n += 1
        else:
            last_sub = what
            img = frame(base, lesson, seg, revealed, active, what, (t + sec) / total, None, seg_no)
            path = tmp / f"{n:05d}.png"
            img.convert("RGB").save(path, compress_level=1)
            lines += [f"file '{path}'", f"duration {sec:.4f}"]
            n += 1
        t += sec
    lines.append(lines[-2])  # ffmpeg's concat needs the last image twice
    (tmp / "frames.txt").write_text("\n".join(lines))
    wav = tmp / "audio.wav"
    import wave
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"".join(e[3] for e in events))
    out = ROOT / "videos" / f"{stem}.mp4"
    out.parent.mkdir(exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(tmp / "frames.txt"),
                    "-i", str(wav), "-vf", f"fps={FPS},format=yuv420p", "-c:v", "libx264", "-preset", "medium",
                    "-crf", "25", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", "-shortest", str(out)],
                   check=True)
    print(f"{stem}: {total:.0f}s, {n} frames, {out.stat().st_size // 1024} KB -> {out}")
    return out


if __name__ == "__main__":
    for stem in sys.argv[1:] or [p.stem for p in lessons()]:
        build(stem)
