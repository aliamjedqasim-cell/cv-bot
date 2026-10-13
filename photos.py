"""The applicant's photo: a face-centred square crop (head and shoulders) and a light, natural correction
of brightness, contrast and sharpness. OpenCV finds the face when it is installed; otherwise the crop
keeps the upper middle of the picture, where the face usually is."""
import os
from io import BytesIO

from PIL import Image, ImageEnhance, ImageFilter, ImageOps, ImageStat

_detector = None


def _face_detector():
    global _detector
    if _detector is None:
        _detector = False
        try:
            import cv2
            paths = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "face.xml")]
            if hasattr(cv2, "data") and hasattr(cv2, "CascadeClassifier"):
                paths.insert(0, os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml"))
            for path in paths:
                if os.path.exists(path) and hasattr(cv2, "CascadeClassifier"):
                    det = cv2.CascadeClassifier(path)
                    if not det.empty():
                        _detector = det
                        break
        except Exception:
            _detector = False
    return _detector or None


def detector_ready():
    return _face_detector() is not None


def find_face(im):
    """(x, y, w, h) of the biggest face, or None."""
    det = _face_detector()
    if not det:
        return None
    try:
        import cv2
        import numpy as np
        scale = min(1.0, 900 / max(im.size))
        small = im.resize((int(im.width * scale), int(im.height * scale))) if scale < 1 else im
        gray = cv2.cvtColor(np.array(small.convert("RGB")), cv2.COLOR_RGB2GRAY)
        side = max(40, min(small.size) // 10)
        faces = det.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(side, side))
        if len(faces) == 0:
            return None
        x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
        return tuple(int(v / scale) for v in (x, y, w, h))
    except Exception:
        return None


def crop_box(size, face):
    """Square crop: head and shoulders when the face is known, else the upper middle."""
    W, H = size
    if face:
        x, y, w, h = face
        side = min(int(max(w, h) * 2.6), W, H)
        cx, cy = x + w / 2, y + h / 2 + h * 0.35  # a little below the face: shoulders in frame
    else:
        side = min(W, H)
        cx, cy = W / 2, (side / 2 + (H - side) / 4) if H > W else H / 2
    left = int(min(max(cx - side / 2, 0), W - side))
    top = int(min(max(cy - side / 2, 0), H - side))
    return left, top, left + side, top + side


def edit_photo(data):
    """Returns (edited JPEG bytes, face_found). Raises if the bytes are not an image."""
    im = ImageOps.exif_transpose(Image.open(BytesIO(data))).convert("RGB")
    face = find_face(im)
    im = im.crop(crop_box(im.size, face)).resize((800, 800), Image.LANCZOS)
    im = ImageOps.autocontrast(im, cutoff=1, preserve_tone=True)
    lum = ImageStat.Stat(im.convert("L")).mean[0]
    if lum < 105:  # dark photo
        im = ImageEnhance.Brightness(im).enhance(min(1.3, 115 / max(lum, 1)))
    elif lum > 195:  # washed out
        im = ImageEnhance.Brightness(im).enhance(0.95)
    im = ImageEnhance.Color(im).enhance(1.05)
    im = im.filter(ImageFilter.UnsharpMask(radius=1.2, percent=60, threshold=3))
    buf = BytesIO()
    im.save(buf, "JPEG", quality=92)
    return buf.getvalue(), bool(face)
