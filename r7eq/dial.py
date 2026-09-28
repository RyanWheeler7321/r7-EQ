from __future__ import annotations

from math import cos, radians, sin

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QAbstractSlider, QWidget

ACCENT = QColor("#E3008C")
TRACK = QColor("#303034")
MUTED = QColor("#6E6E76")
START, SWEEP = 225.0, 270.0   # From 7:30 round to 4:30, clockwise.
DRAG_PIXELS = 200              # Mouse travel for the whole range; Shift is ten times finer.


class Dial(QAbstractSlider):
    """Drag up or right, scroll, or use the arrow keys. Double-click asks for a reset."""

    resetRequested = Signal()

    def __init__(self, size: int = 38, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.SizeVerCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.origin: int | None = None   # Where the lit arc starts; None = the minimum.
        self.muted = False                # Drawn grey while what it controls is switched off.
        self._last: QPointF | None = None
        self._position = 0.0

    def setMuted(self, muted: bool) -> None:
        if muted != self.muted:
            self.muted = muted
            self.update()

    def _angle(self, value: float) -> float:
        span = self.maximum() - self.minimum()
        fraction = (value - self.minimum()) / span if span else 0.0
        return START - SWEEP * min(1.0, max(0.0, fraction))

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self._last = event.position()
        self._position = float(self.value())
        self.setSliderDown(True)
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._last is None:
            return
        moved = (event.position().x() - self._last.x()) - (event.position().y() - self._last.y())
        self._last = event.position()
        fine = 0.1 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1.0
        self._position += moved * fine * (self.maximum() - self.minimum()) / DRAG_PIXELS
        self._position = min(float(self.maximum()), max(float(self.minimum()), self._position))
        self.setSliderPosition(round(self._position))
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if self._last is not None and event.button() == Qt.MouseButton.LeftButton:
            self._last = None
            self.setSliderDown(False)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.resetRequested.emit()
            event.accept()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        side = min(self.width(), self.height())
        line = max(3.0, side / 16)   # Ring and knob grow with the dial.
        ring = QRectF(line, line, side - 2 * line, side - 2 * line)
        painter.setPen(QPen(TRACK, line, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawArc(ring, round(START * 16), round(-SWEEP * 16))
        lit = ACCENT if self.isEnabled() and not self.muted else MUTED
        origin = self.minimum() if self.origin is None else self.origin
        start, end = self._angle(origin), self._angle(self.value())
        if abs(end - start) > 0.5:
            painter.setPen(QPen(lit, line, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawArc(ring, round(start * 16), round((end - start) * 16))

        inset = max(6.0, side * 0.14)
        knob = ring.adjusted(inset, inset, -inset, -inset)
        active = self.isEnabled() and (self.hasFocus() or self.underMouse() or self.isSliderDown())
        painter.setPen(QPen(ACCENT if active else QColor("#3A3A40"), 1))
        painter.setBrush(QColor("#1C1C20"))
        painter.drawEllipse(knob)
        # The pointer, from near the centre to the knob's edge.
        angle = radians(end)
        centre, radius = knob.center(), knob.width() / 2
        direction = QPointF(cos(angle), -sin(angle))
        painter.setPen(QPen(QColor("#F1F0F0") if self.isEnabled() else MUTED, max(2.0, side / 24),
                            Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(centre + direction * radius * 0.25, centre + direction * (radius - 2.5))
        painter.end()
