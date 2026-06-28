"""Social graphics and flyer PDF for listing marketing kits."""

import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

log = logging.getLogger("eos.marketing_graphics")

_IG_SQUARE = (1080, 1080)
_IG_STORY = (1080, 1920)


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size)
    except OSError:
        return ImageFont.load_default()


def _fit_cover(src: Image.Image, size: tuple[int, int]) -> Image.Image:
    return ImageOps.fit(src, size, Image.LANCZOS, centering=(0.5, 0.4))


def _draw_caption(img: Image.Image, *, headline: str, subline: str) -> Image.Image:
    out = img.convert("RGBA")
    w, h = out.size
    bar_h = max(120, h // 5)
    overlay = Image.new("RGBA", (w, bar_h), (26, 35, 50, 210))
    out.paste(overlay, (0, h - bar_h), overlay)
    draw = ImageDraw.Draw(out)
    title_font = _load_font(max(28, w // 22))
    sub_font = _load_font(max(20, w // 32))
    draw.text((48, h - bar_h + 28), headline, fill=(255, 255, 255, 255), font=title_font)
    if subline:
        draw.text(
            (48, h - bar_h + 28 + title_font.size + 12),
            subline,
            fill=(228, 232, 240, 255),
            font=sub_font,
        )
    return out.convert("RGB")


def build_ig_square(cover_path: str, headline: str, subline: str, out_path: Path) -> None:
    with Image.open(cover_path) as raw:
        img = _fit_cover(ImageOps.exif_transpose(raw).convert("RGB"), _IG_SQUARE)
    img = _draw_caption(img, headline=headline, subline=subline)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "JPEG", quality=90, optimize=True)


def build_ig_story(cover_path: str, headline: str, subline: str, out_path: Path) -> None:
    with Image.open(cover_path) as raw:
        img = _fit_cover(ImageOps.exif_transpose(raw).convert("RGB"), _IG_STORY)
    img = _draw_caption(img, headline=headline, subline=subline)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "JPEG", quality=90, optimize=True)


def build_flyer(
    cover_path: str,
    *,
    headline: str,
    subline: str,
    specs: str,
    agent_line: str,
    studio_line: str,
    out_path: Path,
) -> None:
    page_w, page_h = 2550, 3300
    with Image.open(cover_path) as raw:
        hero = _fit_cover(ImageOps.exif_transpose(raw).convert("RGB"), (page_w, 1800))

    page = Image.new("RGB", (page_w, page_h), (248, 246, 242))
    page.paste(hero, (0, 0))

    draw = ImageDraw.Draw(page)
    title_font = _load_font(96)
    body_font = _load_font(52)
    small_font = _load_font(44)

    y = 1880
    draw.text((120, y), headline, fill=(26, 35, 50), font=title_font)
    y += title_font.size + 24
    if subline:
        draw.text((120, y), subline, fill=(107, 114, 128), font=body_font)
        y += body_font.size + 36
    if specs:
        draw.text((120, y), specs, fill=(26, 35, 50), font=body_font)
        y += body_font.size + 48
    if agent_line:
        draw.text((120, y), agent_line, fill=(196, 125, 42), font=small_font)
        y += small_font.size + 20
    if studio_line:
        draw.text((120, y), studio_line, fill=(107, 114, 128), font=small_font)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    page.save(out_path, "PDF", resolution=150.0)
