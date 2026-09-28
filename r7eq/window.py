"""The main window. The analyzer and meter only run while it's visible."""
from __future__ import annotations

import json
import logging
from pathlib import Path
import time
from typing import TYPE_CHECKING, Callable

import numpy as np
from PySide6.QtCore import QByteArray, QEvent, QSignalBlocker, Qt, Signal, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QFileDialog, QInputDialog, QMainWindow, QMessageBox,
                               QVBoxLayout, QWidget)

from .compression import MAX_LIFT_DB, twitch_settings
from .model import SLIDER_DEFAULTS, TWITCH_AMOUNT
from .meter import MeterReader
from .footer import BASE_WIDTH, LOG_SLIDERS, MAX_SCALE, Footer, slider_value
from .graph import GraphWidget
from .header import Header
from .spectrum import FREQUENCIES, SpectrumMonitor
from .storage import atomic_text

if TYPE_CHECKING:
    from .controller import Controller


_METER_TOOLTIP = ("Now: the boost the compressor is applying to the audio playing right now. "
                  "Max: the most it can add at this setting (shown in silence or while hidden).")
_METER_FAILED_TOOLTIP = ("Audio is playing but the compressor sends no readings: Windows audio has not "
                         "loaded it. See the status line and r7-eq.log.")
_METER_IDLE_TOOLTIP = "This device isn't connected, so its compressor isn't running."
_METER_MISSING_POLLS = 25     # ~1.7 s at the 67 ms poll, longer than an Equalizer APO reload.
_LIVE_FLOOR_DB = -60.0
_LIVE_INTERVAL_S = 0.125
# Per-band loudness weight for the analyzer's log bands: equal-width-per-octave bands hold
# power ∝ bandwidth ∝ frequency, then an approximate ITU-R BS.1770 K-weighting.
_K_WEIGHT_DB = (10 * np.log10(FREQUENCIES ** 4 / (FREQUENCIES ** 4 + 38.0 ** 4))
                + 4.0 / (1 + (1500.0 / FREQUENCIES) ** 2))
_LIVE_WEIGHT = FREQUENCIES * 10 ** (_K_WEIGHT_DB / 10)
_FILE_FILTER = "r7-EQ profile (*.r7eq)"


_STYLE = """
QMainWindow#R7EqWindow, QWidget#body, QWidget#header, QWidget#footer {
    background: #0D0D0F;
    color: #E4E7E6;
}
QWidget#header { border-bottom: 1px solid #252529; }
QLabel { color: #DDE1DF; background: transparent; }
QPushButton, QComboBox, QDoubleSpinBox {
    background: #19191C;
    color: #E1E5E3;
    border: 1px solid #343438;
    border-radius: 4px;
    padding: 5px 8px;
    selection-background-color: #E3008C;
}
QPushButton:hover, QComboBox:hover, QDoubleSpinBox:hover { border-color: #E3008C; }
QPushButton:pressed { background: #272329; }
QPushButton:disabled { color: #77777D; border-color: #252529; }
QPushButton:focus, QComboBox:focus, QDoubleSpinBox:focus {
    border-color: #E3008C;
}
QComboBox#deviceSelector { background: transparent; border-color: transparent; }
QComboBox#deviceSelector:hover, QComboBox#deviceSelector:focus { border-color: #E3008C; }
QToolButton#presetActions, QToolButton#windowControl, QToolButton#windowClose, QToolButton#profileAction {
    background: transparent;
    color: #C9CECC;
    border: 0;
    border-radius: 3px;
    padding: 0;
}
QToolButton#presetActions:hover, QToolButton#windowControl:hover, QToolButton#profileAction:hover {
    background: #242428;
    color: #FFFFFF;
}
QToolButton#profileAction { color: #9EA5A2; }
QToolButton#profileAction:disabled { color: #45454B; }
QToolButton#presetActions:focus, QToolButton#windowControl:focus,
QToolButton#windowClose:focus, QToolButton#profileAction:focus {
    border: 1px solid #E3008C;
}
QToolButton#windowClose:hover { background: #673047; color: #FFFFFF; }
QToolButton#presetActions::menu-indicator, QToolButton#profileAction::menu-indicator { image: none; width: 0; }
QComboBox::drop-down { border: 0; width: 19px; }
QComboBox QAbstractItemView {
    background: #19191C;
    color: #E4E7E6;
    border: 1px solid #343438;
    selection-background-color: #E3008C;
    selection-color: #FFFFFF;
    outline: none;
}
QSlider::groove:horizontal { height: 4px; background: #303034; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #E3008C; border-radius: 2px; }
QSlider::handle:horizontal {
    background: #F1F0F0;
    border: 2px solid #E3008C;
    width: 12px;
    margin: -6px 0;
    border-radius: 7px;
}
QDialog { background: #111113; color: #E4E7E6; }
QDialog QLabel { color: #E4E7E6; }
QMenu {
    background: #19191C;
    color: #E4E7E6;
    border: 1px solid #343438;
    padding: 3px;
}
QMenu::item { padding: 6px 18px; }
QMenu::item:selected { background: #793354; }
QMenu::item:disabled { color: #77777D; }
QMenu::separator { height: 1px; background: #303034; margin: 3px 6px; }
QFrame#footerDivider { background: #303034; border: 0; }
QPushButton#chipButton { background: #19191C; color: #B9C0BD; border: 1px solid #343438;
                         border-radius: 4px; padding: 0; }
QFrame#sectionBox { background: #121215; border: 1px solid #2A2A30; border-radius: 3px; }
QPushButton#chipButton:hover, QPushButton#chipButton:focus { color: #FFFFFF; border-color: #E3008C; }
QPushButton#chipButton:pressed { background: #272329; }
QLineEdit#valueField {
    background: transparent;
    color: #DDE1DF;
    border: 1px solid transparent;
    border-radius: 3px;
    padding: 0 2px;
    selection-background-color: #E3008C;
    selection-color: #FFFFFF;
}
QLineEdit#valueField:hover { border-color: #343438; }
QLineEdit#valueField:focus { background: #19191C; border-color: #E3008C; }
QLineEdit#valueField:disabled { color: #77777D; }
"""


class MainWindow(QMainWindow):
    hideRequested = Signal()

    def __init__(self, controller: Controller, spectrum_factory: Callable | None = None,
                 meter_factory: Callable[[], MeterReader] | None = None) -> None:
        super().__init__()
        self.controller = controller
        self._dialog: QWidget | None = None
        self._resize_cursor_widget: QWidget | None = None
        self.setObjectName("R7EqWindow")
        self.setWindowTitle("r7-EQ")
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
        self._base_width = BASE_WIDTH   # Raised to the real minimum once the bars are built.
        self.resize(920, 720)
        self.setMinimumSize(710, 545)
        self.setStyleSheet(_STYLE)

        body = QWidget(self)
        body.setObjectName("body")
        body.setMouseTracking(True)
        column = QVBoxLayout(body)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        self.setCentralWidget(body)
        self.header = Header(body)
        column.addWidget(self.header)
        self.graph = GraphWidget(controller.state, body)
        column.addWidget(self.graph, 1)
        self.footer = Footer(body)
        column.addWidget(self.footer)
        self._fit_minimum_width()

        self._live_power: np.ndarray | None = None
        self._live_at = 0.0
        self._live_stale = QTimer(self)
        self._live_stale.setSingleShot(True)
        self._live_stale.setInterval(700)
        self._live_stale.timeout.connect(self._clear_live)
        self.spectrum = SpectrumMonitor(None, self, capture_factory=spectrum_factory)
        self.spectrum.frameReady.connect(self.graph.set_spectrum)
        self.spectrum.frameReady.connect(self._on_spectrum)
        self.spectrum.statusChanged.connect(self.footer.show_status)
        QApplication.instance().aboutToQuit.connect(self.spectrum.shutdown)
        self.meter = (meter_factory or MeterReader)()
        self._meter_max_db = 0.0
        self._meter_misses = 0        # Polls with audio playing but no reading from the plugin.
        self._meter_failed = False
        self._meter_timer = QTimer(self)
        self._meter_timer.setInterval(67)
        self._meter_timer.timeout.connect(self._poll_meter)
        QApplication.instance().aboutToQuit.connect(self._stop_meter)
        for surface in (body, self.header, self.graph, self.footer):
            surface.installEventFilter(self)

        header, footer = self.header, self.footer
        self.graph.stateEdited.connect(controller.set_state)
        self.graph.editStarted.connect(controller.begin_edit)
        self.graph.editFinished.connect(controller.end_edit)
        self.graph.statusRequested.connect(footer.show_status)
        controller.stateChanged.connect(self._on_state_changed)
        controller.presetsChanged.connect(self._on_presets_changed)
        controller.statusChanged.connect(footer.show_status)
        controller.devicesChanged.connect(self._on_devices_changed)
        header.device_selector.activated.connect(self._select_device)
        header.eq_toggle.toggled.connect(lambda enabled: self._set("enabled", enabled))
        header.comp_toggle.toggled.connect(lambda enabled: self._set("compressor", enabled))
        header.power_toggle.setChecked(controller.power)
        header.power_toggle.toggled.connect(controller.set_power)
        controller.powerChanged.connect(self._on_power_changed)
        header.output_slider.resetRequested.connect(lambda: self._set_output_from_value(0.0))
        header.auto_toggle.toggled.connect(lambda enabled: self._set("auto_headroom", enabled))
        header.output_slider.valueChanged.connect(self._set_output_from_slider)
        header.output_slider.sliderPressed.connect(controller.begin_edit)
        header.output_slider.sliderReleased.connect(controller.end_edit)
        header.output_value.valueEntered.connect(self._set_output_from_value)
        header.preset_selector.activated.connect(self._select_preset)
        header.save_action.triggered.connect(self._save_preset)
        header.delete_action.triggered.connect(self._delete_preset)
        header.export_action.triggered.connect(self._export_profile)
        header.import_action.triggered.connect(self._import_profile)
        header.copy_menu.aboutToShow.connect(self._fill_copy_menu)
        header.fresh_button.clicked.connect(self._start_fresh)
        header.delete_button.clicked.connect(self._delete_profile)
        header.reset_action.triggered.connect(self._reset)
        header.minimize_button.clicked.connect(self._request_hide)
        header.maximize_button.clicked.connect(self._toggle_maximized)
        header.close_button.clicked.connect(self._request_hide)
        footer.compression_slider.valueChanged.connect(lambda value: self._set("compression", value))
        footer.compression_slider.sliderPressed.connect(controller.begin_edit)
        footer.compression_slider.sliderReleased.connect(controller.end_edit)
        footer.compression_value.valueEntered.connect(lambda value: self._set("compression", value))
        footer.compression_slider.resetRequested.connect(lambda: self._set("compression", TWITCH_AMOUNT))
        footer.twitch_button.clicked.connect(self._use_twitch_compressor)
        for key, slider in footer.sliders.items():
            slider.valueChanged.connect(
                lambda value, key=key, steps=slider.property("steps"):
                    self._set(key, slider_value(key, value) if key in LOG_SLIDERS else value / steps))
            slider.sliderPressed.connect(controller.begin_edit)
            slider.sliderReleased.connect(controller.end_edit)
            footer.slider_values[key].valueEntered.connect(lambda value, key=key: self._set(key, value))
            slider.resetRequested.connect(lambda key=key: self._set(key, SLIDER_DEFAULTS[key]))

        self.undo_shortcut = QShortcut(QKeySequence("Ctrl+Z"), self)
        self.undo_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self.undo_shortcut.activated.connect(controller.undo)
        self.redo_shortcut = QShortcut(QKeySequence("Ctrl+Shift+Z"), self)
        self.redo_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self.redo_shortcut.activated.connect(controller.redo)

        self._on_devices_changed()
        self._on_presets_changed(controller.presets)
        self._on_state_changed(controller.state)

        # Window position/size survive restarts; saved shortly after a move/resize settles.
        self._geometry_path = controller.store.root / "window.json"
        self._geometry_timer = QTimer(self)
        self._geometry_timer.setSingleShot(True)
        self._geometry_timer.setInterval(400)
        self._geometry_timer.timeout.connect(self._save_geometry)
        self._geometry_ready = False
        self._restore_geometry()
        self._geometry_ready = True

    # Window geometry, hiding and resizing --------------------------------------------------

    def _restore_geometry(self) -> None:
        try:
            saved = json.loads(self._geometry_path.read_text(encoding="utf-8"))["geometry"]
        except FileNotFoundError:
            return
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logging.warning("operation=window-geometry,status=unreadable,message=%s", exc)
            return
        # Qt moves a window restored onto a disconnected monitor back onto a visible screen.
        if not self.restoreGeometry(QByteArray.fromBase64(str(saved).encode("ascii"))):
            logging.warning("operation=window-geometry,status=rejected")

    def _save_geometry(self) -> None:
        self._geometry_timer.stop()
        payload = {"version": 1, "geometry": bytes(self.saveGeometry().toBase64()).decode("ascii")}
        try:
            atomic_text(self._geometry_path, json.dumps(payload) + "\n")
        except OSError as exc:
            logging.warning("operation=window-geometry,status=save-failed,message=%s", exc)

    def _geometry_changed(self) -> None:
        if getattr(self, "_geometry_ready", False) and self.isVisible():
            self._geometry_timer.start()

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        self._geometry_changed()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_scale()
        self._geometry_changed()

    def _fit_scale(self) -> None:
        """Past its base width, everything grows together."""
        # Measured from the narrowest width everything fits at scale 1, so a scale never overflows.
        scale = round(min(MAX_SCALE, max(1.0, self.width() / self._base_width)), 2)
        if scale != self.footer.scale:
            self.header.set_scale(scale)
            self.graph.set_scale(scale)
            self.footer.set_scale(scale)

    def _request_hide(self) -> None:
        self.hideRequested.emit()

    def closeEvent(self, event) -> None:
        event.ignore()
        self._request_hide()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # Equalizer APO can be added to a device while the editor is hidden. The scan takes
        # ~25 ms, so it runs right after the window appears.
        QTimer.singleShot(0, self.controller.refresh_devices)
        self._update_spectrum()

    def hideEvent(self, event) -> None:
        if self._geometry_timer.isActive():
            self._save_geometry()
        self.spectrum.stop()
        self.graph.clear_spectrum()
        self._clear_live()
        self._stop_meter()
        self._show_meter_idle()
        super().hideEvent(event)

    def _toggle_maximized(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.Type.WindowStateChange and hasattr(self, "header"):
            maximized = self.isMaximized()
            self.header.maximize_button.setText("❐" if maximized else "□")
            self.header.maximize_button.setToolTip("Restore" if maximized else "Maximize")
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange and hasattr(self, "spectrum"):
            self._update_spectrum()

    def _resize_edges(self, position):
        if self.isMaximized() or self.isFullScreen():
            return None
        edges = []
        if position.x() < 6:
            edges.append(Qt.Edge.LeftEdge)
        elif position.x() >= self.width() - 6:
            edges.append(Qt.Edge.RightEdge)
        if position.y() < 6:
            edges.append(Qt.Edge.TopEdge)
        elif position.y() >= self.height() - 6:
            edges.append(Qt.Edge.BottomEdge)
        if not edges:
            return None
        result = edges[0]
        for edge in edges[1:]:
            result |= edge
        return result

    def eventFilter(self, watched, event) -> bool:
        if watched is self.graph and event.type() in (QEvent.Type.Show, QEvent.Type.Hide):
            self._update_spectrum()
        if event.type() == QEvent.Type.Leave and watched is self._resize_cursor_widget:
            watched.unsetCursor()
            self._resize_cursor_widget = None
        if event.type() not in (QEvent.Type.MouseMove, QEvent.Type.MouseButtonPress,
                                QEvent.Type.MouseButtonDblClick):
            return super().eventFilter(watched, event)
        position = watched.mapTo(self, event.position().toPoint())
        edges = self._resize_edges(position)
        if event.type() == QEvent.Type.MouseMove:
            if self._resize_cursor_widget is not None and self._resize_cursor_widget is not watched:
                self._resize_cursor_widget.unsetCursor()
                self._resize_cursor_widget = None
            if edges is None:
                if self._resize_cursor_widget is watched:
                    watched.unsetCursor()
                    self._resize_cursor_widget = None
            else:
                corner = bool(edges & (Qt.Edge.LeftEdge | Qt.Edge.RightEdge)) \
                    and bool(edges & (Qt.Edge.TopEdge | Qt.Edge.BottomEdge))
                if corner:
                    descending = bool(edges & Qt.Edge.LeftEdge) == bool(edges & Qt.Edge.TopEdge)
                    cursor = Qt.CursorShape.SizeFDiagCursor if descending \
                        else Qt.CursorShape.SizeBDiagCursor
                elif edges & (Qt.Edge.LeftEdge | Qt.Edge.RightEdge):
                    cursor = Qt.CursorShape.SizeHorCursor
                else:
                    cursor = Qt.CursorShape.SizeVerCursor
                watched.setCursor(cursor)
                self._resize_cursor_widget = watched
                return True
        elif event.button() == Qt.MouseButton.LeftButton:
            if edges is not None:
                handle = self.windowHandle()
                if handle is not None and handle.startSystemResize(edges):
                    return True
            elif watched is self.header:
                if event.type() == QEvent.Type.MouseButtonDblClick:
                    self._toggle_maximized()
                else:
                    handle = self.windowHandle()
                    if handle is not None:
                        handle.startSystemMove()
                return True
        return super().eventFilter(watched, event)

    # Analyzer and compressor meter: only while the window is visible -----------------------

    def _update_spectrum(self) -> None:
        if self.isVisible() and not self.isMinimized() and self.graph.isVisibleTo(self):
            self.spectrum.start()
        else:
            self.spectrum.stop()
            self.graph.clear_spectrum()
            self._clear_live()
        self._update_meter()

    def _stop_meter(self) -> None:
        self._meter_timer.stop()
        self.meter.close()

    def _meter_blocked(self) -> str:
        """Why no reading can come, or '' when the meter can be polled."""
        if not self._meter_max_db:
            return "off"
        if not self.controller.connected():
            return "not playing"
        return ""

    def _show_meter_idle(self) -> None:
        blocked = self._meter_blocked()
        if blocked == "off":
            self.footer.set_meter_text("", _METER_TOOLTIP)
        elif blocked:
            self.footer.set_meter_text(blocked, _METER_IDLE_TOOLTIP)
        else:
            self.footer.set_meter_text(f"max +{self._meter_max_db:.1f}", _METER_TOOLTIP)

    def _update_meter(self) -> None:
        if self._meter_blocked() or not self.isVisible() or self.isMinimized():
            self._stop_meter()
            self._show_meter_idle()
        elif not self._meter_timer.isActive():
            self._meter_timer.start()
            self._poll_meter()

    def _poll_meter(self) -> None:
        if not self.isVisible() or self.isMinimized() or self._meter_blocked():
            self._update_meter()
            return
        measurement = self.meter.read(self._meter_max_db)
        if measurement is not None:
            self._meter_misses = 0
            self._meter_failed = False
            self.footer.set_meter_text(f"now {measurement[0]:+.1f}", _METER_TOOLTIP)
            return
        # Audio is audible on the device, yet the compressor reports nothing: it is not loaded.
        self._meter_misses = self._meter_misses + 1 if self._live_power is not None else 0
        if self._meter_misses >= _METER_MISSING_POLLS:
            if not self._meter_failed:
                logging.error("operation=compressor_meter,status=not-running,max_db=%.1f",
                              self._meter_max_db)
            self._meter_failed = True
            self.footer.set_meter_text("not running", _METER_FAILED_TOOLTIP, "#F19A9A")
        elif not self._meter_failed:
            self._show_meter_idle()

    def _reset_meter(self) -> None:
        self._meter_misses = 0
        self._meter_failed = False

    def _on_spectrum(self, _frequencies, levels_db) -> None:
        """Keeps the latest loudness-weighted band powers; readouts refresh at ~8 Hz."""
        self._live_stale.start()
        now = time.monotonic()
        if now - self._live_at < _LIVE_INTERVAL_S:
            return
        self._live_at = now
        levels = np.asarray(levels_db, dtype=float)
        self._live_power = None if levels.max() < _LIVE_FLOOR_DB \
            else 10 ** (levels / 10) * _LIVE_WEIGHT
        self.footer.set_live_power(self._live_power)

    def _clear_live(self) -> None:
        self._live_stale.stop()
        if self._live_power is not None:
            self._live_power = None
            self.footer.set_live_power(None)

    # Controller -> window ------------------------------------------------------------------

    def _on_devices_changed(self) -> None:
        controller = self.controller
        profile = controller.profile
        self.header.show_devices(controller.device_list(), controller.device)
        self._fit_minimum_width()   # The device name's width changes the header's.
        # The analyzer listens to the edited device, and only while it's connected.
        self.spectrum.set_endpoint(profile.endpoint_id if profile and controller.connected() else None)
        if not self.spectrum.endpoint_id:
            self.graph.clear_spectrum()
            self._clear_live()
        self.meter.set_slot(profile.slot if profile else 0)
        self._reset_meter()
        has_device = profile is not None
        for widget in (self.graph, self.footer, self.header.output_slider, self.header.output_value,
                       self.header.auto_toggle, self.header.eq_toggle, self.header.comp_toggle,
                       self.header.preset_selector):
            widget.setEnabled(has_device)
        for action in (self.header.save_action, self.header.reset_action):
            action.setEnabled(has_device)
        self._update_spectrum()

    def _fit_minimum_width(self) -> None:
        """Never narrower than the header and footer need, so nothing is clipped."""
        # Asked directly: the body's cached size only catches up on the next layout pass. Measured
        # at the current scale, then brought back to scale 1: the window may shrink back down.
        for bar in (self.header, self.footer):
            bar.setMinimumWidth(0)
        needed = max(self.header.minimumSizeHint().width(), self.footer.minimumSizeHint().width(),
                     self.centralWidget().minimumSizeHint().width())
        needed = round(needed / self.footer.scale)
        self.setMinimumWidth(max(710, needed))
        self._base_width = max(BASE_WIDTH, needed)
        # Scaled up, the bars would otherwise hold the window at their scaled width; narrowing it
        # rescales them to fit, since the scale follows the width.
        for bar in (self.header, self.footer):
            bar.setMinimumWidth(1)

    def _on_state_changed(self, state: dict) -> None:
        self.graph.set_state(state)
        self.header.show_state(state)
        self.footer.show_state(state)
        maximum = MAX_LIFT_DB * state["compression"] / 100 if state["compressor"] else 0.0
        if maximum != self._meter_max_db:
            was_active = self._meter_timer.isActive()
            self._meter_max_db = maximum
            self._reset_meter()
            self._update_meter()
            if maximum and was_active and self._meter_timer.isActive():
                self._poll_meter()
        elif not maximum:
            self.footer.set_meter_text("", _METER_TOOLTIP)
        self.header.show_active_preset(self.controller.active_preset)

    def _on_presets_changed(self, presets: list[str]) -> None:
        self.header.show_presets(presets)
        self.header.show_active_preset(self.controller.active_preset)

    # Window -> controller ------------------------------------------------------------------

    def _on_power_changed(self, on: bool) -> None:
        with QSignalBlocker(self.header.power_toggle):
            self.header.power_toggle.setChecked(on)

    def _use_twitch_compressor(self) -> None:
        state = self.controller.state
        state.update(twitch_settings())
        self.controller.set_state(state, discrete=True)

    def _set(self, key: str, value) -> None:
        state = self.controller.state
        state[key] = value
        self.controller.set_state(state)

    def _set_output_from_slider(self, value: int) -> None:
        self.header.output_value.setValue(value / 100)
        self._set("preamp_db", value / 100)

    def _set_output_from_value(self, value: float) -> None:
        self.header.output_slider.blockSignals(True)
        self.header.output_slider.setValue(round(value * 100))
        self.header.output_slider.blockSignals(False)
        self._set("preamp_db", value)

    def _select_device(self, index: int) -> None:
        key = self.header.device_selector.itemData(index)
        if key is not None:
            self.controller.select_device(key)
        self.header.show_devices(self.controller.device_list(), self.controller.device)

    def _select_preset(self, index: int) -> None:
        name = self.header.preset_selector.itemData(index)
        if name is not None:
            self.controller.select_preset(name)
        self.header.show_active_preset(self.controller.active_preset)

    def _reset(self) -> None:
        self.controller.reset()
        self.header.show_active_preset(self.controller.active_preset)

    def _fill_copy_menu(self) -> None:
        menu = self.header.copy_menu
        menu.clear()
        menu.addSection("Copy settings from")
        for key, name in self.controller.other_profiles():
            menu.addAction(name, lambda key=key: self.controller.copy_from(key))

    # Dialogs: one at a time, never blocking the event loop ---------------------------------

    def _open(self, dialog: QWidget) -> bool:
        if self._dialog is not None:
            self._dialog.raise_()
            return False
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.finished.connect(lambda _: setattr(self, "_dialog", None))
        self._dialog = dialog
        dialog.open()
        return True

    def _confirm(self, title: str, text: str, on_yes: Callable[[], None]) -> None:
        dialog = QMessageBox(
            QMessageBox.Icon.Question, title, text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, self,
        )
        dialog.setDefaultButton(QMessageBox.StandardButton.Cancel)
        dialog.finished.connect(lambda result: on_yes() if result == int(QMessageBox.StandardButton.Yes) else None)
        self._open(dialog)

    def _save_preset(self) -> None:
        dialog = QInputDialog(self)
        dialog.setWindowTitle("Save as new preset")
        dialog.setLabelText("New preset name (it keeps saving as you adjust):")
        dialog.setInputMode(QInputDialog.InputMode.TextInput)
        dialog.setTextValue("")
        dialog.setOkButtonText("Save")
        dialog.accepted.connect(lambda: QTimer.singleShot(0, lambda: self._save_named_preset(dialog.textValue())))
        self._open(dialog)

    def _save_named_preset(self, name: str) -> None:
        name = name.strip()
        if not name:
            self.footer.show_status("error", "Enter a name to save the preset.")
            return
        if name == "Flat":
            self.footer.show_status("error", "Flat is built in; choose another name.")
            return
        if name in self.controller.presets and name != self.controller.active_preset:
            self._confirm("Replace preset", f'Replace "{name}" with the current settings?',
                          lambda: self._store_preset(name))
            return
        self._store_preset(name)

    def _store_preset(self, name: str) -> None:
        self.controller.save_preset(name)
        self.header.show_active_preset(self.controller.active_preset)

    def _delete_preset(self) -> None:
        name = self.controller.active_preset
        if name is None or name == "Flat":
            return
        self._confirm("Delete preset", f'Delete "{name}"?', lambda: self._remove_preset(name))

    def _remove_preset(self, name: str) -> None:
        self.controller.delete_preset(name)
        self.header.show_active_preset(self.controller.active_preset)

    def _start_fresh(self) -> None:
        profile = self.controller.profile
        if profile is None:
            return
        self._confirm("Start fresh",
                      f"Start {profile.name} fresh? Its settings go back to Flat and its presets are "
                      "removed. The old profile is kept as a backup file.", self.controller.start_fresh)

    def _delete_profile(self) -> None:
        profile = self.controller.profile
        if profile is None or self.controller.connected():
            return
        key = profile.key
        self._confirm("Delete profile",
                      f"Delete the profile for {profile.name}? If it's connected again later, it "
                      "starts Flat. A backup file is kept.", lambda: self.controller.delete_profile(key))

    def _file_dialog(self, title: str, save: bool, on_file: Callable[[str], None]) -> None:
        dialog = QFileDialog(self, title)
        dialog.setNameFilter(_FILE_FILTER)
        dialog.setDefaultSuffix("r7eq")
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave if save else QFileDialog.AcceptMode.AcceptOpen)
        dialog.setFileMode(QFileDialog.FileMode.AnyFile if save else QFileDialog.FileMode.ExistingFile)
        dialog.setDirectory(str(Path.home() / "Documents"))
        if save and self.controller.profile:
            dialog.selectFile(f"{self.controller.profile.name}.r7eq")
        # Deferred, so a following confirmation opens after this dialog has closed.
        dialog.fileSelected.connect(lambda path: QTimer.singleShot(0, lambda: on_file(path)))
        self._open(dialog)

    def _export_profile(self) -> None:
        if self.controller.profile is not None:
            self._file_dialog("Export profile", True, self.controller.export_profile)

    def _import_profile(self) -> None:
        profile = self.controller.profile
        if profile is None:
            return
        self._file_dialog("Import profile", False, lambda path: self._confirm(
            "Import profile",
            f"Replace {profile.name}'s settings and presets with {Path(path).name}? "
            "The current ones are kept in a backup file.",
            lambda: self.controller.import_profile(path)))
