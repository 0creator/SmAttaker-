"""
SmAttaker — TP Celebration Images
=====================================
Generates a share-worthy result card on every winning trade close,
scaled to how good the win actually was — a full 2R+ target hit gets
the most elaborate treatment; a small early-close win still looks
clean and branded, just understated. Losses NEVER get an image (see
trade_notify.py) — only a plain, honest text notification with the
exit zone and loss %. Celebrating losses, even gently, teaches the
wrong lesson.

── Design decisions worth knowing before touching this file ──

1. NO emoji glyphs drawn INTO the image. DejaVu Sans (the font used —
   see `_load_font()`) has no color-emoji glyphs; drawing "🏆" would
   render as a blank box (tofu), not a trophy. Every icon here is
   drawn with PIL primitives (circles, arcs, polygons) instead —
   which also looks sharper at 1080px than a rasterized emoji would.
   Emoji are fine in the Telegram *caption* (real emoji font there),
   just never burned into the PNG itself.

2. NO Arabic text drawn INTO the image, for the same class of reason:
   PIL has no bidi/shaping engine, so Arabic glyphs would render
   disconnected and in visual (wrong) order. All image text is Latin.
   Arabic goes in the caption (trade_notify.py), which Telegram
   renders correctly.

3. One parameterized layout, not 16 hand-built templates. Asset class
   (crypto/forex/gold/stocks) selects a color palette + a hand-drawn
   glyph; win tier (1–4) selects how elaborate the treatment is (glow
   intensity, border ornamentation, badge copy). 4 × 4 = 16 genuinely
   distinct outputs from one well-factored function — the same
   engineering principle as backend/utils/permissions.py's tier table:
   a small number of composable dimensions beats 16 near-duplicate
   functions that will inevitably drift out of sync with each other.
"""
import io
import math
from typing import Optional

from PIL import Image, ImageDraw, ImageFont, ImageFilter

CANVAS = 1080


# ── Asset-class visual themes ───────────────────────────
ASSET_THEMES = {
    "crypto": {
        "top": (18, 12, 41), "bottom": (76, 29, 149), "accent": (34, 211, 238),
        "glow": (124, 58, 237), "label": "CRYPTO",
    },
    "forex": {
        "top": (8, 20, 32), "bottom": (14, 116, 179), "accent": (125, 211, 252),
        "glow": (14, 165, 233), "label": "FOREX",
    },
    "gold": {
        "top": (26, 18, 6), "bottom": (120, 84, 15), "accent": (240, 214, 131),
        "glow": (212, 175, 55), "label": "METALS",
    },
    "stocks": {
        "top": (6, 20, 14), "bottom": (21, 107, 60), "accent": (134, 239, 172),
        "glow": (22, 163, 74), "label": "STOCKS",
    },
}
DEFAULT_THEME = ASSET_THEMES["crypto"]

WHITE = (245, 247, 250)
MUTED = (170, 180, 200)


def _theme_for(asset_class: str) -> dict:
    return ASSET_THEMES.get((asset_class or "").lower(), DEFAULT_THEME)


# ── Tier definitions ─────────────────────────────────────
def tier_for_r_multiple(r_multiple: Optional[float]) -> dict:
    """Maps R-multiple to a decoration tier. Tier 1 = most elaborate."""
    r = r_multiple if r_multiple is not None else 0.0
    if r >= 2.0:
        return {"tier": 1, "badge": "LEGENDARY WIN", "glow_layers": 5, "border": "double", "particles": True}
    if r >= 1.5:
        return {"tier": 2, "badge": "STRONG WIN", "glow_layers": 3, "border": "single", "particles": False}
    if r >= 1.0:
        return {"tier": 3, "badge": "SOLID WIN", "glow_layers": 1, "border": "thin", "particles": False}
    return {"tier": 4, "badge": "WIN", "glow_layers": 0, "border": "none", "particles": False}


# ── Fonts (DejaVu, bundled with matplotlib — always present since
# matplotlib is already a hard dependency; see requirements.txt) ────
def _font_paths() -> tuple[str, str]:
    candidates_bold = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    candidates_regular = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    try:
        import matplotlib
        mpl_fonts = matplotlib.get_data_path() + "/fonts/ttf"
        candidates_bold.insert(0, f"{mpl_fonts}/DejaVuSans-Bold.ttf")
        candidates_regular.insert(0, f"{mpl_fonts}/DejaVuSans.ttf")
    except Exception:
        pass
    return candidates_bold, candidates_regular


_BOLD_CANDIDATES, _REGULAR_CANDIDATES = _font_paths()


def _load_font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    candidates = _BOLD_CANDIDATES if bold else _REGULAR_CANDIDATES
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


# ── Drawing helpers ──────────────────────────────────────
def _vertical_gradient(size: int, top: tuple, bottom: tuple) -> Image.Image:
    img = Image.new("RGB", (size, size), top)
    draw = ImageDraw.Draw(img)
    for y in range(size):
        t = y / size
        r = int(top[0] + (bottom[0] - top[0]) * t)
        g = int(top[1] + (bottom[1] - top[1]) * t)
        b = int(top[2] + (bottom[2] - top[2]) * t)
        draw.line([(0, y), (size, y)], fill=(r, g, b))
    return img


def _add_glow(base: Image.Image, center: tuple, color: tuple, layers: int) -> Image.Image:
    """Layered, blurred, semi-transparent circles behind the main
    content — cheap, reliable glow effect with no external assets."""
    if layers <= 0:
        return base
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    for i in range(layers):
        radius = 260 - i * 30
        alpha = max(10, 70 - i * 12)
        layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        d.ellipse(
            [center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius],
            fill=(*color, alpha),
        )
        layer = layer.filter(ImageFilter.GaussianBlur(radius=40))
        overlay = Image.alpha_composite(overlay, layer)
    return Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")


def _add_particles(base: Image.Image, color: tuple, seed: int = 7) -> Image.Image:
    """Deterministic 'confetti dot' scatter for the top tier only —
    same trade always renders identically (no randomness dependency
    on system entropy, matters for reproducibility/testing)."""
    import random
    rng = random.Random(seed)
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    for _ in range(60):
        x, y = rng.randint(0, base.size[0]), rng.randint(0, base.size[1])
        r = rng.randint(2, 5)
        alpha = rng.randint(60, 160)
        d.ellipse([x - r, y - r, x + r, y + r], fill=(*color, alpha))
    return Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")


def _rounded_rect(draw, box, radius, **kwargs):
    draw.rounded_rectangle(box, radius=radius, **kwargs)


def _draw_asset_glyph(draw: ImageDraw.ImageDraw, asset_class: str, cx: int, cy: int, r: int, color: tuple):
    """Hand-drawn vector glyph per asset class — see module docstring
    for why this isn't an emoji."""
    ac = (asset_class or "").lower()
    if ac == "crypto":
        # Stylized coin: outer ring + inner ring + vertical bar (like a stylized currency mark)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color, width=8)
        draw.ellipse([cx - r + 16, cy - r + 16, cx + r - 16, cy + r - 16], outline=color, width=4)
        draw.line([(cx, cy - r + 30), (cx, cy + r - 30)], fill=color, width=8)
        draw.line([(cx - r * 0.35, cy - r * 0.15), (cx + r * 0.35, cy - r * 0.15)], fill=color, width=6)
        draw.line([(cx - r * 0.35, cy + r * 0.15), (cx + r * 0.35, cy + r * 0.15)], fill=color, width=6)
    elif ac == "forex":
        # Crossed exchange arrows
        draw.line([(cx - r, cy - r * 0.3), (cx + r * 0.5, cy - r * 0.3)], fill=color, width=10)
        draw.polygon([(cx + r * 0.5, cy - r * 0.55), (cx + r * 0.5, cy - r * 0.05), (cx + r, cy - r * 0.3)], fill=color)
        draw.line([(cx + r, cy + r * 0.3), (cx - r * 0.5, cy + r * 0.3)], fill=color, width=10)
        draw.polygon([(cx - r * 0.5, cy + r * 0.05), (cx - r * 0.5, cy + r * 0.55), (cx - r, cy + r * 0.3)], fill=color)
    elif ac == "gold":
        # Bullion bar (trapezoid) with a shine line
        top_w, bot_w, h = r * 1.1, r * 1.5, r * 0.9
        pts = [
            (cx - top_w / 2, cy - h / 2), (cx + top_w / 2, cy - h / 2),
            (cx + bot_w / 2, cy + h / 2), (cx - bot_w / 2, cy + h / 2),
        ]
        draw.polygon(pts, outline=color, width=6)
        draw.line([(cx - top_w / 2 + 14, cy - h / 2 + 14), (cx + top_w / 2 - 14, cy - h / 2 + 14)], fill=color, width=4)
    else:  # stocks
        # Ascending bar chart
        bar_w = r * 0.32
        heights = [r * 0.5, r * 0.9, r * 1.3, r * 1.7]
        base_y = cy + r * 0.85
        for i, h in enumerate(heights):
            x0 = cx - r * 1.05 + i * (bar_w + r * 0.18)
            draw.rectangle([x0, base_y - h, x0 + bar_w, base_y], fill=color)


def render_tp_celebration(trade) -> bytes:
    """
    trade: a Trade ORM object, already closed with a winning outcome
    (symbol, asset_class, direction, entry_price, exit_price,
    pnl_percent, pnl_usd, r_multiple all populated).
    """
    theme = _theme_for(trade.asset_class)
    tier = tier_for_r_multiple(trade.r_multiple)

    # Defensive normalization: apply_trade_close() always populates these
    # numerically, but this function must never crash the whole
    # notification pipeline over one malformed/legacy row — a missing
    # image is recoverable (trade_notify.py falls back to text), a
    # raised exception here is not worth risking.
    r_value = trade.r_multiple if trade.r_multiple is not None else 0.0
    pnl_pct_value = trade.pnl_percent if trade.pnl_percent is not None else 0.0
    pnl_usd_value = trade.pnl_usd if trade.pnl_usd is not None else 0.0
    entry_value = trade.entry_price if trade.entry_price is not None else 0.0
    exit_value = trade.exit_price if trade.exit_price is not None else 0.0

    img = _vertical_gradient(CANVAS, theme["top"], theme["bottom"])
    center = (CANVAS // 2, int(CANVAS * 0.38))
    img = _add_glow(img, center, theme["glow"], tier["glow_layers"])
    if tier["particles"]:
        img = _add_particles(img, theme["accent"])

    img = img.convert("RGB")
    draw = ImageDraw.Draw(img)

    # ── Border ornamentation ──
    margin = 28
    if tier["border"] == "double":
        draw.rounded_rectangle([margin, margin, CANVAS - margin, CANVAS - margin], radius=36, outline=theme["accent"], width=4)
        draw.rounded_rectangle([margin + 14, margin + 14, CANVAS - margin - 14, CANVAS - margin - 14], radius=28, outline=theme["accent"], width=2)
    elif tier["border"] == "single":
        draw.rounded_rectangle([margin, margin, CANVAS - margin, CANVAS - margin], radius=32, outline=theme["accent"], width=3)
    elif tier["border"] == "thin":
        draw.rounded_rectangle([margin, margin, CANVAS - margin, CANVAS - margin], radius=28, outline=theme["accent"], width=1)

    # ── Asset badge (top-left) ──
    f_small = _load_font(28, bold=True)
    _rounded_rect(draw, [56, 56, 56 + 220, 56 + 56], 18, outline=theme["accent"], width=2)
    draw.text((72, 72), theme["label"], font=f_small, fill=theme["accent"])

    # ── Tier badge (top-right) ──
    f_badge = _load_font(26, bold=True)
    badge_w = draw.textlength(tier["badge"], font=f_badge) + 48
    bx0 = CANVAS - 56 - badge_w
    _rounded_rect(draw, [bx0, 56, CANVAS - 56, 56 + 56], 18, outline=theme["accent"], width=2)
    draw.text((bx0 + 24, 72), tier["badge"], font=f_badge, fill=WHITE)

    # ── Glyph ──
    _draw_asset_glyph(draw, trade.asset_class, center[0], center[1] - 40, 90, theme["accent"])

    # ── Symbol + direction ──
    # Dynamic font sizing: a long symbol (e.g. an unusually long ticker)
    # must never overflow the canvas edges — shrink the font until it
    # fits within the available width, with a sane floor.
    symbol_text = str(trade.symbol)
    max_symbol_width = CANVAS - 120  # leave margin on both sides
    symbol_font_size = 64
    f_symbol = _load_font(symbol_font_size, bold=True)
    while draw.textlength(symbol_text, font=f_symbol) > max_symbol_width and symbol_font_size > 28:
        symbol_font_size -= 4
        f_symbol = _load_font(symbol_font_size, bold=True)
    sw = draw.textlength(symbol_text, font=f_symbol)
    draw.text((CANVAS / 2 - sw / 2, center[1] + 90), symbol_text, font=f_symbol, fill=WHITE)

    direction_text = f"{'LONG ▲' if (trade.direction or '').lower() == 'long' else 'SHORT ▼'}"
    f_dir = _load_font(30, bold=True)
    dw = draw.textlength(direction_text, font=f_dir)
    dir_color = theme["accent"]
    draw.text((CANVAS / 2 - dw / 2, center[1] + 170), direction_text, font=f_dir, fill=dir_color)

    # ── Big P&L number (dynamically sized — an extreme leveraged move
    # must never overflow the canvas, same reasoning as the symbol above) ──
    pnl_text = f"+{pnl_pct_value:.2f}%"
    max_pnl_width = CANVAS - 80
    pnl_font_size = 120
    f_huge = _load_font(pnl_font_size, bold=True)
    while draw.textlength(pnl_text, font=f_huge) > max_pnl_width and pnl_font_size > 50:
        pnl_font_size -= 6
        f_huge = _load_font(pnl_font_size, bold=True)
    pw = draw.textlength(pnl_text, font=f_huge)
    draw.text((CANVAS / 2 - pw / 2, center[1] + 230), pnl_text, font=f_huge, fill=(134, 239, 172))

    r_text = f"{r_value:+.2f}R"
    f_r = _load_font(42, bold=True)
    rw = draw.textlength(r_text, font=f_r)
    draw.text((CANVAS / 2 - rw / 2, center[1] + 370), r_text, font=f_r, fill=MUTED)

    # ── Footer stats row ──
    f_stat_label = _load_font(20, bold=False)
    f_stat_val = _load_font(30, bold=True)
    footer_y = CANVAS - 200
    stats = [
        ("ENTRY", f"{entry_value:.5g}"),
        ("EXIT", f"{exit_value:.5g}"),
        ("P&L (USD)", f"{pnl_usd_value:+,.2f}"),
    ]
    col_w = (CANVAS - 2 * margin - 40) / 3
    for i, (label, val) in enumerate(stats):
        cx = margin + 20 + col_w * i + col_w / 2
        lw = draw.textlength(label, font=f_stat_label)
        draw.text((cx - lw / 2, footer_y), label, font=f_stat_label, fill=MUTED)
        vw = draw.textlength(val, font=f_stat_val)
        draw.text((cx - vw / 2, footer_y + 30), val, font=f_stat_val, fill=WHITE)
        if i < 2:
            sep_x = margin + 20 + col_w * (i + 1)
            draw.line([(sep_x, footer_y), (sep_x, footer_y + 60)], fill=(90, 96, 110), width=1)

    # ── Brand watermark ──
    f_brand = _load_font(22, bold=True)
    brand = "SmAttaker"
    bw = draw.textlength(brand, font=f_brand)
    draw.text((CANVAS / 2 - bw / 2, CANVAS - 70), brand, font=f_brand, fill=MUTED)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf.read()
