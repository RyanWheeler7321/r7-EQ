"""Device profiles and presets, saved outside the source tree."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import tempfile
import time

from .devices import endpoint_guid
from .model import default_state, normalize_state

# Readers such as Equalizer APO briefly hold the target open (it re-reads r7-eq.txt after every
# change); Windows then refuses the replace with "Access is denied". Retry for up to ~0.8 s.
REPLACE_RETRY_DELAYS = (0.01, 0.02, 0.05, 0.1, 0.2, 0.4)


def data_directory():
    return Path(os.environ.get('APPDATA', Path.home() / '.config')) / 'R7-EQ'


def atomic_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        _replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _replace(source, target):
    for attempt, delay in enumerate((*REPLACE_RETRY_DELAYS, None), start=1):
        try:
            os.replace(source, target)
        except PermissionError:
            if delay is None:
                raise
            time.sleep(delay)
        else:
            if attempt > 1:
                logging.info('operation=replace,status=retried,attempts=%d,file=%s', attempt, Path(target).name)
            return


def valid_active(name, presets):
    return name if isinstance(name, str) and (name == 'Flat' or name in presets) else None


def preset_name(value):
    name = str(value).strip()
    if not name or len(name) > 64 or any(ord(char) < 32 for char in name):
        raise ValueError('Preset name must contain 1–64 printable characters')
    if name.casefold() == 'flat':
        raise ValueError('Flat is the built-in neutral preset')
    return name


def settled_active(active, state, presets):
    """Flat means exactly the neutral state; anything else unsaved is Custom (None)."""
    active = valid_active(active, presets)
    if active is None and state == default_state():
        return 'Flat'
    if active == 'Flat' and state != default_state():
        return None
    return active


def profile_key(endpoint_id):
    """File name for a device: its bare GUID."""
    return endpoint_guid(endpoint_id)[1:-1]


@dataclass
class Profile:
    endpoint_id: str
    name: str
    slot: int   # Keys this device's compressor meter; stays the same for the profile's life.
    state: dict = field(default_factory=default_state)
    presets: dict = field(default_factory=dict)
    active: str | None = 'Flat'

    @property
    def key(self):
        return profile_key(self.endpoint_id)

    def copy(self):
        return copy.deepcopy(self)


def _read_settings(raw):
    """state, presets and active from a profile or export file."""
    if not isinstance(raw.get('presets'), dict):
        raise ValueError('presets missing')
    presets = {preset_name(name): normalize_state(value) for name, value in raw['presets'].items()}
    state = normalize_state(raw['state'])
    return state, presets, settled_active(raw.get('active'), state, presets)


def _settings(profile):
    return {'state': normalize_state(profile.state),
            'presets': {preset_name(name): normalize_state(value) for name, value in profile.presets.items()},
            'active': valid_active(profile.active, profile.presets)}


class Store:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else data_directory()
        self.profiles_path = self.root / 'profiles'
        self.app_path = self.root / 'app.json'

    def profile_path(self, key):
        return self.profiles_path / f'{key}.json'

    def load_profiles(self):
        """A damaged file stops startup instead of being replaced."""
        profiles = {}
        if not self.profiles_path.is_dir():
            return profiles
        for path in sorted(self.profiles_path.glob('*.json')):
            try:
                raw = json.loads(path.read_text(encoding='utf-8'))
                if raw.get('version') != 1:
                    raise ValueError('unsupported profile format')
                slot = raw['slot']
                if type(slot) is not int or slot < 1:
                    raise ValueError('slot must be a positive whole number')
                state, presets, active = _read_settings(raw)
                profile = Profile(str(raw['endpoint_id']), str(raw['name']), slot, state, presets, active)
                if profile.key != path.stem:
                    raise ValueError('file name does not match its device')
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError(f'Cannot load {path}: {exc}') from exc
            profiles[profile.key] = profile
        return profiles

    def save_profile(self, profile):
        payload = {'version': 1, 'name': profile.name, 'endpoint_id': profile.endpoint_id,
                   'slot': profile.slot, **_settings(profile)}
        atomic_text(self.profile_path(profile.key), json.dumps(payload, indent=2) + '\n')

    def delete_profile(self, key):
        self.profile_path(key).unlink(missing_ok=True)

    def backup_profile(self, profile, reason='import'):
        stamp = time.strftime('%Y%m%d-%H%M%S')
        path = self.profiles_path / f'{profile.key}.before-{reason}-{stamp}.json.bak'
        payload = {'version': 1, 'name': profile.name, 'endpoint_id': profile.endpoint_id,
                   'slot': profile.slot, **_settings(profile)}
        atomic_text(path, json.dumps(payload, indent=2) + '\n')
        return path

    def _app_settings(self):
        try:
            settings = json.loads(self.app_path.read_text(encoding='utf-8'))
            if not isinstance(settings, dict):
                raise ValueError('not an object')
            return settings
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            logging.warning('operation=app-settings,status=unreadable,message=%s', exc)
            return {}

    def _save_app_settings(self, **changes):
        atomic_text(self.app_path, json.dumps({**self._app_settings(), **changes}, indent=2) + '\n')

    def last_device(self):
        return self._app_settings().get('device')

    def save_last_device(self, key):
        self._save_app_settings(device=key)

    def power(self):
        """R7-EQ's master switch, for every device."""
        return self._app_settings().get('power', True) is not False

    def save_power(self, on):
        self._save_app_settings(power=bool(on))

    @staticmethod
    def export_profile(profile, path):
        """A profile without its device, so it can load onto any device or PC."""
        payload = {'r7eq': 'profile', 'version': 1, **_settings(profile)}
        atomic_text(Path(path), json.dumps(payload, indent=2) + '\n')

    @staticmethod
    def read_export(path):
        """(state, presets, active) from a .r7eq file; ValueError when it isn't one."""
        try:
            raw = json.loads(Path(path).read_text(encoding='utf-8'))
            if raw.get('r7eq') != 'profile' or raw.get('version') != 1:
                raise ValueError('not an R7-EQ profile')
            return _read_settings(raw)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ValueError(f'Cannot import {Path(path).name}: {exc}') from exc
