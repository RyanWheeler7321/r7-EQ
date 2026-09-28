"""UI state, plus a background writer that only writes the latest change."""
from __future__ import annotations

import copy
import logging
import threading
import time

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from .backend import ApoBackend
from .compression import MAX_SLOT
from .devices import Devices
from .model import default_state, normalize_state
from .storage import Profile, Store, preset_name, profile_key, settled_active, valid_active


class _Writer(QObject):
    finished = Signal(int, str, str, float, bool)

    def __init__(self, store, backend):
        super().__init__()
        self.store, self.backend = store, backend
        self.condition = threading.Condition()
        self.pending = None
        self.unsaved = set()      # Profiles whose last save failed; retried with the next write.
        self.removed = set()
        self.busy = False
        self.stopping = False
        self.saved_revision = 0
        self.failed_revision = 0
        self.thread = threading.Thread(target=self._run, name='R7-EQ settings', daemon=True)
        self.thread.start()

    def submit(self, revision, profiles, changed=(), removed=(), attach=False, power=True):
        """Latest wins, but every profile changed since the last write is still saved."""
        with self.condition:
            attach = attach or (self.pending is not None and self.pending[2])
            self.unsaved.update(changed)
            self.removed.update(removed)
            self.unsaved -= self.removed
            self.pending = (revision, {key: profile.copy() for key, profile in profiles.items()}, attach, power)
            self.condition.notify()

    def _run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.pending is not None or self.stopping)
                if self.pending is None and self.stopping:
                    return
                revision, profiles, attach, power = self.pending
                unsaved, removed = set(self.unsaved), set(self.removed)
                self.pending = None
                self.unsaved.clear()
                self.removed.clear()
                self.busy = True
            started = time.perf_counter()
            saved, changed = False, False
            try:
                for key in removed:
                    self.store.delete_profile(key)
                for key in list(unsaved):
                    if key in profiles:
                        self.store.save_profile(profiles[key])
                    unsaved.discard(key)
                saved = True
                write = self.backend.attach if attach else self.backend.apply
                severity, message, changed = write(profiles.values(), power)
            except Exception as exc:
                severity, message = 'error', str(exc)
            elapsed = (time.perf_counter() - started) * 1000
            with self.condition:
                if saved:
                    self.saved_revision = revision
                else:
                    self.failed_revision = revision
                    self.unsaved |= unsaved
                    self.removed |= removed
                self.busy = False
                self.condition.notify_all()
            self.finished.emit(revision, severity, message, elapsed, changed)

    def flush(self, revision, timeout=3.0):
        deadline = time.monotonic() + timeout
        with self.condition:
            while self.saved_revision < revision and time.monotonic() < deadline:
                if self.failed_revision >= revision and not self.busy and self.pending is None:
                    return False
                self.condition.wait(max(0, deadline - time.monotonic()))
            return self.saved_revision >= revision

    def close(self):
        with self.condition:
            self.stopping = True
            self.condition.notify()
        self.thread.join(timeout=3.0)
        return not self.thread.is_alive()


class Controller(QObject):
    """Every change to the selected profile also goes into its active preset."""
    stateChanged = Signal(dict)
    statusChanged = Signal(str, str)
    presetsChanged = Signal(list)
    devicesChanged = Signal()   # The profile list, the selection or a device's connection changed.
    powerChanged = Signal(bool)

    def __init__(self, store=None, backend=None, devices=None, parent=None):
        super().__init__(parent)
        self.store = store or Store()
        self.profiles = self.store.load_profiles()
        self.power = self.store.power()
        self.backend = backend or ApoBackend()
        self.devices = devices or Devices(self)
        self.devices.defaultChanged.connect(self._follow_default)
        self.devices.devicesChanged.connect(self.refresh_devices)
        self._connected = {}    # Connected playback devices: endpoint ID -> name.
        self._apo = {}          # Endpoint ID -> Equalizer APO installed on it.
        self._key = None
        self._history = {}      # Profile key -> (undo, redo).
        self._last_edit = 0.0
        self._editing = False
        self._edit_recorded = False
        self._revision = 0
        self._last_status = None
        self._apply_count = 0
        self._apply_max_ms = 0.0
        self._closed = False
        self.writer = _Writer(self.store, self.backend)
        self.writer.finished.connect(self._finished)
        self.apply_timer = QTimer(self)
        self.apply_timer.setSingleShot(True)
        self.apply_timer.setInterval(60)
        self.apply_timer.timeout.connect(self._dispatch)
        self.log_timer = QTimer(self)
        self.log_timer.setSingleShot(True)
        self.log_timer.setInterval(600)
        self.log_timer.timeout.connect(self._log_writes)
        self._pending_changes = set()

    # The selected profile ------------------------------------------------------------------

    @property
    def profile(self):
        return self.profiles.get(self._key)

    @property
    def device(self):
        """None until a device has been added."""
        return self._key

    @property
    def state(self):
        return copy.deepcopy(self.profile.state) if self.profile else default_state()

    @property
    def presets(self):
        return ['Flat'] + sorted(self.profile.presets, key=str.casefold) if self.profile else ['Flat']

    @property
    def active_preset(self):
        """'Flat', a user preset name, or None for an unsaved custom setup."""
        return self.profile.active if self.profile else 'Flat'

    def connected(self, key=None):
        profile = self.profiles.get(key or self._key)
        return profile is not None and profile.endpoint_id in self._connected

    def device_list(self):
        """[(key, name, connected)] for the selector, by name."""
        return sorted(((key, profile.name, profile.endpoint_id in self._connected)
                       for key, profile in self.profiles.items()), key=lambda item: item[1].casefold())

    def other_profiles(self):
        return [(key, name) for key, name, _ in self.device_list() if key != self._key]

    def device_warning(self):
        profile = self.profile
        if profile is None:
            return 'No playback device found.'
        if profile.endpoint_id not in self._connected:
            return f'{profile.name} is not connected. Edits are saved and apply when it is.'
        if not self._apo.get(profile.endpoint_id, True):
            return (f'Equalizer APO is not installed on {profile.name}. '
                    "Add it with Equalizer APO's Device Selector.")
        return None

    # Startup and devices -------------------------------------------------------------------

    def initialize(self):
        self.devices.start()
        self._scan_devices()
        self._add_new_devices()
        default = self.devices.default_device()
        key = self._key_for(default)
        if key not in self.profiles:
            key = self.store.last_device()
        if key not in self.profiles:
            key = next(iter(sorted(self.profiles)), None)
        self._key = key
        self.devicesChanged.emit()
        self.stateChanged.emit(self.state)
        self.presetsChanged.emit(self.presets)
        try:
            self.statusChanged.emit(*self.backend.status())
        except Exception as exc:
            self.statusChanged.emit('error', str(exc))
        self._revision += 1
        self._dispatch()

    @staticmethod
    def _key_for(endpoint_id):
        try:
            return profile_key(endpoint_id)
        except ValueError:
            return None

    def _scan_devices(self):
        try:
            self._connected = {device['id']: device['name'] for device in self.devices.playback_devices()}
            self._apo = {identifier: self.devices.apo_installed(identifier) for identifier in self._connected}
        except Exception as exc:   # Keep the last known list; never break editing.
            logging.warning('operation=device-scan,status=failed,message=%s', exc)
        for profile in self.profiles.values():   # Windows renamed a device: follow it.
            name = self._connected.get(profile.endpoint_id)
            if name and name != profile.name:
                profile.name = name
                self._pending_changes.add(profile.key)

    def _add_new_devices(self):
        """New devices start Flat, which changes nothing."""
        managed = {profile.endpoint_id for profile in self.profiles.values()}
        new = [(identifier, name) for identifier, name in self._connected.items() if identifier not in managed]
        used = {profile.slot for profile in self.profiles.values()}
        free = (slot for slot in range(1, MAX_SLOT + 1) if slot not in used)
        for identifier, name in sorted(new, key=lambda item: item[1].casefold()):
            slot = next(free, None)
            if slot is None:
                logging.error('operation=device-add,status=no-slot,name=%r', name)
                break
            profile = Profile(identifier, name, slot)
            self.profiles[profile.key] = profile
            self._pending_changes.add(profile.key)
            logging.info('operation=device-add,name=%r,slot=%d', name, slot)
        if new:
            # The first profile also connects R7-EQ to Equalizer APO's config.txt.
            self._revision += 1
            self._dispatch(attach=True)

    @Slot()
    def refresh_devices(self):
        self._scan_devices()
        self._add_new_devices()
        if self._key is None:
            self._select_default()
        self.devicesChanged.emit()
        self._show_device_status()
        if self._pending_changes:
            self._revision += 1
            self._dispatch()

    def _select_default(self):
        key = self._key_for(self.devices.default_device())
        if key not in self.profiles:
            key = next(iter(sorted(self.profiles)), None)
        if key:
            self.select_device(key)

    @Slot(str)
    def _follow_default(self, endpoint_id):
        """A device Windows just added may not be scanned yet, so an unknown one triggers a scan."""
        key = self._key_for(endpoint_id)
        if key and key not in self.profiles:
            self.refresh_devices()
        logging.info('operation=default-device,endpoint_id=%s,managed=%s', endpoint_id, key in self.profiles)
        if key in self.profiles and key != self._key:
            self.select_device(key)

    def select_device(self, key):
        if key not in self.profiles or key == self._key:
            return
        self.end_edit()
        self._key = key
        try:
            self.store.save_last_device(key)
        except OSError as exc:
            logging.warning('operation=app-settings,status=save-failed,message=%s', exc)
        self.devicesChanged.emit()
        self.presetsChanged.emit(self.presets)
        self.stateChanged.emit(self.state)
        self._show_device_status()
        logging.info('operation=device-select,name=%r', self.profile.name)

    def delete_profile(self, key):
        """Only for devices that aren't connected. A backup file is kept."""
        profile = self.profiles.get(key)
        if profile is None or self.connected(key):
            return
        try:
            self.store.backup_profile(profile, 'delete')
        except OSError as exc:
            self.statusChanged.emit('error', f'Could not back up {profile.name}: {exc}')
            return
        del self.profiles[key]
        self._history.pop(key, None)
        self._pending_changes.discard(key)
        logging.info('operation=profile-delete,name=%r', profile.name)
        self._revision += 1
        self._dispatch(removed={key})
        if key == self._key:
            self._key = None
            self._select_default()
            if self._key:
                return
            self.presetsChanged.emit(self.presets)
            self.stateChanged.emit(self.state)
        self.devicesChanged.emit()
        self._show_device_status()

    def start_fresh(self):
        """Flat with no presets, like a new device. A backup file is kept."""
        profile = self.profile
        if profile is None:
            return
        try:
            self.store.backup_profile(profile, 'reset')
        except OSError as exc:
            self.statusChanged.emit('error', f'Could not back up {profile.name}: {exc}')
            return
        profile.state, profile.presets, profile.active = default_state(), {}, 'Flat'
        self._history.pop(self._key, None)
        self._last_edit = 0
        logging.info('operation=profile-reset,name=%r', profile.name)
        self.presetsChanged.emit(self.presets)
        self._changed()

    def copy_from(self, key):
        """Copies the settings only; presets stay with their device."""
        source = self.profiles.get(key)
        if source is None or self.profile is None or key == self._key:
            return
        self._commit(copy.deepcopy(source.state), discrete=True)
        logging.info('operation=profile-copy,source=%r,target=%r', source.name, self.profile.name)

    def _show_device_status(self):
        warning = self.device_warning()
        if warning:
            self.statusChanged.emit('warning', warning)
        elif self._last_status:
            self.statusChanged.emit(*self._last_status)

    # Export and import ---------------------------------------------------------------------

    def export_profile(self, path):
        if self.profile is None:
            return
        try:
            self.store.export_profile(self.profile, path)
        except OSError as exc:
            self.statusChanged.emit('error', f'Export failed: {exc}')
            return
        self.statusChanged.emit('success', f'Exported {self.profile.name} to {path}')
        logging.info('operation=profile-export,name=%r', self.profile.name)

    def import_profile(self, path):
        """The old settings and presets are kept in a backup."""
        profile = self.profile
        if profile is None:
            return
        try:
            state, presets, active = self.store.read_export(path)
            backup = self.store.backup_profile(profile)
        except (OSError, ValueError) as exc:
            self.statusChanged.emit('error', str(exc))
            return
        profile.state, profile.presets, profile.active = state, presets, active
        self._history.pop(self._key, None)
        self._last_edit = 0
        logging.info('operation=profile-import,name=%r,backup=%s', profile.name, backup.name)
        self.presetsChanged.emit(self.presets)
        self._changed()

    # Editing -------------------------------------------------------------------------------

    def set_state(self, raw, discrete=False):
        """discrete: a one-click change (a button), always its own undo step."""
        self._commit(raw, discrete)

    def begin_edit(self):
        self._editing = True
        self._edit_recorded = False

    def end_edit(self):
        self._editing = False
        self._last_edit = 0

    def _undo_redo(self):
        return self._history.setdefault(self._key, ([], []))

    def _commit(self, raw, discrete=False):
        if self.profile is None:
            return
        try:
            state = normalize_state(raw)
        except (ValueError, KeyError, TypeError) as exc:
            self.statusChanged.emit('error', str(exc))
            return
        if state == self.profile.state:
            return
        now = time.monotonic()
        record = discrete or (not self._edit_recorded if self._editing else now - self._last_edit > 0.35)
        if record:
            self._remember()
            self._edit_recorded = self._editing
        self._undo_redo()[1].clear()
        self._last_edit = 0 if discrete else now
        self.profile.state = state
        self._follow_active()
        self._changed()

    def _snapshot(self):
        return self.state, self.profile.active

    def _remember(self):
        undo = self._undo_redo()[0]
        undo.append(self._snapshot())
        del undo[:-100]

    def _restore(self, snapshot):
        self.profile.state = copy.deepcopy(snapshot[0])
        self.profile.active = valid_active(snapshot[1], self.profile.presets)
        self._last_edit = 0
        self._follow_active()
        self._changed()

    def _follow_active(self):
        """Autosave: the selected user preset always holds the live settings."""
        profile = self.profile
        if profile.active in profile.presets:
            profile.presets[profile.active] = copy.deepcopy(profile.state)
        else:
            profile.active = settled_active(profile.active, profile.state, profile.presets)

    def _changed(self):
        self._revision += 1
        self._pending_changes.add(self._key)
        self.stateChanged.emit(self.state)
        # Throttle, rather than restart a debounce: dragging remains audible.
        if not self.apply_timer.isActive():
            self.apply_timer.start()
        self.log_timer.start()

    def undo(self):
        if self.profile is None:
            return
        undo, redo = self._undo_redo()
        if undo:
            redo.append(self._snapshot())
            self._restore(undo.pop())

    def redo(self):
        if self.profile is None:
            return
        undo, redo = self._undo_redo()
        if redo:
            undo.append(self._snapshot())
            self._restore(redo.pop())

    def reset(self):
        self.select_preset('Flat')

    def select_preset(self, name):
        profile = self.profile
        if profile is None:
            return
        if name == 'Flat':
            target = default_state()
        elif name in profile.presets:
            target = profile.presets[name]
        else:
            return
        if name == profile.active and target == profile.state:
            return
        self._remember()
        self._undo_redo()[1].clear()
        self._last_edit = 0
        profile.active = name
        profile.state = copy.deepcopy(target)
        self._changed()
        logging.info('operation=preset-select,name=%r', name)

    def save_preset(self, name):
        if self.profile is None:
            return
        try:
            name = preset_name(name)
        except ValueError as exc:
            self.statusChanged.emit('error', str(exc))
            return
        self.profile.presets[name] = self.state
        self.profile.active = name
        self._last_edit = 0  # The next adjustment is its own undo step, not merged across the save.
        self.presetsChanged.emit(self.presets)
        self._changed()
        logging.info('operation=preset-save,name=%r', name)

    def delete_preset(self, name):
        profile = self.profile
        if profile is not None and name in profile.presets:
            del profile.presets[name]
            if profile.active == name:
                profile.active = None
            self.presetsChanged.emit(self.presets)
            self._changed()
            logging.info('operation=preset-delete,name=%r', name)

    # Writing -------------------------------------------------------------------------------

    @Slot()
    def _dispatch(self, attach=False, removed=()):
        self.apply_timer.stop()
        changed, self._pending_changes = self._pending_changes, set()
        self.writer.submit(self._revision, self.profiles, {key for key in changed if key}, removed, attach, self.power)

    def set_power(self, on):
        """Off writes no section for any device, so they all play untouched."""
        if on == self.power:
            return
        self.power = on
        try:
            self.store.save_power(on)
        except OSError as exc:
            logging.warning('operation=app-settings,status=save-failed,message=%s', exc)
        logging.info('operation=power,on=%s', on)
        self.powerChanged.emit(on)
        self._revision += 1
        self._dispatch()

    @Slot(int, str, str, float, bool)
    def _finished(self, revision, severity, message, elapsed, changed):
        self._apply_count += int(changed)
        self._apply_max_ms = max(elapsed, self._apply_max_ms)
        if (severity, message) != self._last_status:
            self._last_status = (severity, message)
            logging.log(logging.ERROR if severity == 'error' else logging.INFO,
                        'operation=apply,status=%s,revision=%d,dur_ms=%.2f,message=%s', severity, revision, elapsed, message)
        if revision == self._revision:
            warning = self.device_warning()
            if severity == 'info' and warning:
                severity, message = 'warning', warning
            self.statusChanged.emit(severity, message)
        if not self.log_timer.isActive():
            self.log_timer.start()

    def _log_writes(self):
        if self._apply_count:
            points = len(self.profile.state['points']) if self.profile else 0
            logging.info('operation=edit,status=written,revision=%d,points=%d,writes=%d,max_write_ms=%.2f',
                         self._revision, points, self._apply_count, self._apply_max_ms)
        self._apply_count = 0
        self._apply_max_ms = 0.0

    def flush(self, timeout=3.0):
        if self.apply_timer.isActive():
            self._dispatch()
        return self.writer.flush(self._revision, timeout)

    def close(self):
        if self._closed:
            return True
        if not self.flush():
            self.statusChanged.emit('error', 'Could not save the latest edit; window kept open')
            return False
        self.log_timer.stop()
        self._log_writes()
        self.devices.close()
        self._closed = self.writer.close()
        return self._closed
