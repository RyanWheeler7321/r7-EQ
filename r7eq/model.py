"""EQ curve math, with no GUI or audio dependencies."""
from __future__ import annotations

import bisect
import copy
import math
import uuid

import numpy as np

MIN_HZ, MAX_HZ = 20.0, 20000.0
MIN_DB, MAX_DB = -12.0, 12.0
MAX_POINTS = 64
MIN_LOG_GAP = 0.006
INITIAL_FREQUENCIES = (20, 32, 50, 80, 125, 200, 315, 500, 800, 1250, 2000, 3150, 5000, 8000, 12500, 20000)
# Character sliders layered over the drawn curve: state key -> (minimum dB, maximum dB).
TONE_RANGES = {'tilt_db': (-3.0, 3.0), 'warmth_db': (0.0, 6.0),
               'presence_db': (-4.0, 2.0), 'air_db': (0.0, 4.0)}
# Speaker compressor (Chrome's, as on Twitch): where it starts, how softly, how hard it holds,
# its timing, how it listens, and the parallel dry blend. Ratio stops at Chrome's 20:1.
COMPRESSOR_RANGES = {'comp_threshold_db': (-100.0, 0.0), 'comp_knee_db': (0.0, 40.0),
                     'comp_ratio': (1.0, 20.0), 'comp_attack_ms': (0.0, 200.0),
                     'comp_release_ms': (1.0, 1000.0), 'comp_bass_hz': (0.0, 300.0),
                     'comp_dry_pct': (0.0, 100.0)}
# r7 Space plugin: stereo width, speaker 3D, room amount and room size (percent).
SPACE_RANGES = {'width_pct': (0.0, 200.0), 'depth_3d': (0.0, 100.0),
                'room_pct': (0.0, 100.0), 'room_size': (0.0, 100.0)}
SLIDER_RANGES = {**TONE_RANGES, **COMPRESSOR_RANGES, **SPACE_RANGES}
SLIDER_DEFAULTS = {key: 0.0 for key in SLIDER_RANGES} | {'width_pct': 100.0, 'room_size': 50.0}
# New profiles start from Twitch's compressor settings (Amount stays off).
SLIDER_DEFAULTS |= {'comp_threshold_db': -50.0, 'comp_knee_db': 40.0, 'comp_ratio': 12.0,
                    'comp_release_ms': 250.0}
# Amount (boost) that matches Twitch's: +12.4 dB of the 36 dB maximum.
TWITCH_AMOUNT = 34.6
# Profiles saved before the Chrome compressor used ReaComp's fixed 6 dB knee.
REACOMP_KNEE_DB = 6.0


def _number(value, name):
    if isinstance(value, bool):
        raise ValueError(f'{name} must be a finite number')
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f'{name} must be a finite number')
    return value


def default_state():
    return {'enabled': True, 'preamp_db': 0.0, 'auto_headroom': False,
            'compressor': False, 'compression': TWITCH_AMOUNT, **SLIDER_DEFAULTS,
            'points': [{'id': f'p{i}', 'frequency': float(hz), 'gain': 0.0}
                       for i, hz in enumerate(INITIAL_FREQUENCIES)]}


def normalize_state(raw):
    if not isinstance(raw, dict):
        raise ValueError('EQ state must be an object')
    source = raw.get('points')
    if not isinstance(source, (list, tuple)) or not 2 <= len(source) <= MAX_POINTS:
        raise ValueError(f'EQ needs 2–{MAX_POINTS} points')
    points, identifiers = [], set()
    for item in source:
        identifier = str(item['id'])
        if not identifier or len(identifier) > 64 or identifier in identifiers:
            raise ValueError('Curve point identities must be unique')
        identifiers.add(identifier)
        hz, gain = _number(item['frequency'], 'Frequency'), _number(item['gain'], 'Gain')
        if not MIN_HZ <= hz <= MAX_HZ or not MIN_DB <= gain <= MAX_DB:
            raise ValueError('Curve point outside frequency/gain limits')
        points.append({'id': identifier, 'frequency': hz, 'gain': gain})
    points.sort(key=lambda point: point['frequency'])
    if points[0]['frequency'] != MIN_HZ or points[-1]['frequency'] != MAX_HZ:
        raise ValueError('Curve must include 20 Hz and 20 kHz endpoints')
    if any(math.log(right['frequency'] / left['frequency']) < MIN_LOG_GAP - 1e-9
           for left, right in zip(points, points[1:])):
        raise ValueError('Curve points are too close together')
    preamp = _number(raw.get('preamp_db', 0), 'Output gain')
    if not -18 <= preamp <= 6:
        raise ValueError('Output gain must be between -18 and +6 dB')
    compression = _number(raw.get('compression', 0), 'Compression')
    if not 0 <= compression <= 100:
        raise ValueError('Compression must be between 0 and 100')
    # Before the compressor had its own switch, an Amount of 0 meant off.
    compressor = raw.get('compressor', compression > 0)
    if 'compressor' not in raw and not compression:
        compression = TWITCH_AMOUNT
    if any(type(value) is not bool for value in (raw.get('enabled', True), raw.get('auto_headroom', False),
                                                 compressor)):
        raise ValueError('EQ switches must be boolean')
    sliders = {}
    defaults = SLIDER_DEFAULTS
    if 'comp_rms_ms' in raw and 'comp_knee_db' not in raw:
        # A ReaComp-era profile: its knee, and a release within Chrome's 1 s.
        release = _number(raw.get('comp_release_ms', 250), 'comp_release_ms')
        raw = {**raw, 'comp_release_ms': min(release, COMPRESSOR_RANGES['comp_release_ms'][1])}
        defaults = {**defaults, 'comp_knee_db': REACOMP_KNEE_DB}
    for key, (low, high) in SLIDER_RANGES.items():
        value = _number(raw.get(key, defaults[key]), key)
        if not low <= value <= high:
            raise ValueError(f'{key} must be between {low:g} and {high:g}')
        sliders[key] = value
    return {'enabled': raw.get('enabled', True), 'preamp_db': preamp,
            'auto_headroom': raw.get('auto_headroom', False), 'compressor': compressor, 'compression': compression,
            **sliders, 'points': points}


def space_active(state):
    return any(state[key] != SLIDER_DEFAULTS[key] for key in ('width_pct', 'depth_3d', 'room_pct'))


def space_headroom_db(state):
    """Worst case for Auto headroom: a hard-panned source through the widened side, plus the room."""
    if not space_active(state):
        return 0.0
    side = max(1.0, state['width_pct'] / 100) * (1 + 0.8 * state['depth_3d'] / 100)
    return 20 * math.log10((1 + side) / 2) + 20 * math.log10(1 + 0.5 * state['room_pct'] / 100)


def tone_shape(key, frequency):
    """Response of one character slider at 1 dB; frequency can be a float or an array."""
    f = np.asarray(frequency, dtype=float)
    if key == 'tilt_db':  # Pivot at 1 kHz; full amount by 50 Hz (negative) and 20 kHz (positive).
        return np.clip(np.log(f / 1000.0) / math.log(20.0), -1.0, 1.0)
    if key == 'warmth_db':  # Low shelf below ~150 Hz that stops boosting under ~35 Hz.
        shelf = 1.0 / (1.0 + (f / 150.0) ** 4)
        guard = (f / 35.0) ** 4 / (1.0 + (f / 35.0) ** 4)
        return shelf * guard
    if key == 'presence_db':  # Broad bell centred on 2.5 kHz, ~2 octaves wide.
        return np.exp(-0.5 * (np.log2(f / 2500.0) / 0.8) ** 2)
    if key == 'air_db':  # High shelf above ~8 kHz.
        return 1.0 / (1.0 + (8000.0 / f) ** 2)
    raise KeyError(key)


def has_tone(state):
    return any(state[key] for key in TONE_RANGES)


def tone_response(state, frequency):
    f = np.asarray(frequency, dtype=float)
    total = np.zeros_like(f)
    for key in TONE_RANGES:
        if state[key]:
            total = total + state[key] * tone_shape(key, f)
    return total


def tone_peak(key, amount):
    """The largest gain one slider applies in 20 Hz–20 kHz (tilt: at the treble end)."""
    if not amount:
        return 0.0
    return float(amount * np.max(tone_shape(key, np.geomspace(MIN_HZ, MAX_HZ, 512))))


def response_samples(state, count=512, tolerance=None):
    """Authored curve plus character sliders: exactly what Equalizer APO receives."""
    samples = sample_curve(state, count=count, tolerance=tolerance)
    if not has_tone(state):
        return samples
    tone = tone_response(state, [hz for hz, _ in samples])
    return [(hz, gain + float(extra)) for (hz, gain), extra in zip(samples, tone)]


def _slopes(x, y):
    """PCHIP tangents: curves pass through the curve's points without gain overshoot."""
    h = [b - a for a, b in zip(x, x[1:])]
    d = [(b - a) / width for a, b, width in zip(y, y[1:], h)]
    if len(x) == 2:
        return [d[0], d[0]]
    slopes = [0.0] * len(x)
    for i in range(1, len(x) - 1):
        if d[i - 1] * d[i] > 0:
            w1, w2 = 2 * h[i] + h[i - 1], h[i] + 2 * h[i - 1]
            slopes[i] = (w1 + w2) / (w1 / d[i - 1] + w2 / d[i])
    for index, a, b, da, db in ((0, h[0], h[1], d[0], d[1]), (-1, h[-1], h[-2], d[-1], d[-2])):
        slope = ((2 * a + b) * da - a * db) / (a + b)
        if slope * da <= 0:
            slope = 0.0
        elif da * db < 0 and abs(slope) > abs(3 * da):
            slope = 3 * da
        slopes[index] = slope
    return slopes


def sample_curve(state, count=512, tolerance=None):
    points = state['points']
    x, y = [math.log(p['frequency']) for p in points], [p['gain'] for p in points]
    slopes = _slopes(x, y)
    count = min(4096, max(2, int(count)))
    positions = set(x + [x[0] + (x[-1] - x[0]) * i / (count - 1) for i in range(count)])
    if tolerance is not None:
        tolerance = max(0.005, _number(tolerance, 'Curve tolerance'))
        # Bound linear interpolation error by max|second derivative| * step² / 8.
        # APO's log-linear points then follow the displayed cubic even in narrow edits.
        for i in range(len(x) - 1):
            h = x[i + 1] - x[i]
            a = 2*y[i] - 2*y[i + 1] + h*(slopes[i] + slopes[i + 1])
            b = -3*y[i] + 3*y[i + 1] - h*(2*slopes[i] + slopes[i + 1])
            curvature = max(abs(2*b), abs(6*a + 2*b))
            steps = max(1, min(128, math.ceil(math.sqrt(curvature / (8*tolerance)))))
            positions.update(x[i] + h*j/steps for j in range(1, steps))
    positions = sorted(positions)
    samples = []
    for position in positions:
        i = max(0, min(len(x) - 2, bisect.bisect_right(x, position) - 1))
        h = x[i + 1] - x[i]
        t = (position - x[i]) / h
        gain = ((2*t**3 - 3*t*t + 1) * y[i] + (t**3 - 2*t*t + t) * h * slopes[i]
                + (-2*t**3 + 3*t*t) * y[i + 1] + (t**3 - t*t) * h * slopes[i + 1])
        samples.append((math.exp(position), gain))
    return samples


def set_point(state, point_id, frequency, gain):
    updated = copy.deepcopy(state)
    points = updated['points']
    index = next(i for i, point in enumerate(points) if point['id'] == point_id)
    hz = _number(frequency, 'Frequency')
    if index == 0:
        hz = MIN_HZ
    elif index == len(points) - 1:
        hz = MAX_HZ
    else:
        hz = min(points[index + 1]['frequency'] / math.exp(MIN_LOG_GAP),
                 max(points[index - 1]['frequency'] * math.exp(MIN_LOG_GAP), hz))
    points[index].update(frequency=hz, gain=min(MAX_DB, max(MIN_DB, _number(gain, 'Gain'))))
    return normalize_state(updated)


def add_point(state, frequency, gain):
    if len(state['points']) >= MAX_POINTS:
        raise ValueError(f'Maximum {MAX_POINTS} points')
    updated = copy.deepcopy(state)
    updated['points'].append({'id': uuid.uuid4().hex[:12], 'frequency': _number(frequency, 'Frequency'),
                              'gain': min(MAX_DB, max(MIN_DB, _number(gain, 'Gain')))})
    return normalize_state(updated)


def remove_point(state, point_id):
    if point_id in (state['points'][0]['id'], state['points'][-1]['id']):
        raise ValueError('The 20 Hz and 20 kHz endpoints cannot be removed')
    updated = copy.deepcopy(state)
    updated['points'] = [p for p in updated['points'] if p['id'] != point_id]
    return normalize_state(updated)


def adjust_width(state, point_id, factor):
    """Spreads the neighbors in log frequency. Endpoints stay put and points never cross."""
    updated = copy.deepcopy(state)
    points = updated['points']
    index = next(i for i, p in enumerate(points) if p['id'] == point_id)
    factor = min(2.0, max(0.5, _number(factor, 'Width')))
    center = math.log(points[index]['frequency'])
    for neighbor in (index - 1, index + 1):
        if 0 < neighbor < len(points) - 1:
            target = center + (math.log(points[neighbor]['frequency']) - center) * factor
            lo = math.log(points[neighbor - 1]['frequency']) + MIN_LOG_GAP
            hi = math.log(points[neighbor + 1]['frequency']) - MIN_LOG_GAP
            points[neighbor]['frequency'] = math.exp(min(hi, max(lo, target)))
    return normalize_state(updated)


def effective_preamp(state):
    if not state['enabled']:
        return 0.0
    preamp = state['preamp_db']
    if state['auto_headroom']:
        peak = max(p['gain'] for p in state['points'])
        if has_tone(state):
            peak = max(peak, max(gain for _, gain in response_samples(state, count=1024)))
        preamp = min(preamp, -peak - space_headroom_db(state))
    return preamp
