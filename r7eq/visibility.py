"""Show and hide, also through a registered window message (wParam 0 toggle, 1 show, 2 hide)."""
from __future__ import annotations

import ctypes
import logging
import os
import time

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, QTimer, Qt
from PySide6.QtWidgets import QApplication, QDialog
from shiboken6 import isValid

MESSAGE_NAME = 'R7EQ.Visibility.v1'
WINDOW_PROPERTY = 'R7EQ.Managed.v1'

def owned_dialogs(window):
    result = []
    for widget in QApplication.topLevelWidgets():
        if not isinstance(widget, QDialog) or not widget.isVisible():
            continue
        parent = widget.parent()
        while parent is not None:
            if parent is window:
                result.append(widget)
                break
            parent = parent.parent()
    return result


class Visibility(QObject):
    def __init__(self, window, parent=None):
        super().__init__(parent)
        self.window = window
        self.dialogs = []

    def request(self, action):
        started = time.perf_counter()
        if action == 'toggle':
            action = 'hide' if self.window.isVisible() and not self.window.isMinimized() else 'show'
        if action == 'hide':
            if not self.window.isVisible():
                return True
            self.dialogs = owned_dialogs(self.window)
            for dialog in self.dialogs:
                dialog.hide()
            self.window.hide()
        elif action == 'show':
            if self.window.isMinimized():
                self.window.showNormal()
            else:
                self.window.show()
            if os.name == 'nt' and QApplication.platformName() == 'windows':
                # A hidden process's first ShowWindow can inherit SW_HIDE.
                # ShowWindowAsync bypasses that startup override without activation.
                ctypes.windll.user32.ShowWindowAsync(ctypes.c_void_p(int(self.window.winId())), 8)
            self.window.raise_()
            for dialog in self.dialogs:
                if isValid(dialog):
                    dialog.show()
            self.dialogs.clear()
        else:
            return False
        logging.info('operation=visibility,action=%s,visible=%d,dur_ms=%.2f', action,
                     self.window.isVisible(), (time.perf_counter() - started) * 1000)
        return True


class NativeVisibility(QAbstractNativeEventFilter):
    def __init__(self, visibility, managed=True):
        super().__init__()
        self.visibility = visibility
        self.message = 0
        self.hwnd = 0
        if os.name == 'nt' and QApplication.platformName() == 'windows':
            self.message = ctypes.windll.user32.RegisterWindowMessageW(MESSAGE_NAME)
            self.hwnd = int(visibility.window.winId())
            if managed:
                user32 = ctypes.windll.user32
                user32.SetPropW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_void_p]
                user32.SetPropW.restype = ctypes.c_int
                if not user32.SetPropW(self.hwnd, WINDOW_PROPERTY, 1):
                    raise ctypes.WinError()

    def nativeEventFilter(self, event_type, message):
        if not self.message:
            return False, 0
        from ctypes import wintypes
        native = wintypes.MSG.from_address(int(message))
        if native.hWnd != self.hwnd or native.message != self.message:
            return False, 0
        action = {0: 'toggle', 1: 'show', 2: 'hide'}.get(int(native.wParam))
        if action is not None:
            QTimer.singleShot(0, lambda: self.visibility.request(action))
        return True, 0
