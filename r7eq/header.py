"""The title bar: device, Output, the switches, presets and window buttons."""
from __future__ import annotations

from PySide6.QtCore import QRectF, QSignalBlocker, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QAbstractButton, QComboBox, QHBoxLayout, QLabel, QMenu, QToolButton, QWidget

from . import model
from .dial import Dial
from .footer import ValueField, divider


class Switch(QAbstractButton):
    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setText(text)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(text)
        self.setFont(QFont("Segoe UI", 10))
        self.scale = 1.0   # Drawn at this size; the header sets it with the window.

    def set_scale(self, scale: float) -> None:
        self.scale = scale
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:
        k = self.scale
        return QSize(round((44 + self.fontMetrics().horizontalAdvance(self.text()) + 4) * k), round(30 * k))

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        k = self.scale
        painter.scale(k, k)
        track = QColor("#E3008C") if self.isChecked() else QColor("#303034")
        if not self.isEnabled():
            track.setAlpha(90)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(track)
        painter.drawRoundedRect(QRectF(0, 6, 36, 18), 9, 9)
        painter.setBrush(QColor("#F8F8F8") if self.isEnabled() else QColor("#878D8A"))
        painter.drawEllipse(19 if self.isChecked() else 2, 8, 14, 14)
        painter.setPen(QPen(QColor("#E3008C"), 1) if self.hasFocus()
                       else QPen(Qt.PenStyle.NoPen))
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(0, 4, 38, 22), 11, 11)
        painter.setPen(QColor("#D6DBD9") if self.isEnabled() else QColor("#777E7C"))
        painter.drawText(QRectF(44, 0, self.width() / k - 44, self.height() / k),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                         self.text())
        painter.end()


def window_button(text: str, tooltip: str, parent: QWidget) -> QToolButton:
    button = QToolButton(parent)
    button.setObjectName("windowControl")
    button.setText(text)
    button.setToolTip(tooltip)
    button.setAccessibleName(tooltip)
    button.setFont(QFont("Segoe UI", 12))
    button.setFixedSize(29, 31)
    return button


def icon_button(glyph: str, tooltip: str, parent: QWidget) -> QToolButton:
    """A Windows icon-font glyph (Segoe Fluent Icons on Windows 11, MDL2 Assets on 10)."""
    button = QToolButton(parent)
    button.setObjectName("profileAction")
    button.setText(glyph)
    button.setToolTip(tooltip)
    button.setAccessibleName(tooltip.split(".")[0])
    font = QFont()
    font.setFamilies(["Segoe Fluent Icons", "Segoe MDL2 Assets"])
    font.setPointSize(12)
    button.setFont(font)
    button.setFixedSize(32, 32)
    return button


class Header(QWidget):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("header")
        self.setFixedHeight(60)
        self.setMouseTracking(True)
        self.scale = 1.0
        self._base: list[tuple] = []   # Each control's font, fixed width and fixed height at scale 1.
        controls = QHBoxLayout(self)
        controls.setContentsMargins(24, 0, 12, 0)
        controls.setSpacing(5)
        brand = QLabel("R7-EQ", self)
        brand.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        brand_font = QFont("Montserrat", 19, QFont.Weight.DemiBold)
        brand_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.7)
        brand.setFont(brand_font)
        brand.setStyleSheet("color: #F6F7F6;")
        controls.addWidget(brand)
        controls.addSpacing(8)
        self.device_selector = QComboBox(self)
        self.device_selector.setObjectName("deviceSelector")
        self.device_selector.setAccessibleName("Playback device")
        self.device_selector.setFont(QFont("Segoe UI", 10))
        self.device_selector.setToolTip("The device being edited. It follows the Windows default "
                                        "output; pick another to edit it while it isn't playing.")
        controls.addWidget(self.device_selector)
        controls.addSpacing(4)
        self.copy_button = icon_button("\uE8C8", "Copy settings from another device. "
                                       "Its presets stay its own; undo puts yours back.", self)
        self.copy_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.copy_menu = QMenu(self.copy_button)
        self.copy_button.setMenu(self.copy_menu)
        controls.addWidget(self.copy_button)
        self.fresh_button = icon_button("\uE72C", "Start this device fresh: Flat, no presets. "
                                        "The old profile is kept as a backup file.", self)
        controls.addWidget(self.fresh_button)
        self.delete_button = icon_button("\uE74D", "Delete this profile. Only for devices that "
                                         "aren't connected; a backup file is kept.", self)
        controls.addWidget(self.delete_button)
        self._gap(controls)

        # Output: gain before everything, headroom, and the gain actually applied.
        output_label = QLabel("Output", self)
        output_label.setToolTip("Output gain before everything else.")
        controls.addWidget(output_label)
        self.output_slider = Dial(28, self)
        self.output_slider.setRange(-1800, 600)
        self.output_slider.setSingleStep(10)
        self.output_slider.setPageStep(100)
        self.output_slider.origin = 0
        self.output_slider.setAccessibleName("Output gain")
        self.output_slider.setToolTip(output_label.toolTip())
        controls.addWidget(self.output_slider)
        self.output_value = ValueField(-18, 6, 2, 0.1, lambda value: f"{value:+.2f} dB" if value else "0.00 dB",
                                       self)
        self.output_value.setFont(QFont("Segoe UI", 9))
        self.output_value.setFixedHeight(self.output_value.fontMetrics().height() + 4)
        self.output_value.setFixedWidth(self.output_value.fontMetrics().horizontalAdvance("−18.00 dB") + 12)
        self.output_value.setAccessibleName("Exact output gain in decibels")
        self.output_value.setToolTip(output_label.toolTip())
        controls.addWidget(self.output_value)
        self._gap(controls)
        self.auto_toggle = Switch("Auto headroom", self)
        self.auto_toggle.setToolTip(
            "Caps applied output gain at the opposite of the highest EQ gain, including the "
            "Character sliders, plus room for what Space can add. "
            "Only reduces your chosen gain; this is not an audio peak limiter."
        )
        controls.addWidget(self.auto_toggle)
        controls.addSpacing(12)
        self.trim_label = QLabel("Applied", self)
        self.trim_label.setToolTip("Effective output gain after optional headroom compensation")
        controls.addWidget(self.trim_label)
        self.trim_value = QLabel(self)
        self.trim_value.setFont(QFont("Segoe UI", 9))
        self.trim_value.setStyleSheet("color: #DDE1DF;")
        self.trim_value.setToolTip(self.trim_label.toolTip())
        self.trim_value.setMinimumWidth(self.trim_value.fontMetrics().horizontalAdvance("−18.0 dB") + 4)
        controls.addWidget(self.trim_value)
        controls.addStretch(1)
        self._gap(controls)

        # Everything R7-EQ does, then the EQ (with Space) and the compressor on their own.
        self.power_toggle = Switch("All", self)
        self.power_toggle.setToolTip("R7-EQ on or off for every device: EQ, Space, compressor and clip guard. "
                                     "Off, your audio plays untouched.")
        controls.addWidget(self.power_toggle)
        controls.addSpacing(8)
        self.eq_toggle = Switch("EQ", self)
        self.eq_toggle.setToolTip("The EQ curve, Character and Space on this device.")
        controls.addWidget(self.eq_toggle)
        controls.addSpacing(8)
        self.comp_toggle = Switch("Comp", self)
        self.comp_toggle.setToolTip("The compressor on this device.")
        controls.addWidget(self.comp_toggle)
        controls.addSpacing(16)
        self.preset_selector = QComboBox(self)
        self.preset_selector.setAccessibleName("EQ preset")
        self.preset_selector.setFixedWidth(170)
        controls.addWidget(self.preset_selector)
        self.menu_button = QToolButton(self)
        self.menu_button.setObjectName("presetActions")
        self.menu_button.setText("...")
        self.menu_button.setFont(QFont("Segoe UI", 12))
        self.menu_button.setAccessibleName("More")
        self.menu_button.setToolTip("Presets and profile files. Presets save every change automatically.")
        self.menu_button.setFixedSize(25, 30)
        self.menu_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.menu_button)
        self.save_action = menu.addAction("Save as new preset…")
        self.delete_action = menu.addAction("Delete preset…")
        menu.addSeparator()
        self.export_action = menu.addAction("Export profile…")
        self.import_action = menu.addAction("Import profile…")
        menu.addSeparator()
        self.reset_action = menu.addAction("Reset to Flat")
        self.reset_action.setToolTip("Switch to Flat. Saved presets are kept.")
        self.menu_button.setMenu(menu)
        controls.addWidget(self.menu_button)
        controls.addSpacing(16)
        self.minimize_button = window_button("−", "Hide", self)
        controls.addWidget(self.minimize_button)
        self.maximize_button = window_button("□", "Maximize", self)
        controls.addWidget(self.maximize_button)
        self.close_button = window_button("×", "Hide editor", self)
        self.close_button.setObjectName("windowClose")
        controls.addWidget(self.close_button)

    def _fit_device_width(self) -> None:
        """As wide as the selected name (the list itself shows every name in full)."""
        k = self.scale
        text = self.device_selector.fontMetrics().horizontalAdvance(self.device_selector.currentText())
        self.device_selector.setFixedWidth(round(min(max(text + 42 * k, 120 * k), 280 * k)))

    def set_scale(self, scale: float) -> None:
        self.scale = scale
        if not self._base:
            for widget in self.findChildren(QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly):
                low, high = widget.minimumSize(), widget.maximumSize()
                width = low.width() if low.width() == high.width() else None
                height = low.height() if low.height() == high.height() else None
                self._base.append((widget, QFont(widget.font()), width, height))
        self.setFixedHeight(round(60 * scale))
        self.layout().setContentsMargins(round(24 * scale), 0, round(12 * scale), 0)
        for widget, font, width, height in self._base:
            if isinstance(widget, Switch):
                widget.set_scale(scale)
                continue
            scaled = QFont(font)
            scaled.setPointSizeF(font.pointSizeF() * scale)
            widget.setFont(scaled)
            # Fixed widths and heights grow too; divider lines stay one pixel wide.
            if width is not None and width > 1 and widget is not self.device_selector:
                widget.setFixedWidth(round(width * scale))
            if height is not None:
                widget.setFixedHeight(round(height * scale))
        self._fit_device_width()

    def _gap(self, controls: QHBoxLayout) -> None:
        controls.addSpacing(6)
        controls.addWidget(divider(self, 22))
        controls.addSpacing(6)

    def show_devices(self, devices: list[tuple[str, str, bool]], current: str | None) -> None:
        """Devices that aren't connected are dimmed."""
        with QSignalBlocker(self.device_selector):
            self.device_selector.clear()
            if not devices:
                self.device_selector.addItem("No device", None)
            for key, name, connected in devices:
                self.device_selector.addItem(name if connected else f"{name} (not connected)", key)
                if not connected:
                    index = self.device_selector.count() - 1
                    self.device_selector.setItemData(index, QColor("#7C8482"), Qt.ItemDataRole.ForegroundRole)
                    self.device_selector.setItemData(index, f"{name} is not connected. Edits are saved "
                                                     "and apply when it plays.", Qt.ItemDataRole.ToolTipRole)
            self.device_selector.setCurrentIndex(max(0, self.device_selector.findData(current)))
        self._fit_device_width()
        view = self.device_selector.view()
        view.setMinimumWidth(view.sizeHintForColumn(0) + 30)
        connected = next((item[2] for item in devices if item[0] == current), True)
        self.device_selector.setStyleSheet("" if connected else "color: #7C8482;")
        has_device = current is not None
        for action in (self.export_action, self.import_action):
            action.setEnabled(has_device)
        self.copy_button.setEnabled(has_device and len(devices) > 1)
        self.fresh_button.setEnabled(has_device)
        self.delete_button.setEnabled(has_device and not connected)

    def show_state(self, state: dict) -> None:
        with QSignalBlocker(self.eq_toggle), QSignalBlocker(self.auto_toggle), QSignalBlocker(self.output_slider), \
                QSignalBlocker(self.comp_toggle):
            self.eq_toggle.setChecked(state["enabled"])
            self.comp_toggle.setChecked(state["compressor"])
            self.auto_toggle.setChecked(state["auto_headroom"])
            self.output_slider.setValue(round(state["preamp_db"] * 100))
            self.output_value.setValue(state["preamp_db"])
        self.trim_value.setText(f"{model.effective_preamp(state):+.1f} dB")

    def show_presets(self, presets: list[str]) -> None:
        with QSignalBlocker(self.preset_selector):
            self.preset_selector.clear()
            self.preset_selector.addItem("Custom (unsaved)", None)
            for name in presets:
                self.preset_selector.addItem(name, name)

    def show_active_preset(self, active: str | None) -> None:
        index = self.preset_selector.findData(active) if active is not None else 0
        with QSignalBlocker(self.preset_selector):
            self.preset_selector.setCurrentIndex(max(0, index))
        self.delete_action.setEnabled(active not in (None, "Flat"))
