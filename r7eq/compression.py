"""R7Compressor.dll: Chrome's Web Audio compressor, the one Twitch's player compressor uses."""
from pathlib import Path
import math

LIBRARY = Path(__file__).resolve().parent.parent / 'plugins' / 'R7Compressor.dll'
MAX_LIFT_DB = 36.0
MAX_SLOT = 999
# Twitch's player compressor defaults.
TWITCH = {'comp_threshold_db': -50.0, 'comp_knee_db': 40.0, 'comp_ratio': 12.0,
          'comp_attack_ms': 0.0, 'comp_release_ms': 250.0, 'comp_dry_pct': 0.0, 'comp_bass_hz': 0.0}


def chrome_makeup_db(threshold, knee, ratio):
    """Chrome's makeup gain, (1 / curve(0 dBFS)) ** 0.6: about +12.4 dB at Twitch's settings."""
    linear_threshold = 10 ** (threshold / 20)

    def knee_curve(x, k):
        if x < linear_threshold:
            return x
        return linear_threshold + (1 - math.exp(-k * (x - linear_threshold))) / k

    db_x = threshold + knee
    x = 10 ** (db_x / 20)
    x2, db_x2 = (x * 1.001, 20 * math.log10(x * 1.001)) if x >= linear_threshold else (1, 0)
    low, high, k, slope = 0.1, 10000.0, 5.0, 1.0
    for _ in range(15):
        if x >= linear_threshold:
            slope = (20 * math.log10(knee_curve(x2, k)) - 20 * math.log10(knee_curve(x, k))) / (db_x2 - db_x)
        if slope < 1 / ratio:
            high = k
        else:
            low = k
        k = math.sqrt(low * high)
    knee_threshold = 10 ** (db_x / 20)
    if 1 < knee_threshold:
        full = knee_curve(1, k)
    else:
        full = 10 ** ((20 * math.log10(knee_curve(knee_threshold, k)) + (0 - db_x) / ratio) / 20)
    return 0.6 * -20 * math.log10(full)


def twitch_settings():
    """Amount is set to the boost Chrome itself adds."""
    lift = chrome_makeup_db(TWITCH['comp_threshold_db'], TWITCH['comp_knee_db'], TWITCH['comp_ratio'])
    return {**TWITCH, 'compressor': True, 'compression': round(100 * lift / MAX_LIFT_DB, 1)}


def parameters(state):
    """Plugin values, each 0..1. Bass is the detector's high-pass."""
    amount = float(state['compression'])
    if not math.isfinite(amount) or not 0 <= amount <= 100:
        raise ValueError('Compression must be between 0 and 100')
    lift = MAX_LIFT_DB * amount / 100
    return {
        'Thresh': (state['comp_threshold_db'] + 100) / 100, 'Knee': state['comp_knee_db'] / 40,
        'Ratio': (state['comp_ratio'] - 1) / 99, 'Attack': state['comp_attack_ms'] / 1000,
        'Release': state['comp_release_ms'] / 1000, 'Makeup': lift / 48,
        'Dry': state['comp_dry_pct'] / 100, 'Bass': state['comp_bass_hz'] / 1000,
    }, lift


def config_lines(state, slot):
    """slot keeps each device's meter reading separate."""
    if not state['compressor']:
        return []
    if type(slot) is not int or not 1 <= slot <= MAX_SLOT:
        raise ValueError('Meter slot must be 1–999')
    params, lift = parameters(state)
    # Which meter to publish to, then whether to publish at all.
    params['Slot'] = slot / 1000
    params['Meter'] = 1.0
    values = ' '.join(f'"{name}" {value:.10f}' for name, value in params.items())
    return [f'# Speaker compression: boost {lift:.2f} dB; threshold {state["comp_threshold_db"]:g} dBFS; '
            f'knee {state["comp_knee_db"]:g} dB; ratio {state["comp_ratio"]:g}:1; '
            f'attack {state["comp_attack_ms"]:g} ms; release {state["comp_release_ms"]:g} ms; '
            f'ignores bass below {state["comp_bass_hz"]:g} Hz; dry {state["comp_dry_pct"]:g}%',
            f'VSTPlugin: Library "{LIBRARY}" {values}']
