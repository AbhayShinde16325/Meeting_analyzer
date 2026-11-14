"""Light-theme consistency tests.

The application intentionally has no dark mode: every window must render a
white/off-white background with dark text, independent of the Windows theme.
These tests guard the invariants that failed before (CSS variables and media
queries that Qt stylesheets cannot parse, unstyled QTextBrowser panes
inheriting a dark palette, and transcript HTML without an explicit
background).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QColor, QPalette

import main
from main import _apply_light_theme

STYLESHEET_PATH = (
    Path(main.__file__).resolve().parent / "styles" / "app_stylesheet.qss"
)


def _stylesheet_text() -> str:
    return STYLESHEET_PATH.read_text(encoding="utf-8")


def test_stylesheet_is_static_light_only():
    text = _stylesheet_text()
    # Qt stylesheets support neither CSS variables nor media queries; using
    # them silently breaks every colour the rule declares.
    assert "var(" not in text, "stylesheet must use literal colours only"
    assert "@media" not in text, "stylesheet must not switch on system theme"
    assert ":root" not in text


def test_stylesheet_does_not_use_qt_unsupported_properties():
    """Properties that generate 'Could not parse stylesheet' warnings."""

    text = _stylesheet_text()
    for unsupported in ("-qt-text-color", "text-color:", "outline-radius",
                        "transition:", "flip:", "line-height:", "word-wrap:"):
        assert unsupported not in text, f"invalid QSS property: {unsupported}"


def test_stylesheet_covers_text_browsers_with_light_colors():
    text = _stylesheet_text()
    browsers_rule = text[text.index("QTextBrowser,") : text.index("QTextBrowser,") + 400]
    assert "background-color: #ffffff" in browsers_rule
    assert "color: #20242a" in browsers_rule


def test_stylesheet_has_no_dark_backgrounds():
    """Every background in the sheet is white/off-white (or dark only for
    tooltips and primary/record buttons, which keep light label text)."""

    text = _stylesheet_text()
    forbidden_dark = ("#0f172a", "#1e293b;\n    color: #f1f5f9", "#111827")
    for snippet in forbidden_dark:
        assert snippet not in text


def test_apply_light_theme_installs_light_palette(qapp):
    _apply_light_theme(qapp)
    palette = qapp.palette()

    light = QColor(palette.color(QPalette.ColorRole.Window))
    base = QColor(palette.color(QPalette.ColorRole.Base))
    text = QColor(palette.color(QPalette.ColorRole.Text))
    window_text = QColor(palette.color(QPalette.ColorRole.WindowText))

    assert light.lightness() > 200, "window background must be light"
    assert base.lightness() > 200, "text background must be white"
    assert text.lightness() < 100, "text colour must be dark"
    assert window_text.lightness() < 100, "window text must be dark"


def test_wrap_html_uses_explicit_light_background(qapp):
    from ui.meeting_details import MeetingDetailsScreen

    wrapped = MeetingDetailsScreen._wrap_html("<p>transcript body</p>")
    assert "background-color: #ffffff" in wrapped
    assert "color: #20242a" in wrapped
