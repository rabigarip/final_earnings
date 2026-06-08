"""
Jabal Asset Management deck — design tokens.

Single source of truth for the palette, fonts, and size scale used
across the new renderer. Imported by every render_jabal_*.py module.
"""

from __future__ import annotations

from pptx.dml.color import RGBColor
from pptx.util import Inches, Pt


# ── Page geometry ─────────────────────────────────────────────
PAGE_W_IN = 7.50
PAGE_H_IN = 13.32
MARGIN_L = 0.45
MARGIN_R = 0.45
CONTENT_W = PAGE_W_IN - MARGIN_L - MARGIN_R  # 6.60"


# ── Colors (RGBColor instances) ───────────────────────────────
BLACK     = RGBColor(0x1A, 0x1A, 0x1A)
GRAY      = RGBColor(0x5C, 0x5C, 0x5C)
MUTED     = RGBColor(0x9A, 0x9A, 0x9A)
GOLD      = RGBColor(0xA2, 0x88, 0x60)
GOLD_DK   = RGBColor(0x7E, 0x68, 0x49)
POS       = RGBColor(0x2F, 0x7D, 0x4F)
NEG       = RGBColor(0xB8, 0x32, 0x27)
PAGE_BG   = RGBColor(0xFA, 0xF8, 0xF4)
CARD      = RGBColor(0xEF, 0xE8, 0xDC)
WHITE     = RGBColor(0xFF, 0xFF, 0xFF)


# ── Fonts ─────────────────────────────────────────────────────
FONT_DISPLAY = "Georgia"
FONT_UI      = "Calibri"


# ── Size scale (Pt) ───────────────────────────────────────────
SZ_HERO     = Pt(26)     # Slide 1 company name
SZ_SECTION  = Pt(17)     # Slide 2/3 section hero ("Executive Summary")
SZ_KICKER   = Pt(10.5)   # "EARNINGS PREVIEW NOTE" / section labels
SZ_VALUE_LG = Pt(15)     # Big metric values
SZ_VALUE    = Pt(14)     # Card-level metric values
SZ_LABEL    = Pt(8.5)    # Metric labels ("LAST CLOSE")
SZ_BODY     = Pt(10)
SZ_META     = Pt(10)     # ticker · sector · industry meta line
SZ_HEADER   = Pt(10)     # top header line
SZ_FOOTER   = Pt(8)
SZ_TINY     = Pt(7)
SZ_BULLET_PILL = Pt(8)
SZ_TAB_NUM  = Pt(8.5)    # "1 / 3"


# ── Visual primitives ─────────────────────────────────────────
RULE_THICK_PT  = 0.5
BORDER_THICK_PT = 0.5
LEFT_ACCENT_W_IN = 0.05
CARD_PAD_IN = 0.18


# ── Helpers ───────────────────────────────────────────────────
def in_(x: float) -> "Inches":
    """Shorter alias so call-sites stay tidy."""
    return Inches(x)


def signed_color(value: float) -> RGBColor:
    """Green for positive, red for negative, black for zero."""
    if value is None:
        return GRAY
    return POS if value > 0 else (NEG if value < 0 else BLACK)
