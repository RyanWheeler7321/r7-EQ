"""Windows playback devices: which are connected, which is the default, and which have Equalizer APO."""
from __future__ import annotations

import logging
import os
import re
import sys
import threading
import uuid

from PySide6.QtCore import QObject, Signal

ENDPOINT = re.compile(r'^\{0\.0\.0\.[0-9a-fA-F]{8}\}\.\{([0-9a-fA-F-]{36})\}$')
E_RENDER, E_MULTIMEDIA, DEVICE_STATE_ACTIVE = 0, 1, 1
_com = threading.local()


def _load_pycaw():
    """comtypes fails on a thread already in the other COM mode, so it's imported on its own thread."""
    if 'comtypes' not in sys.modules:
        thread = threading.Thread(target=__import__, args=('pycaw.pycaw',), name='R7-EQ COM import')
        thread.start()
        thread.join()
    if not getattr(_com, 'ready', False):
        import ctypes
        ctypes.windll.ole32.CoInitializeEx(None, 2)   # Apartment; an existing mode is kept.
        _com.ready = True
    from pycaw import pycaw
    return pycaw.AudioUtilities


def endpoint_guid(identifier):
    match = ENDPOINT.fullmatch(str(identifier))
    if not match:
        raise ValueError('An exact Windows playback device ID is required')
    return '{' + str(uuid.UUID(match.group(1))) + '}'


def playback_devices():
    if os.name != 'nt':
        return []
    return [{'id': device.id, 'name': device.FriendlyName}
            for device in _load_pycaw().GetAllDevices(E_RENDER, DEVICE_STATE_ACTIVE)
            if ENDPOINT.fullmatch(device.id)]


def default_device():
    if os.name != 'nt':
        return None
    try:
        return _load_pycaw().GetDeviceEnumerator().GetDefaultAudioEndpoint(E_RENDER, E_MULTIMEDIA).GetId()
    except Exception as exc:   # No playback device at all.
        logging.info('operation=default-device,status=none,message=%s', exc)
        return None


def apo_installed(identifier):
    """Set in Equalizer APO's Device Selector."""
    if os.name != 'nt':
        return True
    import winreg
    key = r'SOFTWARE\EqualizerAPO\Child APOs' + '\\' + endpoint_guid(identifier)
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY):
            return True
    except FileNotFoundError:
        return False


class Devices(QObject):
    """Change notifications arrive on the GUI thread. No polling."""
    defaultChanged = Signal(str)
    devicesChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._enumerator = None
        self._client = None

    playback_devices = staticmethod(playback_devices)
    default_device = staticmethod(default_device)
    apo_installed = staticmethod(apo_installed)

    def start(self):
        if os.name != 'nt' or self._client is not None:
            return
        watcher = self
        try:
            audio = _load_pycaw()
            from pycaw.callbacks import MMNotificationClient

            class Client(MMNotificationClient):
                # Called on a Windows audio thread; Qt queues the signals to the GUI thread.
                def on_default_device_changed(self, flow, flow_id, role, role_id, device_id):
                    if flow_id == E_RENDER and role_id == E_MULTIMEDIA:
                        watcher.defaultChanged.emit(device_id or '')

                def on_device_added(self, device_id):
                    watcher.devicesChanged.emit()

                def on_device_removed(self, device_id):
                    watcher.devicesChanged.emit()

                def on_device_state_changed(self, device_id, new_state, new_state_id):
                    watcher.devicesChanged.emit()

            self._enumerator = audio.GetDeviceEnumerator()
            self._client = Client()
            self._enumerator.RegisterEndpointNotificationCallback(self._client)
        except Exception as exc:
            self._enumerator = self._client = None
            logging.error('operation=device-watch,status=failed,message=%s', exc)

    def close(self):
        if self._client is not None:
            try:
                self._enumerator.UnregisterEndpointNotificationCallback(self._client)
            except Exception as exc:
                logging.warning('operation=device-watch,status=unregister-failed,message=%s', exc)
            self._enumerator = self._client = None
