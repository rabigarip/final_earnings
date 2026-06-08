"""Report styling — colors, spacing, typography, and table helpers for memo/QA .docx."""

from __future__ import annotations
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, Inches
from docx.dml.color import RGBColor

# ─── Colors ─────────────────────────────────────────────────────────────
ACCENT = RGBColor(0x1A, 0x52, 0x76)   # navy
BODY = RGBColor(0x1C, 0x28, 0x33)     # charcoal
GRAY = RGBColor(0x5D, 0x6D, 0x7E)     # muted gray
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
HEADER_FILL = "E8EEF2"                # light gray for header rows

# ─── Spacing ────────────────────────────────────────────────────────────
SPACE_NONE = Pt(0)
SPACE_TINY = Pt(2)
SPACE_SMALL = Pt(4)
SPACE_MED = Pt(6)

# ─── Font sizes ─────────────────────────────────────────────────────────
TITLE_PT = 11
SECTION_PT = 10
BODY_PT = 9
SMALL_PT = 8
SOURCE_PT = 7

# ─── Page margins ───────────────────────────────────────────────────────
MARGIN_IN = 0.6


def apply_section_margins(section) -> None:
    section.top_margin = Inches(MARGIN_IN)
    section.bottom_margin = Inches(MARGIN_IN)
    section.left_margin = Inches(MARGIN_IN)
    section.right_margin = Inches(MARGIN_IN)


# ─── Table helpers ──────────────────────────────────────────────────────

def set_cell_shading(cell, fill_hex: str) -> None:
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:fill"), fill_hex)
    tcPr.append(shd)


def style_table_header_row(row, headers: list[str], run_fn) -> None:
    """Apply shaded header row; run_fn(paragraph, text, bold, size_pt, color)."""
    for i, label in enumerate(headers):
        if i >= len(row.cells):
            break
        cell = row.cells[i]
        cell.text = ""
        set_cell_shading(cell, HEADER_FILL)
        run_fn(cell.paragraphs[0], label, bold=True, size_pt=SOURCE_PT, color=ACCENT)


def set_compact_row_height(row, height_pt: float = 14) -> None:
    try:
        row.height = Pt(height_pt)
    except Exception:
        pass
