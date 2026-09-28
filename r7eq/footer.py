"""The settings under the graph and the status line."""
from __future__ import annotations

import math
import re
from typing import Callable

import numpy as np
from PySide6.QtCore import QPointF, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSizePolicy,
                               QVBoxLayout, QWidget)

from . import model
from .dial import Dial
from .spectrum import FREQUENCIES

TONE_CONTROLS = (
    ("tilt_db", "Tilt", "Darker ↔ brighter, pivoting at 1 kHz. The value is the gain reached by "
                        "20 kHz; the opposite is applied by 50 Hz."),
    ("warmth_db", "Warmth", "Bass shelf below ~150 Hz. It stops boosting under ~35 Hz, "
                            "where small speakers have little headroom."),
    ("presence_db", "Presence", "Broad band around 2.5 kHz, where speech sounds most "
                                "\"radio-like\". Negative softens it."),
    ("air_db", "Air", "Treble shelf above ~8 kHz."),
)
COMPRESSOR_CONTROLS = (
    ("comp_threshold_db", "Threshold", "Level where compression starts. Lower = more of the audio "
                                       "gets evened out. Twitch uses −50 dB, which levels nearly everything."),
    ("comp_knee_db", "Knee", "How gradually compression starts around the threshold. 0 = a hard corner; "
                             "Twitch's 40 dB eases in over a wide range, so even heavy settings sound natural."),
    ("comp_ratio", "Ratio", "How hard it holds levels above the threshold. 2:1 is gentle, "
                            "4:1 firm, 12:1 (Twitch) and up flattens."),
)
TIMING_CONTROLS = (
    ("comp_attack_ms", "Attack", "How quickly it clamps down. It hears 6 ms ahead, so 0 ms (Twitch) "
                                 "catches hits cleanly; 10–30 ms lets drum hits and consonants through."),
    ("comp_release_ms", "Release", "How quickly quiet audio comes back after something loud. It recovers "
                                   "faster the harder it was pushed. Twitch uses 250 ms; under ~50 ms is "
                                   "fast and even but can pump or rough up bass."),
    ("comp_bass_hz", "Ignore bass", "The compressor stops listening below this frequency, so kicks "
                                    "and bass stop ducking voices. The bass itself is not filtered."),
    ("comp_dry_pct", "Dry", "Blends the uncompressed audio back in (parallel compression): "
                            "keeps the lift on quiet parts while hits keep their punch."),
)
# Slider position <-> value on a log scale, so short times get as much travel as long ones.
LOG_SLIDERS = {"comp_attack_ms", "comp_release_ms"}
LOG_STEPS = 1000
SPACE_CONTROLS = (
    ("width_pct", "Width", "Stereo width. 100% = unchanged, 0% = mono. Widening only affects sound "
                           "above ~150 Hz; bass and centred voices stay put."),
    ("depth_3d", "3D", "Speaker crosstalk cancellation: stereo sounds can spread past the speakers. "
                       "Needs stereo content; mono voices are left centred."),
    ("room_pct", "Room", "A small room around everything, including mono voices."),
    ("room_size", "Size", "Room size: later reflections and a longer tail (0.2–1.0 s)."),
)
COMPRESSION_TOOLTIP = (
    "Boost after compression: up to 36 dB at 100% (Twitch adds 12.4 dB, about 35%). "
    "Audio below the threshold gets the full boost; louder audio gets less, set by Ratio. "
    "The clip guard catches peaks. Independent of the EQ toggle; continues while this window is hidden.")
TWITCH_TOOLTIP = ("T: sets the compressor to exactly what Twitch's player compressor uses "
                  "(−50 dB, 40 dB knee, 12:1, 0 ms attack, 250 ms release, +12.4 dB boost) and "
                  "switches it on. Dry and Ignore bass go off. Ctrl+Z undoes it.")
STATIC_TOOLTIP = "The fixed effect of this setting (it does not depend on what is playing)."
LIVE_TOOLTIP = ("Live: how much louder (+) or quieter (−) this slider makes the audio playing now, "
                "weighted like a loudness meter. Max: the largest gain it applies at any frequency "
                "(shown when nothing is playing or the window was hidden).")
LARGE_DIAL, SMALL_DIAL = 80, 42   # Character and Space get one row of big dials; the compressor two rows.
BASE_WIDTH, MAX_SCALE = 1500, 1.5  # The footer grows with the window past this width, up to 1.5 times.
TONE_TEXTS = ("−4.0 dB", "max +5.4", "now −4.0", "EQ off")
SPACE_TEXTS = ("side +6.0", "side ≤+5.1", "wet −6 dB", "tail 1.0 s", "room off", "200%")
COMPRESSOR_TEXTS = ("300 Hz", "now +36.0", "max +36.0", "1000 ms", "20.0:1", "−100 dB", "not running",
                    "not playing")
STATUS_COLORS = {"error": "#F19A9A", "warning": "#E8BD80", "success": "#A6D2AD"}


def value_text(key: str, value: float) -> str:
    if key in model.TONE_RANGES:
        return f"{value:+.1f} dB" if value else "0.0 dB"
    if key in ("comp_attack_ms", "comp_release_ms"):
        return f"{value:.1f} ms" if value < 10 and value != round(value) else f"{value:.0f} ms"
    if key in ("comp_threshold_db", "comp_knee_db"):
        return f"{value:.0f} dB"
    if key == "comp_ratio":
        return f"{value:.1f}:1"
    if key == "comp_dry_pct":
        return f"{value:.0f}%" if value else "off"
    if key == "comp_bass_hz":
        return f"{value:.0f} Hz" if value else "off"
    return f"{value:.0f}%"


def static_detail(key: str, state: dict) -> str:
    """Second readout line, for settings whose effect doesn't depend on what's playing."""
    if key in model.COMPRESSOR_RANGES:
        return ""
    if not state["enabled"]:
        return "EQ off"
    if key == "width_pct":
        width = state["width_pct"]
        return "mono" if not width else f"side {20 * math.log10(width / 100):+.1f}"
    if key == "depth_3d":
        depth = state["depth_3d"]
        return f"side ≤{20 * math.log10(1 + 0.8 * depth / 100):+.1f}" if depth else "off"
    if key == "room_pct":
        room = state["room_pct"]
        return f"wet {20 * math.log10(0.5 * room / 100):.0f} dB" if room else "off"
    if key == "room_size":
        return f"tail {0.2 + 0.8 * state['room_size'] / 100:.1f} s" if state["room_pct"] else "room off"
    return ""


def slider_value(key: str, position: int) -> float:
    low, high = model.SLIDER_RANGES[key]
    if key not in LOG_SLIDERS:
        return position
    # low at 0, high at the end; +1 keeps an attack of 0 reachable.
    value = low + (high - low + 1) ** (position / LOG_STEPS) - 1
    return round(value, 1) if value < 10 else round(value)


def slider_position(key: str, value: float) -> int:
    low, high = model.SLIDER_RANGES[key]
    return round(LOG_STEPS * math.log(value - low + 1) / math.log(high - low + 1))


def divider(parent: QWidget, height: int = 18) -> QFrame:
    line = QFrame(parent)
    line.setObjectName("footerDivider")
    line.setFixedSize(1, height)
    return line


NUMBER = re.compile(r"[-+]?(\d+\.?\d*|\.\d+)")


class ValueField(QLineEdit):
    """Click to type a value. Enter or clicking away applies, Esc cancels, Up/Down nudge it."""

    valueEntered = Signal(float)

    def __init__(self, low: float, high: float, decimals: int, step: float,
                 show: Callable[[float], str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("valueField")
        self.low, self.high, self.decimals, self.step, self.show = low, high, decimals, step, show
        self.value = low
        self._applying = False
        self.setText(show(low))
        self.setTextMargins(0, 0, 0, 0)
        self.setFixedHeight(self.fontMetrics().height() + 4)
        self.editingFinished.connect(self._apply)

    def setValue(self, value: float) -> None:
        self.value = value
        if not self.hasFocus():
            self.setText(self.show(value))

    def _number_text(self) -> str:
        text = f"{self.value:.{self.decimals}f}"
        return text.rstrip("0").rstrip(".") if "." in text else text

    def _apply(self) -> None:
        if self._applying:
            return
        match = NUMBER.search(self.text().replace("−", "-").replace(",", "."))
        if match is None and self.text().strip().lower() in ("off", "peak", "mono"):
            typed = 0.0
        elif match is None:
            typed = self.value
        else:
            typed = float(match.group())
        typed = round(min(self.high, max(self.low, typed)), self.decimals)
        self._applying = True
        if typed != round(self.value, self.decimals):
            self.valueEntered.emit(typed)
        self.clearFocus()
        self._applying = False
        self.setText(self.show(self.value))

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        self.setText(self._number_text())
        # Qt places the cursor after a focusing click; select once it has.
        QTimer.singleShot(0, self.selectAll)

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self.setText(self.show(self.value))

    def mousePressEvent(self, event) -> None:
        if not self.hasFocus():
            # Take focus without placing the cursor, so the whole number stays selected.
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self._focus_click = True
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if getattr(self, "_focus_click", False):
            self._focus_click = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.setText(self._number_text())
            self.clearFocus()
            return
        if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            direction = 1 if event.key() == Qt.Key.Key_Up else -1
            nudged = round(min(self.high, max(self.low, self.value + direction * self.step)), self.decimals)
            if nudged != self.value:
                self.valueEntered.emit(nudged)
            self.setText(self._number_text())
            self.selectAll()
            return
        super().keyPressEvent(event)


class SectionBand(QWidget):
    """A section's title band, grey while that section is switched off."""

    def __init__(self, text: str, parent: QWidget) -> None:
        super().__init__(parent)
        self.active = True
        self.title = QLabel(text, self)
        self.title.setStyleSheet("color: #F4F5F5;")
        row = QHBoxLayout(self)
        row.setSpacing(10)
        row.addWidget(self.title)
        row.addStretch(1)

    def set_active(self, active: bool) -> None:
        if active != self.active:
            self.active = active
            self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width, height = self.width(), self.height()
        tip = height * 0.32
        shape = QPolygonF([QPointF(0, 0), QPointF(width - tip, 0), QPointF(width, height / 2),
                           QPointF(width - tip, height), QPointF(0, height)])
        tint = QColor("#E3008C") if self.active else QColor("#6E6E76")
        fill = QLinearGradient(0, 0, width, 0)
        tint.setAlpha(85)
        fill.setColorAt(0, tint)
        tint.setAlpha(30)
        fill.setColorAt(1, tint)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawPolygon(shape)
        edge = QColor("#E3008C") if self.active else QColor("#6E6E76")
        edge.setAlpha(150)
        painter.setPen(QPen(edge, 1))
        painter.drawLine(QPointF(0.5, height - 0.5), QPointF(width - 0.5, height - 0.5))
        painter.end()


class Footer(QWidget):
    """Everything grows with the window's width, so the proportions never change."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("footer")
        self.setMouseTracking(True)
        self.sliders: dict[str, Dial] = {}
        self.slider_values: dict[str, ValueField] = {}
        self.slider_details: dict[str, QLabel] = {}
        self._tone_gains: dict[str, np.ndarray] = {}
        self._tone_amounts: dict[str, float] = {}
        self._tone_enabled = True
        self._live_power: np.ndarray | None = None
        self._cells: list[tuple] = []      # (name, dial, value, detail, readout texts, dial size)
        self._bands: list[SectionBand] = []
        self._bodies: list[QVBoxLayout] = []
        self._grids: list[QGridLayout] = []
        self.scale = 0.0
        self.bottom = QVBoxLayout(self)
        groups = QHBoxLayout()
        groups.setContentsMargins(0, 0, 0, 0)
        self.groups = groups
        self.character_box = self._group("CHARACTER", TONE_CONTROLS, TONE_TEXTS)
        self.space_box = self._group("SPACE", SPACE_CONTROLS, SPACE_TEXTS)
        groups.addWidget(self.character_box, 1)
        groups.addWidget(self.space_box, 1)

        self.compressor_box, body = self._box("COMPRESSOR")
        self.twitch_button = QPushButton("T", self.compressor_box)
        self.twitch_button.setObjectName("chipButton")
        self.twitch_button.setToolTip(TWITCH_TOOLTIP)
        self.twitch_button.setAccessibleName("Use Twitch's compressor settings")
        self.twitch_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._bands[-1].layout().insertWidget(1, self.twitch_button)
        grid = self._grid()
        # Amount first: how much it lifts; then how it compresses, then its timing and listening.
        self.compression_slider = Dial(SMALL_DIAL, self.compressor_box)
        self.compression_slider.setRange(0, 100)
        self.compression_slider.setPageStep(10)
        self.compression_slider.setAccessibleName("Compression amount")
        self.compression_value = ValueField(0, 100, 0, 1, lambda value: f"{value:.0f}%", self.compressor_box)
        self.meter_value = self._cell(grid, 0, 0, "Amount", COMPRESSION_TOOLTIP, self.compression_slider,
                                         self.compression_value, COMPRESSOR_TEXTS, SMALL_DIAL)
        for index, (key, name, tooltip) in enumerate(COMPRESSOR_CONTROLS + TIMING_CONTROLS, start=1):
            self._place_dial(grid, index // 4, index % 4, key, name, tooltip, self.compressor_box,
                             SMALL_DIAL, COMPRESSOR_TEXTS)
        # Only Amount has a second line here; the rest stay packed without one.
        for key in model.COMPRESSOR_RANGES:
            self.slider_details[key].hide()
        body.addLayout(grid)
        groups.addWidget(self.compressor_box, 1)
        self.bottom.addLayout(groups)

        self.status_label = QLabel("", self)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.status_label.setAccessibleName("EQ status")
        self.bottom.addWidget(self.status_label)
        self.set_scale(1.0)

    def set_scale(self, scale: float) -> None:
        self.scale = scale

        def px(value: float) -> int:
            return round(value * scale)

        def font(points: float, weight=QFont.Weight.Normal) -> QFont:
            return QFont("Segoe UI", points * scale, weight)

        self.bottom.setContentsMargins(px(24), px(12), px(24), px(4))
        self.bottom.setSpacing(px(4))
        self.groups.setSpacing(px(12))
        for band in self._bands:
            band.setFixedHeight(px(32))
            band.layout().setContentsMargins(px(14), 0, px(14), 0)
            band.title.setFont(font(10.5, QFont.Weight.DemiBold))
        for body in self._bodies:
            body.setContentsMargins(px(12), px(12), px(12), px(12))
        for grid in self._grids:
            grid.setHorizontalSpacing(px(6))
            grid.setVerticalSpacing(px(4))
        self.twitch_button.setFont(font(8, QFont.Weight.DemiBold))
        self.twitch_button.setFixedSize(px(22), px(18))
        for name, dial, value, detail, texts, size in self._cells:
            name.setFont(font(10))
            name.setFixedHeight(QFontMetrics(name.font()).height())
            dial.setFixedSize(px(size), px(size))
            value.setFont(font(10))
            detail.setFont(font(8.5))
            width = max(QFontMetrics(value.font()).horizontalAdvance(text) for text in texts) + px(8)
            value.setFixedSize(width, QFontMetrics(value.font()).height() + px(4))
            detail.setFixedSize(width, QFontMetrics(detail.font()).height())
        self.status_label.setFont(font(9))
        self.status_label.setFixedHeight(QFontMetrics(self.status_label.font()).height())
        self._fit_height()
        # The boxes only report their new sizes after Qt's next layout pass; measure again then.
        QTimer.singleShot(0, self._fit_height)

    def _fit_height(self) -> None:
        self.setFixedHeight(self.bottom.sizeHint().height())

    def _box(self, heading: str) -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame(self)
        frame.setObjectName("sectionBox")
        column = QVBoxLayout(frame)
        column.setContentsMargins(1, 1, 1, 1)
        column.setSpacing(0)
        band = SectionBand(heading, frame)
        column.addWidget(band)
        body = QVBoxLayout()
        column.addLayout(body)
        self._bands.append(band)
        self._bodies.append(body)
        return frame, body

    def _grid(self) -> QGridLayout:
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        self._grids.append(grid)
        return grid

    def _group(self, heading: str, controls, texts) -> QFrame:
        frame, body = self._box(heading)
        grid = self._grid()
        for index, (key, name, tooltip) in enumerate(controls):
            self._place_dial(grid, 0, index, key, name, tooltip, frame, LARGE_DIAL, texts)
        body.addStretch(1)
        body.addLayout(grid)
        body.addStretch(1)
        return frame

    def _cell(self, grid: QGridLayout, row: int, column: int, name: str, tooltip: str, dial: Dial,
              value: ValueField, texts, size: int) -> QLabel:
        """Name over the dial, then a typable value and its effect underneath."""
        cell = QVBoxLayout()
        cell.setContentsMargins(0, 0, 0, 0)
        cell.setSpacing(2)
        label = QLabel(name, dial.parentWidget())
        label.setStyleSheet("color: #C9CECC;")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setToolTip(tooltip)
        dial.setToolTip(tooltip)
        value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        value.setAccessibleName(f"Exact {name.lower()}")
        value.setToolTip(tooltip)
        detail = QLabel(dial.parentWidget())
        detail.setStyleSheet("color: #9FA8A5;")
        detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cell.addWidget(label)
        cell.addWidget(dial, 0, Qt.AlignmentFlag.AlignHCenter)
        cell.addWidget(value, 0, Qt.AlignmentFlag.AlignHCenter)
        cell.addWidget(detail, 0, Qt.AlignmentFlag.AlignHCenter)
        grid.addLayout(cell, row, column, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        grid.setColumnStretch(column, 1)
        self._cells.append((label, dial, value, detail, texts, size))
        return detail

    def _place_dial(self, grid: QGridLayout, row: int, column: int, key: str, name: str, tooltip: str,
                    parent: QWidget, size: int, texts) -> None:
        low, high = model.SLIDER_RANGES[key]
        # Tone in 0.1 dB, ratio in 0.1 steps, the rest in whole units; attack and release on a log scale.
        steps = 10 if key in model.TONE_RANGES or key == "comp_ratio" else 1
        dial = Dial(size, parent)
        if key in LOG_SLIDERS:
            dial.setRange(0, LOG_STEPS)
            dial.setSingleStep(10)
            dial.setPageStep(50)
        else:
            dial.setRange(round(low * steps), round(high * steps))
            dial.setSingleStep(1)
            dial.setPageStep(5 if steps == 10 else 10)
        # The lit arc grows from "no change": 0 dB for +/- ranges, 100% for width.
        if low < 0 < high:
            dial.origin = 0
        elif key == "width_pct":
            dial.origin = 100
        dial.setProperty("steps", steps)
        dial.setAccessibleName(name)
        decimals = 1 if steps == 10 or key in LOG_SLIDERS else 0
        value = ValueField(low, high, decimals, 1 / steps, lambda amount, key=key: value_text(key, amount), parent)
        detail = self._cell(grid, row, column, name, tooltip, dial, value, texts, size)
        detail.setToolTip(LIVE_TOOLTIP if key in model.TONE_RANGES else STATIC_TOOLTIP)
        self.sliders[key] = dial
        self.slider_values[key] = value
        self.slider_details[key] = detail

    def show_state(self, state: dict) -> None:
        with QSignalBlocker(self.compression_slider):
            self.compression_slider.setValue(round(state["compression"]))
        self.compression_value.setValue(state["compression"])
        # Switched off, the compressor's dials stay editable but go grey.
        self.compression_slider.setMuted(not state["compressor"])
        self._bands[0].set_active(state["enabled"])
        self._bands[1].set_active(state["enabled"])
        self._bands[2].set_active(state["compressor"])
        for key in model.COMPRESSOR_RANGES:
            self.sliders[key].setMuted(not state["compressor"])
        self._tone_enabled = state["enabled"]
        self._tone_amounts = {key: state[key] for key in model.TONE_RANGES}
        for key, slider in self.sliders.items():
            with QSignalBlocker(slider):
                slider.setValue(slider_position(key, state[key]) if key in LOG_SLIDERS
                                else round(state[key] * slider.property("steps")))
            self.slider_values[key].setValue(state[key])
            if key in model.TONE_RANGES:
                self._tone_gains[key] = state[key] * model.tone_shape(key, FREQUENCIES)
            else:
                self.slider_details[key].setText(static_detail(key, state))
        self._show_tone_effect()

    def set_live_power(self, power: np.ndarray | None) -> None:
        """Loudness-weighted band powers of what's playing, or None in silence."""
        self._live_power = power
        self._show_tone_effect()

    def _show_tone_effect(self) -> None:
        # The loopback analyzer hears the processed output, so each slider's share is the
        # loudness difference between the output and the output with that slider removed.
        power = self._live_power
        for key, gains in self._tone_gains.items():
            label = self.slider_details[key]
            if not self._tone_enabled:
                label.setText("EQ off")
            elif power is None:
                amount = self._tone_amounts[key]
                label.setText(f"max {model.tone_peak(key, amount):+.1f}" if amount else "max 0.0")
            else:
                without = float(np.sum(power * 10 ** (-gains / 10)))
                delta = 10 * np.log10(float(np.sum(power)) / without) if without > 0 else 0.0
                label.setText(f"now {delta:+.1f}" if abs(delta) >= 0.05 else "now 0.0")

    def set_meter_text(self, text: str, tooltip: str, color: str = "#9FA8A5") -> None:
        self.meter_value.setText(text)
        self.meter_value.setStyleSheet(f"color: {color};")
        self.meter_value.setToolTip(tooltip)

    def show_status(self, severity: str, message: str) -> None:
        self.status_label.setStyleSheet(f"color: {STATUS_COLORS.get(severity, '#9BA6A2')};")
        self.status_label.setText(message)
        self.status_label.setToolTip(message)
