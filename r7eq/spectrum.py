"""WASAPI loopback spectrum, only while visible. Never picks a default or capture device."""
from __future__ import annotations

import ctypes
import logging
import math
import os
import threading
import time
from typing import Callable

import numpy as np
from PySide6.QtCore import QObject, Signal, Slot

from .devices import endpoint_guid
from .model import MAX_HZ, MIN_HZ

SAMPLE_RATE = 48_000
BLOCK_FRAMES = 512  # 93.75 overlapping analysis frames/s, paced by captured audio.
FFT_FRAMES = 8_192
FLOOR_DB = -84.0

_WINDOW = np.hanning(FFT_FRAMES).astype(np.float32)
_FFT_HZ = np.fft.rfftfreq(FFT_FRAMES, 1 / SAMPLE_RATE)
_EDGES = np.geomspace(MIN_HZ, MAX_HZ, 257)
FREQUENCIES = np.sqrt(_EDGES[:-1] * _EDGES[1:])
_VALID_BINS = (_FFT_HZ >= MIN_HZ) & (_FFT_HZ <= MAX_HZ)
_BIN_BANDS = np.searchsorted(_EDGES, _FFT_HZ[_VALID_BINS], side='right') - 1
_BIN_BANDS = np.clip(_BIN_BANDS, 0, len(FREQUENCIES) - 1)


def spectrum_db(samples: np.ndarray) -> np.ndarray:
    """dBFS per log band. Channel powers add up, so out-of-phase stereo doesn't cancel."""
    samples = np.asarray(samples)
    if samples.ndim != 2 or samples.shape[0] != FFT_FRAMES or not 1 <= samples.shape[1] <= 32:
        raise ValueError('Spectrum requires 8192 frames and 1–32 interleaved channels')
    if not np.isfinite(samples).all():
        raise ValueError('Spectrum capture contained non-finite samples')
    fft = np.fft.rfft(samples * _WINDOW[:, None], axis=0)
    amplitude = np.sqrt(np.mean(np.abs(fft) ** 2, axis=1)) * (2 / _WINDOW.sum())
    bands = np.interp(FREQUENCIES, _FFT_HZ, amplitude)
    np.maximum.at(bands, _BIN_BANDS, amplitude[_VALID_BINS])
    return np.clip(20 * np.log10(np.maximum(bands, 10 ** (FLOOR_DB / 20))), FLOOR_DB, 0)


class SpectrumAnalyzer:
    def __init__(self) -> None:
        self._buffer: np.ndarray | None = None
        self._position = 0
        self._filled = 0
        self._last_frame: float | None = None
        self._levels: np.ndarray | None = None

    def push(self, block: np.ndarray, now: float) -> np.ndarray | None:
        block = np.asarray(block)
        if block.ndim != 2 or block.shape[0] != BLOCK_FRAMES or not 1 <= block.shape[1] <= 32:
            raise ValueError('Spectrum capture returned an unexpected frame count or channel layout')
        if self._buffer is None:
            self._buffer = np.empty((FFT_FRAMES, block.shape[1]), dtype=np.float32)
        elif self._buffer.shape[1] != block.shape[1]:
            raise ValueError('Spectrum capture changed channel count')
        if not np.isfinite(block).all():
            raise ValueError('Spectrum capture contained non-finite samples')
        self._buffer[self._position:self._position + BLOCK_FRAMES] = block
        self._position = (self._position + BLOCK_FRAMES) % FFT_FRAMES
        self._filled = min(FFT_FRAMES, self._filled + BLOCK_FRAMES)
        if self._filled < FFT_FRAMES:
            return None
        ordered = self._buffer if self._position == 0 else np.concatenate(
            (self._buffer[self._position:], self._buffer[:self._position]))
        current = spectrum_db(ordered)
        if self._levels is not None and self._last_frame is not None:
            elapsed = max(0.0, now - self._last_frame)
            decay = math.exp(-elapsed / 0.26)
            attack = math.exp(-elapsed / 0.07)
            previous = self._levels
            blend = np.where(current > previous, attack, decay)
            current = blend * previous + (1 - blend) * current
        self._levels = current
        self._last_frame = now
        return current


def _open_loopback(identifier: str):
    import soundcard as sc

    # get_microphone() accepts substring/fuzzy matches; never use it here.
    endpoint = next((device for device in sc.all_microphones(include_loopback=True)
                     if device.isloopback and device.id.casefold() == identifier.casefold()), None)
    if endpoint is None:
        raise RuntimeError('The selected device has no active WASAPI loopback')
    channels = endpoint.channels
    if not 1 <= channels <= 32:
        raise RuntimeError(f'Unsupported channel count: {channels}')
    return endpoint.recorder(samplerate=SAMPLE_RATE, channels=channels, blocksize=BLOCK_FRAMES)


class _Session:
    def __init__(self) -> None:
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.frame_pending = False
        self.thread: threading.Thread | None = None


class SpectrumMonitor(QObject):
    """start/stop/shutdown are GUI-thread calls; capture and FFT stay on the worker."""

    frameReady = Signal(object, object)  # FREQUENCIES, dBFS bands
    statusChanged = Signal(str, str)
    _delivered = Signal(object, object)
    _finished = Signal(object, object)

    def __init__(self, endpoint_id: str | None, parent: QObject | None = None,
                 capture_factory: Callable | None = None) -> None:
        super().__init__(parent)
        self.endpoint_id = endpoint_id
        self._capture_factory = capture_factory or _open_loopback
        self._session: _Session | None = None
        self._wanted = False
        self._closed = False
        self._delivered.connect(self._on_frame)
        self._finished.connect(self._on_finished)

    @property
    def active(self) -> bool:
        return self._session is not None and self._wanted

    def set_endpoint(self, endpoint_id: str | None) -> None:
        """None stops listening. A running analyzer restarts on the new device."""
        if endpoint_id == self.endpoint_id:
            return
        wanted = self._wanted
        self.stop()
        self.endpoint_id = endpoint_id
        if wanted:
            self.start()

    def start(self) -> None:
        if self._closed or self._wanted or not self.endpoint_id:
            return
        self._wanted = True
        if self._session is not None:  # A prior stream is still closing; never overlap endpoints.
            return
        try:
            endpoint_guid(self.endpoint_id)
            if self._capture_factory is _open_loopback and os.name == 'nt':
                # SoundCard initializes process-wide COM on import. Pin that import to the
                # GUI thread; each short-lived capture worker initializes its own COM.
                __import__('soundcard')
        except Exception as exc:
            self._wanted = False
            message = f'Spectrum unavailable: {exc}'
            logging.error('operation=spectrum,status=failed,message=%s', message)
            self.statusChanged.emit('error', message)
            return
        session = _Session()
        self._session = session
        session.thread = threading.Thread(target=self._capture, args=(session,),
                                          name='r7-EQ spectrum', daemon=True)
        session.thread.start()

    def stop(self) -> None:
        self._wanted = False
        if self._session is not None:
            self._session.stop.set()

    def shutdown(self) -> bool:
        self._closed = True
        self.stop()
        session = self._session
        if session is not None and session.thread is not None:
            session.thread.join(timeout=0.75)
            if session.thread.is_alive():
                logging.error('operation=spectrum,status=stop-timeout; WASAPI capture did not close within 750ms')
                self.statusChanged.emit('error', 'Spectrum capture did not close promptly; see r7-EQ log')
                return False
        return True

    def _capture(self, session: _Session) -> None:
        initialized = False
        error = None
        frames = 0
        started = time.perf_counter()
        try:
            if self._capture_factory is _open_loopback and os.name == 'nt':
                ole32 = ctypes.windll.ole32
                hr = ole32.CoInitializeEx(None, 0)  # MTA for SoundCard's WASAPI calls.
                if hr not in (0, 1):
                    raise OSError(f'WASAPI COM initialization failed (HRESULT {hr & 0xffffffff:#x})')
                initialized = True
            if session.stop.is_set():
                return
            with self._capture_factory(self.endpoint_id) as recorder:
                if session.stop.is_set():
                    return
                logging.info('operation=spectrum,status=active,endpoint_id=%s,channels=%s',
                             self.endpoint_id, getattr(recorder, 'channelmap', 'configured'))
                analyzer = SpectrumAnalyzer()
                while not session.stop.is_set():
                    block = recorder.record(BLOCK_FRAMES)
                    if session.stop.is_set():
                        break
                    levels = analyzer.push(block, time.monotonic())
                    if levels is not None:
                        frames += 1
                        with session.lock:
                            if session.frame_pending:
                                continue
                            session.frame_pending = True
                        self._delivered.emit(session, levels)
        except Exception as exc:
            error = str(exc)
            logging.exception('operation=spectrum,status=failed,endpoint_id=%s', self.endpoint_id)
        finally:
            if initialized:
                ctypes.windll.ole32.CoUninitialize()
            logging.info('operation=spectrum,status=stopped,frames=%d,dur_ms=%.1f',
                         frames, (time.perf_counter() - started) * 1000)
            self._finished.emit(session, error)

    @Slot(object, object)
    def _on_frame(self, session: _Session, levels: np.ndarray) -> None:
        with session.lock:
            session.frame_pending = False
        if session is self._session and self._wanted and not session.stop.is_set() and not self._closed:
            self.frameReady.emit(FREQUENCIES, levels)

    @Slot(object, object)
    def _on_finished(self, session: _Session, error: str | None) -> None:
        if session is not self._session:
            return
        self._session = None
        if error and not session.stop.is_set():
            self._wanted = False
            self.statusChanged.emit('error', f'Spectrum unavailable: {error}')
        elif self._wanted and not self._closed:
            self._wanted = False
            self.start()
