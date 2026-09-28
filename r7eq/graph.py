"""The EQ curve editor, with the speaker's spectrum drawn behind it."""
from __future__ import annotations

import math

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QApplication,
    QDoubleSpinBox,
    QFormLayout,
    QMenu,
    QVBoxLayout,
    QWidget,
)

from . import model


_PINK = QColor("#E3008C")
_BG = QColor("#0D0D0F")
_PLOT = QColor("#111113")
_TEXT = QColor("#D1D6D3")
_MUTED = QColor("#A0A8A5")
_SPECTRUM = QColor(244, 66, 66, 200)
_LOG_SPAN = math.log(model.MAX_HZ / model.MIN_HZ)
_FREQUENCY_LABELS = (
    (20, "20 Hz"), (50, "50"), (100, "100"), (200, "200"),
    (500, "500"), (1000, "1k"), (2000, "2k"), (5000, "5k"),
    (10000, "10k"), (20000, "20k"),
)


class GraphWidget(QWidget):
    """Every move redraws right away; the controller decides when to write."""

    stateEdited = Signal(dict)
    editStarted = Signal()
    editFinished = Signal()
    selectionChanged = Signal(object)  # point id, or None
    statusRequested = Signal(str, str)  # severity, actionable text

    def __init__(self, state: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = model.normalize_state(state)
        self.scale = 1.0   # Text and margins grow with the window; set by the window.
        self._selected_id: str | None = None
        self._dragging_id: str | None = None
        self._drag_start_state: dict | None = None
        self._drag_press = QPointF()
        self._drag_origin = QPointF()
        self._drag_moved = False
        self._reclicked = False
        # The re-click menu waits out a double-click, which opens the exact editor instead.
        self._menu_timer = QTimer(self)
        self._menu_timer.setSingleShot(True)
        self._menu_timer.timeout.connect(self._open_pending_menu)
        self._pending_menu = None
        self._drag_last_sent: dict | None = None
        self._wheel_remainder = 0
        self._grid: QPixmap | None = None
        self._grid_dpr = 0.0
        self._curve = QPainterPath()
        self._fill = QPainterPath()
        self._total = QPainterPath()  # Curve plus Character sliders; empty when they are all zero.
        self._spectrum_values = None
        self._spectrum_path = QPainterPath()
        self._spectrum_fill = QPainterPath()
        self._edit_dialog: QDialog | None = None
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Equalizer frequency response")
        self._rebuild_curve()

    @property
    def selected_point_id(self) -> str | None:
        return self._selected_id

    def graph_rect(self) -> QRectF:
        # Left room for the dB labels; the right edge lines up with the boxes below.
        k = self.scale
        return QRectF(62.0 * k, 34.0 * k, max(1.0, self.width() - 86.0 * k),
                      max(1.0, self.height() - 62.0 * k))

    def frequency_to_x(self, frequency: float) -> float:
        rect = self.graph_rect()
        hz = max(model.MIN_HZ, min(model.MAX_HZ, frequency))
        return rect.left() + math.log(hz / model.MIN_HZ) / _LOG_SPAN * rect.width()

    def x_to_frequency(self, x: float) -> float:
        rect = self.graph_rect()
        fraction = max(0.0, min(1.0, (x - rect.left()) / rect.width()))
        return model.MIN_HZ * math.exp(fraction * _LOG_SPAN)

    def gain_to_y(self, gain: float) -> float:
        rect = self.graph_rect()
        return rect.top() + (model.MAX_DB - gain) / (model.MAX_DB - model.MIN_DB) * rect.height()

    def y_to_gain(self, y: float) -> float:
        rect = self.graph_rect()
        fraction = max(0.0, min(1.0, (y - rect.top()) / rect.height()))
        return model.MAX_DB - fraction * (model.MAX_DB - model.MIN_DB)

    def point_position(self, point_id: str) -> QPointF | None:
        for point in self._state["points"]:
            if point["id"] == point_id:
                return QPointF(self.frequency_to_x(point["frequency"]),
                               self.gain_to_y(point["gain"]))
        return None

    def point_at(self, position: QPointF, radius: float = 12.0) -> str | None:
        rect = self.graph_rect()
        x_scale = rect.width() / _LOG_SPAN
        y_scale = rect.height() / (model.MAX_DB - model.MIN_DB)
        closest, distance_sq = None, radius * radius
        for point in self._state["points"]:
            dx = position.x() - rect.left() \
                - math.log(point["frequency"] / model.MIN_HZ) * x_scale
            dy = position.y() - rect.top() \
                - (model.MAX_DB - point["gain"]) * y_scale
            candidate = dx * dx + dy * dy
            if candidate <= distance_sq:
                closest, distance_sq = point["id"], candidate
        return closest

    def select_point(self, point_id: str | None) -> None:
        if point_id is not None and self.point_position(point_id) is None:
            return
        if point_id == self._selected_id:
            return
        self._selected_id = point_id
        self._wheel_remainder = 0
        self.selectionChanged.emit(point_id)
        self.update()

    def set_state(self, state: dict) -> None:
        """From the controller; doesn't emit stateEdited back."""
        incoming = model.normalize_state(state)
        if incoming == self._state:
            return
        was_dragging = self._dragging_id is not None
        self._dragging_id = None
        self._drag_start_state = None
        self._drag_last_sent = None
        self._state = incoming
        if was_dragging:
            self.editFinished.emit()
        if self._selected_id is not None and self.point_position(self._selected_id) is None:
            self.select_point(None)
        self._rebuild_curve()
        self.update()

    def _rebuild_curve(self) -> None:
        samples = model.sample_curve(self._state, count=512)
        rect = self.graph_rect()
        x_scale = rect.width() / _LOG_SPAN
        y_scale = rect.height() / (model.MAX_DB - model.MIN_DB)
        curve = QPainterPath()
        for index, (frequency, gain) in enumerate(samples):
            x = rect.left() + math.log(frequency / model.MIN_HZ) * x_scale
            y = rect.top() + (model.MAX_DB - gain) * y_scale
            if index == 0:
                curve.moveTo(x, y)
            else:
                curve.lineTo(x, y)
        self._curve = curve
        fill = QPainterPath(curve)
        baseline = rect.top() + model.MAX_DB * y_scale
        fill.lineTo(curve.currentPosition().x(), baseline)
        fill.lineTo(rect.left(), baseline)
        fill.closeSubpath()
        self._fill = fill
        self._total = QPainterPath()
        if model.has_tone(self._state):
            for index, (frequency, gain) in enumerate(model.response_samples(self._state, count=512)):
                x = rect.left() + math.log(frequency / model.MIN_HZ) * x_scale
                y = rect.top() + (model.MAX_DB - gain) * y_scale
                if index == 0:
                    self._total.moveTo(x, y)
                else:
                    self._total.lineTo(x, y)

    def set_spectrum(self, frequencies, levels_db) -> None:
        self._spectrum_values = frequencies, levels_db
        self._rebuild_spectrum()
        self.update()

    def clear_spectrum(self) -> None:
        if self._spectrum_values is not None:
            self._spectrum_values = None
            self._spectrum_path = QPainterPath()
            self._spectrum_fill = QPainterPath()
            self.update()

    def _rebuild_spectrum(self) -> None:
        if self._spectrum_values is None:
            return
        rect = self.graph_rect()
        frequencies, levels_db = self._spectrum_values
        path = QPainterPath()
        for index, (frequency, level) in enumerate(zip(frequencies, levels_db)):
            x = rect.left() + math.log(float(frequency) / model.MIN_HZ) / _LOG_SPAN * rect.width()
            y = rect.bottom() - (max(-84.0, min(0.0, float(level))) + 84) / 84 * rect.height() * .70
            if index == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        self._spectrum_path = path
        fill = QPainterPath(path)
        fill.lineTo(path.currentPosition().x(), rect.bottom())
        fill.lineTo(rect.left(), rect.bottom())
        fill.closeSubpath()
        self._spectrum_fill = fill

    def _commit(self, changed: dict) -> bool:
        if changed == self._state:
            return False
        self._state = changed
        if self._selected_id is not None and self.point_position(self._selected_id) is None:
            self.select_point(None)
        self._rebuild_curve()
        self.update()
        self.stateEdited.emit(changed)
        return True

    def _report_error(self, action: str, error: ValueError) -> None:
        self.statusRequested.emit("error", f"{action}: {error}")

    def add_point(self, frequency: float, gain: float) -> bool:
        try:
            changed = model.add_point(self._state, frequency, gain)
        except ValueError as error:
            self._report_error("Add point", error)
            return False
        previous_ids = {point["id"] for point in self._state["points"]}
        added_id = next((point["id"] for point in changed["points"]
                         if point["id"] not in previous_ids), None)
        applied = self._commit(changed)
        if added_id is not None:
            self.select_point(added_id)
        return applied

    def set_selected_values(self, frequency: float, gain: float) -> bool:
        if self._selected_id is None:
            return False
        try:
            changed = model.set_point(self._state, self._selected_id, frequency, gain)
        except ValueError as error:
            self._report_error("Edit point", error)
            return False
        return self._commit(changed)

    def remove_selected_point(self) -> bool:
        if self._selected_id is None:
            return False
        try:
            changed = model.remove_point(self._state, self._selected_id)
        except ValueError as error:
            self._report_error("Remove point", error)
            return False
        return self._commit(changed)

    def reset_selected_point(self) -> bool:
        """Back to 0 dB at the same frequency."""
        point = next((point for point in self._state["points"] if point["id"] == self._selected_id), None)
        if point is None:
            return False
        return self.set_selected_values(point["frequency"], 0.0)

    def adjust_selected_width(self, factor: float) -> bool:
        if self._selected_id is None:
            return False
        try:
            changed = model.adjust_width(self._state, self._selected_id, factor)
        except ValueError as error:
            self._report_error("Adjust width", error)
            return False
        return self._commit(changed)

    def edit_selected_point(self) -> None:
        if self._edit_dialog is not None:
            self._edit_dialog.raise_()
            return
        point = next((point for point in self._state["points"]
                      if point["id"] == self._selected_id), None)
        if point is None:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Edit point")
        dialog.setMinimumWidth(260)
        layout = QVBoxLayout(dialog)
        fields = QFormLayout()
        frequency = QDoubleSpinBox(dialog)
        frequency.setRange(model.MIN_HZ, model.MAX_HZ)
        frequency.setDecimals(3)
        frequency.setSingleStep(1.0)
        frequency.setSuffix(" Hz")
        frequency.setValue(point["frequency"])
        frequency.setEnabled(point is not self._state["points"][0]
                             and point is not self._state["points"][-1])
        gain = QDoubleSpinBox(dialog)
        gain.setRange(model.MIN_DB, model.MAX_DB)
        gain.setDecimals(2)
        gain.setSingleStep(0.1)
        gain.setSuffix(" dB")
        gain.setValue(point["gain"])
        shown_frequency, shown_gain = frequency.value(), gain.value()
        fields.addRow("Frequency", frequency)
        fields.addRow("Gain", gain)
        layout.addLayout(fields)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=dialog,
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Apply")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        point_id = point["id"]

        def apply_values() -> None:
            if self.point_position(point_id) is None:
                return
            self.select_point(point_id)
            self.set_selected_values(
                point["frequency"] if frequency.value() == shown_frequency else frequency.value(),
                point["gain"] if gain.value() == shown_gain else gain.value(),
            )

        dialog.accepted.connect(apply_values)
        dialog.finished.connect(lambda _: setattr(self, "_edit_dialog", None))
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._edit_dialog = dialog
        dialog.open()

    def hideEvent(self, event) -> None:
        self._menu_timer.stop()
        if self._dragging_id is not None:
            self._dragging_id = None
            self._drag_start_state = None
            self._drag_last_sent = None
            self._drag_moved = False
            self.unsetCursor()
            self.editFinished.emit()
        super().hideEvent(event)

    def set_scale(self, scale: float) -> None:
        self.scale = scale
        self._grid = None
        self._rebuild_curve()
        self._rebuild_spectrum()
        self.update()

    def resizeEvent(self, event) -> None:
        self._grid = None
        self._rebuild_curve()
        self._rebuild_spectrum()
        super().resizeEvent(event)

    def changeEvent(self, event) -> None:
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange,
                            QEvent.Type.StyleChange):
            self._grid = None
        super().changeEvent(event)

    def _rebuild_grid(self) -> None:
        ratio = self.devicePixelRatioF()
        grid = QPixmap(max(1, math.ceil(self.width() * ratio)),
                       max(1, math.ceil(self.height() * ratio)))
        grid.setDevicePixelRatio(ratio)
        grid.fill(_BG)
        painter = QPainter(grid)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.graph_rect()
        painter.fillRect(rect, _PLOT)
        for decade in (10, 100, 1000, 10000):
            for multiple in range(1, 10):
                frequency = decade * multiple
                if not model.MIN_HZ < frequency < model.MAX_HZ:
                    continue
                painter.setPen(QPen(QColor("#2B2B30") if multiple in (1, 5)
                                    else QColor("#202024"), 1))
                x = self.frequency_to_x(frequency)
                painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
        for gain in (12, 6, 0, -6, -12):
            painter.setPen(QPen(QColor("#3B3B42") if gain == 0
                                else QColor("#29292E"), 1))
            y = self.gain_to_y(gain)
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
        painter.setPen(QPen(QColor("#424249"), 1))
        painter.drawRect(rect)

        k = self.scale
        painter.setFont(QFont("Segoe UI", 9 * k))
        painter.setPen(_TEXT)
        for gain in (12, 6, 0, -6, -12):
            label = f"+{gain}" if gain > 0 else str(gain)
            painter.drawText(QRectF(rect.left() - 42 * k, self.gain_to_y(gain) - 9 * k, 32 * k, 18 * k),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                             label)
        for frequency, label in _FREQUENCY_LABELS:
            painter.drawText(QRectF(self.frequency_to_x(frequency) - 30 * k,
                                    rect.bottom() + 6 * k, 60 * k, 17 * k),
                             Qt.AlignmentFlag.AlignCenter, label)
        # Units sit with their numbers instead of taking up a title line each.
        painter.setPen(_MUTED)
        painter.drawText(QRectF(rect.left() - 42 * k, rect.top() - 22 * k, 32 * k, 17 * k),
                         Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, "dB")
        painter.end()
        self._grid = grid
        self._grid_dpr = ratio

    @staticmethod
    def _frequency_text(frequency: float) -> str:
        if frequency >= 1000:
            text = f"{frequency / 1000:.2f}".rstrip("0")
            return (text + "0" if text.endswith(".") else text) + " kHz"
        return f"{frequency:.1f}".rstrip("0").rstrip(".") + " Hz"

    def _draw_selection(self, painter: QPainter) -> None:
        point = next((item for item in self._state["points"]
                      if item["id"] == self._selected_id), None)
        if point is None:
            return
        center = QPointF(self.frequency_to_x(point["frequency"]),
                         self.gain_to_y(point["gain"]))
        k = self.scale
        painter.setFont(QFont("Segoe UI", 9 * k))
        first = self._frequency_text(point["frequency"])
        second = f"{point['gain']:+.1f} dB"
        metrics = painter.fontMetrics()
        width = max(metrics.horizontalAdvance(first),
                    metrics.horizontalAdvance(second)) + 19 * k
        rect = self.graph_rect()
        left = center.x() + 17 * k
        if left + width > rect.right() - 4:
            left = center.x() - width - 17 * k
        left = max(rect.left() + 5, min(left, rect.right() - width - 5))
        top = center.y() - 45 * k
        if top < rect.top() + 5:
            top = center.y() + 16 * k
        top = max(rect.top() + 5, min(top, rect.bottom() - 45 * k))
        box = QRectF(left, top, width, 40 * k)
        right_of_point = box.center().x() > center.x()
        tail_x = box.left() if right_of_point else box.right()
        tail_y = max(box.top() + 9, min(center.y(), box.bottom() - 9))
        tip_x = center.x() + 8 if right_of_point else center.x() - 8
        tail = QPainterPath()
        tail.moveTo(tail_x, tail_y - 6)
        tail.lineTo(tip_x, center.y())
        tail.lineTo(tail_x, tail_y + 6)
        tail.closeSubpath()
        bubble = QPainterPath()
        bubble.addRoundedRect(box, 3, 3)
        bubble = bubble.united(tail)
        painter.setBrush(QColor("#1B1B1F"))
        painter.setPen(QPen(QColor("#696970"), 1))
        painter.drawPath(bubble)
        painter.setPen(QColor("#F4F5F4"))
        painter.drawText(QRectF(left + 9 * k, top + 4 * k, width - 18 * k, 15 * k),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, first)
        painter.drawText(QRectF(left + 9 * k, top + 20 * k, width - 18 * k, 15 * k),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, second)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(227, 0, 140, 85), 5))
        painter.drawEllipse(center, 9, 9)
        painter.setPen(QPen(QColor("#F6F7F6"), 1.5))
        painter.drawEllipse(center, 7, 7)
        painter.setPen(QPen(_PINK, 1.5))
        painter.drawEllipse(center, 5, 5)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#F8F8F8"))
        painter.drawEllipse(center, 2.3, 2.3)

    def paintEvent(self, event) -> None:
        if self._grid is None or self._grid_dpr != self.devicePixelRatioF():
            self._rebuild_grid()
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._grid)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.save()
        painter.setClipRect(self.graph_rect())
        if self._spectrum_values is not None:
            rect = self.graph_rect()
            spectrum_gradient = QLinearGradient(0, rect.top(), 0, rect.bottom())
            spectrum_gradient.setColorAt(0, QColor(244, 66, 66, 42))
            spectrum_gradient.setColorAt(1, QColor(244, 66, 66, 8))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(spectrum_gradient)
            painter.drawPath(self._spectrum_fill)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(_SPECTRUM, 1.3))
            painter.drawPath(self._spectrum_path)
            painter.setFont(QFont("Segoe UI", 8 * self.scale))
            painter.setPen(QColor("#E89797"))
            painter.drawText(QRectF(rect.right() - 179 * self.scale, rect.top() + 5, 170 * self.scale, 16 * self.scale),
                             Qt.AlignmentFlag.AlignRight, "SPEAKER  ·  −84–0 dBFS")
        if self._state["enabled"]:
            gradient = QLinearGradient(0, self.graph_rect().top(),
                                       0, self.graph_rect().bottom())
            gradient.setColorAt(0, QColor(227, 0, 140, 32))
            gradient.setColorAt(0.5, QColor(227, 0, 140, 29))
            gradient.setColorAt(1, QColor(227, 0, 140, 22))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(gradient)
            painter.drawPath(self._fill)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(227, 0, 140, 43 if self._state["enabled"] else 18), 5))
        painter.drawPath(self._curve)
        painter.setPen(QPen(_PINK if self._state["enabled"] else QColor(227, 0, 140, 90), 2))
        painter.drawPath(self._curve)
        if not self._total.isEmpty():
            painter.setPen(QPen(QColor(246, 247, 246, 170 if self._state["enabled"] else 60),
                                1.4, Qt.PenStyle.DashLine))
            painter.drawPath(self._total)
            painter.setFont(QFont("Segoe UI", 8 * self.scale))
            painter.setPen(QColor(246, 247, 246, 170))
            painter.drawText(QRectF(self.graph_rect().left() + 8, self.graph_rect().top() + 5, 220 * self.scale, 16 * self.scale),
                             Qt.AlignmentFlag.AlignLeft, "- - -  WITH CHARACTER")
        painter.restore()
        for point in self._state["points"]:
            if point["id"] == self._selected_id:
                continue
            center = QPointF(self.frequency_to_x(point["frequency"]),
                             self.gain_to_y(point["gain"]))
            painter.setPen(QPen(_PINK if self._state["enabled"]
                                else QColor(227, 0, 140, 110), 1.7))
            painter.setBrush(QColor("#F2F2F2") if self._state["enabled"] else _MUTED)
            painter.drawEllipse(center, 3.5, 3.5)
        if self._selected_id is not None:
            self._draw_selection(painter)
        painter.end()

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        self._menu_timer.stop()
        point_id = self.point_at(event.position())
        self._reclicked = point_id is not None and point_id == self._selected_id
        self.select_point(point_id)
        if point_id is not None:
            self._dragging_id = point_id
            self._drag_start_state = self._state
            self._drag_last_sent = self._state
            self._drag_press = event.position()
            self._drag_origin = self.point_position(point_id)
            self._drag_moved = False
            self.editStarted.emit()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._dragging_id is not None and self._drag_start_state is not None:
            delta = event.position() - self._drag_press
            if not self._drag_moved and delta.x() ** 2 + delta.y() ** 2 < 9:
                return
            self._drag_moved = True
            try:
                changed = model.set_point(
                    self._drag_start_state, self._dragging_id,
                    self.x_to_frequency(self._drag_origin.x() + delta.x()),
                    self.y_to_gain(self._drag_origin.y() + delta.y()),
                )
            except ValueError as error:
                self._report_error("Move point", error)
                return
            if changed != self._state:
                self._state = changed
                self._rebuild_curve()
                self.update()
                self._drag_last_sent = changed
                self.stateEdited.emit(changed)
            event.accept()
            return
        hovered = self.point_at(event.position())
        self.setCursor(Qt.CursorShape.PointingHandCursor if hovered
                       else Qt.CursorShape.CrossCursor if self.graph_rect().contains(event.position())
                       else Qt.CursorShape.ArrowCursor)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._dragging_id is not None:
            last_sent = self._drag_last_sent
            point_id = self._dragging_id
            self._dragging_id = None
            self._drag_start_state = None
            self._drag_last_sent = None
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            if self._drag_moved and last_sent is not None and self._state != last_sent:
                self.stateEdited.emit(self._state)
            self.editFinished.emit()
            # Clicking the already selected point without moving it opens its menu.
            if self._reclicked and not self._drag_moved:
                self._pending_menu = point_id, event.globalPosition().toPoint()
                self._menu_timer.start(QApplication.doubleClickInterval())
            self._reclicked = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._reclicked = False
            self._menu_timer.stop()
            self._dragging_id = None
            self._drag_start_state = None
            self._drag_last_sent = None
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            point_id = self.point_at(event.position())
            if point_id is not None:
                self.select_point(point_id)
                self.edit_selected_point()
            elif self.graph_rect().contains(event.position()):
                self.add_point(self.x_to_frequency(event.position().x()),
                               self.y_to_gain(event.position().y()))
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event) -> None:
        point_id = self.point_at(QPointF(event.pos()))
        if point_id is None:
            event.ignore()
            return
        self.select_point(point_id)
        self._point_menu(point_id, event.globalPos())
        event.accept()

    def _open_pending_menu(self) -> None:
        point_id, position = self._pending_menu
        if point_id == self._selected_id:
            self._point_menu(point_id, position)

    def _point_menu(self, point_id: str, position) -> None:
        point = next((point for point in self._state["points"] if point["id"] == point_id), None)
        if point is None:
            return
        menu = QMenu(self)
        menu.setObjectName("pointMenu")
        reset = menu.addAction("Reset to 0 dB", self.reset_selected_point)
        reset.setEnabled(point["gain"] != 0)
        menu.addAction("Edit exact values…", self.edit_selected_point)
        if point_id not in (self._state["points"][0]["id"],
                            self._state["points"][-1]["id"]):
            menu.addAction("Remove point", self.remove_selected_point)
        menu.aboutToHide.connect(menu.deleteLater)
        menu.popup(position)

    def wheelEvent(self, event) -> None:
        if self._selected_id is None or self.point_at(event.position()) != self._selected_id:
            event.ignore()
            return
        delta = event.angleDelta().y()
        if not delta:
            delta = event.pixelDelta().y() * 2
        self._wheel_remainder = max(-480, min(480, self._wheel_remainder + delta))
        steps = int(self._wheel_remainder / 120)
        if steps:
            self._wheel_remainder -= steps * 120
            self.adjust_selected_width(1.12 ** steps)
        event.accept()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape and self._drag_start_state is not None:
            last_sent = self._drag_last_sent
            self._state = self._drag_start_state
            self._dragging_id = None
            self._drag_start_state = None
            self._drag_last_sent = None
            self._rebuild_curve()
            self.update()
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            if last_sent is not None and self._state != last_sent:
                self.stateEdited.emit(self._state)
            self.editFinished.emit()
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self._selected_id is not None:
            self.remove_selected_point()
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self._selected_id is not None:
            self.edit_selected_point()
            event.accept()
            return
        super().keyPressEvent(event)
