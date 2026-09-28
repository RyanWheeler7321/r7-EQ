"""Reads the compressor's meter while the window is visible. Each device has its own slot."""
from __future__ import annotations

import ctypes
import logging
import math
import os
import struct
from typing import Callable

_FORMAT = struct.Struct('<IIIIQQffffII')
_SEQUENCE = struct.Struct('<I')
FILE_MAP_READ = 0x0004
EVENT_MODIFY_STATE = 0x0002
MAGIC = 0x52374D31
VERSION = 1
MAX_AGE_MS = 500


def mapping_names(slot: int) -> tuple[str, str]:
    return f'Global\\R7EQ.Dynboost.{slot}.v1', f'Global\\R7EQ.Dynboost.Request.{slot}.v1'


class _WindowsMapping:
    def __init__(self, slot: int) -> None:
        mapping_name, request_name = mapping_names(slot)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenFileMappingW.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p)
        kernel.OpenFileMappingW.restype = ctypes.c_void_p
        kernel.MapViewOfFile.argtypes = (ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                         ctypes.c_uint32, ctypes.c_size_t)
        kernel.MapViewOfFile.restype = ctypes.c_void_p
        kernel.OpenEventW.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p)
        kernel.OpenEventW.restype = ctypes.c_void_p
        kernel.SetEvent.argtypes = (ctypes.c_void_p,)
        kernel.SetEvent.restype = ctypes.c_int
        kernel.UnmapViewOfFile.argtypes = (ctypes.c_void_p,)
        kernel.UnmapViewOfFile.restype = ctypes.c_int
        kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel.CloseHandle.restype = ctypes.c_int
        handle = kernel.OpenFileMappingW(FILE_MAP_READ, False, mapping_name)
        if not handle:
            raise OSError(ctypes.get_last_error(), 'OpenFileMappingW failed')
        address = kernel.MapViewOfFile(handle, FILE_MAP_READ, 0, 0, _FORMAT.size)
        if not address:
            error = ctypes.get_last_error()
            kernel.CloseHandle(handle)
            raise OSError(error, 'MapViewOfFile failed')
        request_handle = kernel.OpenEventW(EVENT_MODIFY_STATE, False, request_name)
        if not request_handle:
            error = ctypes.get_last_error()
            kernel.UnmapViewOfFile(address)
            kernel.CloseHandle(handle)
            raise OSError(error, 'OpenEventW failed')
        self._kernel = kernel
        self._handle = handle
        self._address = address
        self._request_handle = request_handle

    def request(self) -> None:
        if not self._kernel.SetEvent(self._request_handle):
            raise OSError(ctypes.get_last_error(), 'SetEvent failed')

    def read(self, offset: int, size: int) -> bytes:
        return ctypes.string_at(self._address + offset, size)

    def close(self) -> None:
        if self._request_handle:
            self._kernel.CloseHandle(self._request_handle)
            self._request_handle = 0
        if self._address:
            self._kernel.UnmapViewOfFile(self._address)
            self._address = 0
        if self._handle:
            self._kernel.CloseHandle(self._handle)
            self._handle = 0


if os.name == 'nt':
    _get_tick_count = ctypes.windll.kernel32.GetTickCount64
    _get_tick_count.restype = ctypes.c_uint64


def _tick_count_ms() -> int:
    return _get_tick_count()


class MeterReader:
    def __init__(self, opener: Callable | None = None, clock: Callable[[], int] | None = None) -> None:
        self._opener = opener or _WindowsMapping
        self._clock = clock or _tick_count_ms
        self._mapping = None
        self._status: str | None = None
        self.slot = 0

    def set_slot(self, slot: int) -> None:
        if slot != self.slot:
            self.close()
            self.slot = slot

    def _report(self, status: str) -> None:
        if status != self._status:
            logging.info('operation=compressor_meter,status=%s', status)
            self._status = status

    def read(self, expected_max_db: float) -> tuple[float, float] | None:
        """(actual, max) dB, or None; a missing map is retried next poll."""
        if expected_max_db <= 0 or not self.slot:
            self.close()
            return None
        if self._mapping is None:
            if os.name != 'nt' and self._opener is _WindowsMapping:
                self._report('unavailable')
                return None
            try:
                self._mapping = self._opener(self.slot)
            except OSError:
                self._report('unavailable')
                return None
        try:
            self._mapping.request()
            first = _SEQUENCE.unpack(self._mapping.read(8, 4))[0]
            if first & 1:
                return None
            snapshot = _FORMAT.unpack(self._mapping.read(0, _FORMAT.size))
            last = _SEQUENCE.unpack(self._mapping.read(8, 4))[0]
            (magic, version, sequence, writer_pid, instance_id, timestamp_ms,
             actual_db, maximum_db, input_peak, output_peak, signal_present, _) = snapshot
            if first != last or sequence != first or last & 1:
                return None
            age = self._clock() - timestamp_ms
            if (magic != MAGIC or version != VERSION or writer_pid == 0 or instance_id == 0
                    or signal_present != 1 or not all(map(math.isfinite,
                                                          (actual_db, maximum_db, input_peak, output_peak)))
                    or not 0 <= age <= MAX_AGE_MS or abs(maximum_db - expected_max_db) > .02):
                return None
            self._report('active')
            return actual_db, maximum_db
        except (OSError, ValueError, struct.error):
            self.close()
            self._report('unavailable')
            return None

    def close(self) -> None:
        if self._mapping is not None:
            self._mapping.close()
            self._mapping = None
