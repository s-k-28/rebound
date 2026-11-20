"""The design doc's token table and contrast claims must match the code."""

import re
from pathlib import Path

from rebound import ui

DESIGN = (Path(__file__).resolve().parent.parent / "docs" / "DESIGN.md").read_text()


def luminance(hex_color: str) -> float:
    channels = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_every_token_in_the_design_table_matches_the_code():
    rows = re.findall(r"\| `([A-Z_0-9]+)` \| `(#[0-9A-F]{6})`", DESIGN)
    assert len(rows) >= 14
    for name, value in rows:
        assert getattr(ui, name) == value, name


def test_text_colours_stay_readable_on_every_surface_they_sit_on():
    grounds = [ui.PAPER, ui.SURFACE, ui.SURFACE_2, ui.SELECTED, ui.AMBER_WASH, ui.RED_WASH]
    for text in (ui.INK, ui.INK_2, ui.INK_3, ui.ACCENT, ui.AMBER, ui.RED):
        for ground in grounds:
            assert contrast(text, ground) >= 4.5, (text, ground)


def test_button_and_field_boundaries_meet_their_contrast_floors():
    assert contrast(ui.SURFACE, ui.ACCENT) >= 4.5 and contrast(ui.SURFACE, ui.ACCENT_DEEP) >= 4.5
    assert contrast(ui.FIELD_BORDER, ui.SURFACE) >= 3.0
