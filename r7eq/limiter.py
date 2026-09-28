"""R7 Limiter: the last step, so the output never goes above -0.3 dBFS."""
from pathlib import Path

from .model import effective_preamp, response_samples, space_active

LIBRARY = Path(__file__).resolve().parent.parent / 'plugins' / 'R7Limiter.dll'
CEILING_DB = -0.3


def needed(state):
    """Only when something in the chain can raise the level above the source's own peaks."""
    if state['compressor']:
        return True
    if not state['enabled']:
        return False
    if space_active(state):
        return True
    peak = max(0.0, max(gain for _, gain in response_samples(state, count=512)))
    return effective_preamp(state) + peak > 1e-6


def config_lines(state):
    if not needed(state):
        return []
    ceiling = (CEILING_DB + 3) / 3   # Plugin parameter: 0..1 maps to -3..0 dBFS.
    return [f'# Clip guard: lookahead peak limiter, ceiling {CEILING_DB:g} dBFS (2 ms)',
            f'VSTPlugin: Library "{LIBRARY}" "Ceiling" {ceiling:.6f}']
