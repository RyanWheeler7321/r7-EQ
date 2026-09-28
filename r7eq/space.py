"""R7 Space: width with centred bass, speaker 3D and a small room, via the native R7Space VST2."""
from pathlib import Path

from .model import space_active

LIBRARY = Path(__file__).resolve().parent.parent / 'plugins' / 'R7Space.dll'


def config_lines(state):
    """Nothing is inserted at neutral settings, so Space costs nothing until it is used."""
    if not space_active(state):
        return []
    params = {'Width': state['width_pct'] / 200, '3D': state['depth_3d'] / 100,
              'Room': state['room_pct'] / 100, 'Size': state['room_size'] / 100}
    values = ' '.join(f'"{name}" {value:.6f}' for name, value in params.items())
    return [f'# Space: width {state["width_pct"]:g}%, 3D {state["depth_3d"]:g}%, '
            f'room {state["room_pct"]:g}% (size {state["room_size"]:g}%)',
            f'VSTPlugin: Library "{LIBRARY}" {values}']
