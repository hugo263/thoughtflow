"""Bounded, read-only Zstandard decoding without a mandatory Python package."""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Iterator

MAX_OUTPUT_BYTES = 256 * 1024 * 1024
MAX_INPUT_BYTES = 512 * 1024 * 1024
MAX_WINDOW_BYTES = 128 * 1024 * 1024
CHUNK = 64 * 1024


class CompressionError(Exception):
    def __init__(self, code: str, public_message: str):
        super().__init__(public_message)
        self.code = code
        self.public_message = public_message


class _InBuffer(ctypes.Structure):
    _fields_ = [("src", ctypes.c_void_p), ("size", ctypes.c_size_t), ("pos", ctypes.c_size_t)]


class _OutBuffer(ctypes.Structure):
    _fields_ = [("dst", ctypes.c_void_p), ("size", ctypes.c_size_t), ("pos", ctypes.c_size_t)]


_discovery_lock = threading.Lock()
_discovered = False
_library = None
_binary = None


def _discover() -> None:
    global _discovered, _library, _binary
    with _discovery_lock:
        if _discovered:
            return
        candidates = [ctypes.util.find_library("zstd")]
        if os.name == "posix":
            candidates += ["/opt/homebrew/lib/libzstd.dylib", "/usr/local/lib/libzstd.dylib", "libzstd.so.1"]
        elif os.name == "nt":
            candidates += ["libzstd.dll", "zstd.dll"]
        for name in candidates:
            if not name:
                continue
            try:
                lib = ctypes.CDLL(name)
                lib.ZSTD_createDStream.argtypes = []
                lib.ZSTD_createDStream.restype = ctypes.c_void_p
                lib.ZSTD_freeDStream.argtypes = [ctypes.c_void_p]
                lib.ZSTD_freeDStream.restype = ctypes.c_size_t
                lib.ZSTD_initDStream.argtypes = [ctypes.c_void_p]
                lib.ZSTD_initDStream.restype = ctypes.c_size_t
                lib.ZSTD_decompressStream.argtypes = [ctypes.c_void_p, ctypes.POINTER(_OutBuffer), ctypes.POINTER(_InBuffer)]
                lib.ZSTD_decompressStream.restype = ctypes.c_size_t
                lib.ZSTD_isError.argtypes = [ctypes.c_size_t]
                lib.ZSTD_isError.restype = ctypes.c_uint
                # Require this API so malformed frames cannot request unlimited memory.
                lib.ZSTD_DCtx_setMaxWindowSize.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
                lib.ZSTD_DCtx_setMaxWindowSize.restype = ctypes.c_size_t
                _library = lib
                break
            except (OSError, AttributeError):
                continue
        if _library is None:
            _binary = shutil.which("zstd")
        _discovered = True


def available() -> bool:
    _discover()
    return _library is not None or bool(_binary)


def native_available() -> bool:
    """Native decoding allows safe bounded metadata sniffing without processes."""
    _discover()
    return _library is not None


def availability_reason() -> str:
    return "Zstandard support is unavailable. Install zstd and restart Thoughtflow."


def _bad_file() -> CompressionError:
    return CompressionError("zstd_invalid", "The compressed conversation cannot be fully decoded. It may be damaged or exceed the decompression memory limit.")


def _over_limit() -> CompressionError:
    return CompressionError("zstd_too_large", "The decompressed conversation exceeds 256 MiB. It was not read in full; use another viewer.")


def _library_chunks(path: Path, lib, max_input_bytes: int, timeout_seconds: float) -> Iterator[bytes]:
    stream = lib.ZSTD_createDStream()
    if not stream:
        raise CompressionError("zstd_memory", "Cannot allocate decompression resources. Try again later.")
    try:
        result = lib.ZSTD_initDStream(stream)
        if lib.ZSTD_isError(result):
            raise _bad_file()
        result = lib.ZSTD_DCtx_setMaxWindowSize(stream, MAX_WINDOW_BYTES)
        if lib.ZSTD_isError(result):
            raise _bad_file()
        total, total_input, remaining, saw_input, deadline = 0, 0, 1, False, time.monotonic() + timeout_seconds
        with path.open("rb") as file:
            while True:
                if time.monotonic() >= deadline:
                    raise CompressionError("zstd_timeout", "Decompression timed out. History was not read in full; try again later.")
                data = file.read(CHUNK)
                if not data:
                    break
                total_input += len(data)
                if total_input > max_input_bytes:
                    raise CompressionError("zstd_too_large", "The compressed file exceeds the input limit. It was not read in full.")
                saw_input = True
                source = ctypes.create_string_buffer(data, len(data))
                incoming = _InBuffer(ctypes.cast(source, ctypes.c_void_p), len(data), 0)
                while incoming.pos < incoming.size:
                    if time.monotonic() >= deadline:
                        raise CompressionError("zstd_timeout", "Decompression timed out. History was not read in full; try again later.")
                    old_position = incoming.pos
                    buffer = ctypes.create_string_buffer(CHUNK)
                    outgoing = _OutBuffer(ctypes.cast(buffer, ctypes.c_void_p), CHUNK, 0)
                    remaining = lib.ZSTD_decompressStream(stream, ctypes.byref(outgoing), ctypes.byref(incoming))
                    if lib.ZSTD_isError(remaining):
                        raise _bad_file()
                    if incoming.pos == old_position and outgoing.pos == 0:
                        raise _bad_file()
                    if outgoing.pos:
                        total += outgoing.pos
                        if total > MAX_OUTPUT_BYTES:
                            raise _over_limit()
                        yield buffer.raw[:outgoing.pos]
        if not saw_input or remaining != 0:
            raise _bad_file()
    finally:
        lib.ZSTD_freeDStream(stream)


def _binary_chunks(path: Path, executable: str, timeout_seconds: float = 90) -> Iterator[bytes]:
    """Fallback starts one fixed-argument decoder only for the selected file."""
    try:
        proc = subprocess.Popen([executable, "-q", "-d", "-c", "-M128M", "--", str(path)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        raise CompressionError("zstd_unavailable", availability_reason()) from None
    out: queue.Queue = queue.Queue(maxsize=4)
    stop = threading.Event()

    def pump() -> None:
        try:
            while not stop.is_set():
                data = proc.stdout.read(CHUNK)
                while not stop.is_set():
                    try:
                        out.put(data, timeout=.5)
                        break
                    except queue.Full:
                        continue
                if not data:
                    break
        except OSError:
            try:
                out.put(None, timeout=.5)
            except queue.Full:
                pass

    thread = threading.Thread(target=pump, daemon=True)
    thread.start()
    deadline, total = time.monotonic() + timeout_seconds, 0
    try:
        while True:
            wait = deadline - time.monotonic()
            if wait <= 0:
                raise CompressionError("zstd_timeout", "Decompression timed out. History was not read in full; try again later.")
            try:
                data = out.get(timeout=wait)
            except queue.Empty:
                raise CompressionError("zstd_timeout", "Decompression timed out. History was not read in full; try again later.") from None
            if data is None:
                raise _bad_file()
            if data == b"":
                try:
                    code = proc.wait(timeout=max(.1, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    raise CompressionError("zstd_timeout", "Decompression timed out. History was not read in full; try again later.") from None
                if code != 0:
                    raise _bad_file()
                break
            total += len(data)
            if total > MAX_OUTPUT_BYTES:
                raise _over_limit()
            yield data
    finally:
        stop.set()
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        if proc.stdout:
            proc.stdout.close()
        thread.join(timeout=2)


def iter_chunks(path: Path, *, max_input_bytes: int | None = None,
                timeout_seconds: float = 90, check_total_size: bool = True) -> Iterator[bytes]:
    _discover()
    if max_input_bytes is None:
        max_input_bytes = MAX_INPUT_BYTES
    if (check_total_size or _library is None) and path.stat().st_size > max_input_bytes:
        raise CompressionError("zstd_too_large", "The compressed file exceeds the input limit. It was not read in full.")
    if _library is not None:
        yield from _library_chunks(path, _library, max_input_bytes, timeout_seconds)
    elif _binary:
        yield from _binary_chunks(path, _binary, timeout_seconds)
    else:
        raise CompressionError("zstd_unavailable", availability_reason())
