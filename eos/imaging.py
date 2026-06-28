"""Pillow pipeline: sRGB convert, orientation bake, derivatives, RE export crops."""

import io
import logging

from PIL import Image, ImageCms, ImageFilter, ImageOps

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:  # pragma: no cover
    pass

log = logging.getLogger("eos.imaging")

PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".tif", ".tiff"}

_SRGB = ImageCms.createProfile("sRGB")

_ANCHORS = {
    "tl": (0.0, 0.0), "tc": (0.5, 0.0), "tr": (1.0, 0.0),
    "ml": (0.0, 0.5), "c": (0.5, 0.5), "mr": (1.0, 0.5),
    "bl": (0.0, 1.0), "bc": (0.5, 1.0), "br": (1.0, 1.0),
}


def _to_srgb(img: Image.Image) -> Image.Image:
    icc = img.info.get("icc_profile")
    if icc:
        try:
            src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            img = ImageCms.profileToProfile(img, src, _SRGB, outputMode="RGB")
        except Exception as e:
            log.warning("ICC convert failed (%s) — assuming sRGB", e)
            img = img.convert("RGB")
    elif img.mode != "RGB":
        img = img.convert("RGB")
    return img


def _apply_overlay(crop: Image.Image, overlay: dict) -> Image.Image:
    cw, ch = crop.size
    with Image.open(overlay["path"]) as raw:
        logo = raw.convert("RGBA")
    target_w = max(1, round(cw * overlay["scale_pct"] / 100))
    target_h = max(1, round(logo.height * target_w / logo.width))
    logo = logo.resize((target_w, target_h), Image.LANCZOS)

    opacity = overlay["opacity"] / 100
    if opacity < 1:
        alpha = logo.getchannel("A").point(lambda a: round(a * opacity))
        logo.putalpha(alpha)

    margin = round(cw * overlay["margin_pct"] / 100)
    fx, fy = _ANCHORS.get(overlay["position"], _ANCHORS["br"])
    x = round((cw - target_w - 2 * margin) * fx) + margin
    y = round((ch - target_h - 2 * margin) * fy) + margin

    base = crop.convert("RGBA")
    blur = max(2, round(target_w * 0.03))
    pad = blur * 2
    sh = Image.new("L", (target_w + 2 * pad, target_h + 2 * pad), 0)
    sh.paste(logo.getchannel("A"), (pad, pad))
    sh = sh.filter(ImageFilter.GaussianBlur(blur)).point(lambda a: round(a * 0.7))
    shadow = Image.new("RGBA", sh.size, (0, 0, 0, 0))
    shadow.putalpha(sh)
    off = max(1, round(target_h * 0.02))
    base.alpha_composite(shadow, (x - pad, y - pad + off))
    base.alpha_composite(logo, (x, y))
    return base.convert("RGB")


def make_crops(src_path: str, out_dir, stem: str, quality: int,
               presets, overlay: dict | None = None) -> list[str]:
    written = []
    with Image.open(src_path) as im:
        im = ImageOps.exif_transpose(im)
        im = _to_srgb(im)
        for ps in presets:
            crop = ImageOps.fit(
                im, (ps["width"], ps["height"]), Image.LANCZOS,
                centering=(ps["centering_x"], ps["centering_y"]),
            )
            if ps["brand_overlay"] and overlay:
                crop = _apply_overlay(crop, overlay)
            out = out_dir / f"{stem}_{ps['slug']}.jpg"
            crop.save(out, "JPEG", quality=quality, progressive=True, optimize=True)
            written.append(out.name)
    return written


def make_derivatives(src_path: str, web_path: str, thumb_path: str,
                     web_max: int, thumb_max: int, quality: int) -> tuple[int, int]:
    with Image.open(src_path) as im:
        im = ImageOps.exif_transpose(im)
        w, h = im.size
        im = _to_srgb(im)

        web = im.copy()
        web.thumbnail((web_max, web_max), Image.LANCZOS)
        web.save(web_path, "JPEG", quality=quality, progressive=True, optimize=True)

        im.thumbnail((thumb_max, thumb_max), Image.LANCZOS)
        im.save(thumb_path, "JPEG", quality=quality, progressive=True, optimize=True)
    return w, h