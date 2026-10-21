"""Thumbnails for videos that Telegram has no thumbnail for (typically MKV files sent as documents).

A single frame is decoded straight from the video through the app's own /dl link (HTTP range requests, so only a few MB are
read, never the whole file) and saved as a small JPEG in a disk cache. Uses PyAV, whose wheel bundles FFmpeg's libraries:
nothing has to be installed on the host. If the `av` package is missing everything here quietly switches itself off.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
import time
from fractions import Fraction
from typing import Dict, Optional

from Backend.logger import LOGGER

try:                                           # optional dependency
    import av
except Exception:                              # pragma: no cover - depends on the environment
    av = None

AVAILABLE = av is not None
ENABLED = AVAILABLE and os.getenv("EXPLORER_THUMBS", "true").lower() == "true"
THUMB_WIDTH = 480
CACHE_DIR = os.getenv("EXPLORER_THUMB_DIR") or os.path.join(tempfile.gettempdir(), "explorer_thumbs")
CACHE_MAX_BYTES = int(os.getenv("EXPLORER_THUMB_CACHE_MB", "300")) * 1024 * 1024
JOBS = max(1, int(os.getenv("EXPLORER_THUMB_JOBS", "1")))
JOB_TIMEOUT = 120
FAIL_TTL = 6 * 3600

_sem: Optional[asyncio.Semaphore] = None
_pending: set = set()
_failed: Dict[str, float] = {}
_writes = 0


# ----------------------------------------------------------------------------------------------- disk cache
def _path(fid: str) -> str:
    return os.path.join(CACHE_DIR, hashlib.sha1(fid.encode()).hexdigest() + ".jpg")


def cached(fid: str) -> Optional[bytes]:
    try:
        with open(_path(fid), "rb") as f:
            data = f.read()
        os.utime(_path(fid))                                    # LRU: touch on use
        return data or None
    except OSError:
        return None


def store(fid: str, data: bytes) -> None:
    global _writes
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = _path(fid) + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, _path(fid))
        _writes += 1
        if _writes % 25 == 0:
            _prune()
    except OSError as e:
        LOGGER.debug(f"[Explorer] thumbnail cache write failed: {e}")


def _prune() -> None:
    try:
        files = [(e.stat().st_mtime, e.stat().st_size, e.path) for e in os.scandir(CACHE_DIR) if e.name.endswith(".jpg")]
        total = sum(s for _, s, _ in files)
        for _, size, path in sorted(files):
            if total <= CACHE_MAX_BYTES * 0.9:
                break
            os.remove(path)
            total -= size
    except OSError:
        pass


# ----------------------------------------------------------------------------------------------- frame extraction
def _encode_jpeg(frame, width: int) -> bytes:
    sar = float(frame.sample_aspect_ratio) if getattr(frame, "sample_aspect_ratio", None) else 1.0
    if not sar or sar <= 0:
        sar = 1.0
    w = min(width, frame.width) if frame.width else width
    w -= w % 2
    h = max(2, int(round(w * frame.height / (frame.width * sar))))
    h -= h % 2
    small = frame.reformat(width=w, height=h, format="yuvj420p")
    ctx = av.CodecContext.create("mjpeg", "w")
    ctx.width, ctx.height, ctx.pix_fmt = w, h, "yuvj420p"
    ctx.time_base = Fraction(1, 25)
    ctx.options = {"qmin": "4", "qmax": "6", "huffman": "optimal"}
    out = b"".join(bytes(p) for p in ctx.encode(small)) + b"".join(bytes(p) for p in ctx.encode(None))
    return out


def _luma(frame) -> float:
    plane = bytes(frame.reformat(width=64, height=36, format="yuvj420p").planes[0])
    sample = plane[::7] or b"\x00"
    return sum(sample) / len(sample)


def grab_frame(url: str, hint_seconds: float = 0.0, width: int = THUMB_WIDTH) -> Optional[bytes]:
    """Blocking: returns JPEG bytes of a representative frame, or None. Run it in a worker thread."""
    if av is None:
        return None
    options = {"reconnect": "1", "reconnect_streamed": "1", "reconnect_delay_max": "4", "probesize": "8000000", "analyzeduration": "8000000"}
    with av.open(url, mode="r", timeout=(10.0, 30.0), options=options) as c:
        vs = next((s for s in c.streams if s.type == "video"), None)
        if vs is None:
            return None
        dur = (c.duration / av.time_base) if c.duration else float(hint_seconds or 0)
        if dur and dur > 40:
            targets = [dur * 0.10, dur * 0.25, dur * 0.45]
        elif dur and dur > 4:
            targets = [dur * 0.3, dur * 0.6]
        else:
            targets = [min(5.0, dur * 0.5) if dur else 0.0, 0.0]
        best = None
        for t in targets:
            try:
                c.seek(int(max(0.0, t) * av.time_base), backward=True, any_frame=False)
            except Exception:
                try:
                    c.seek(0)
                except Exception:
                    pass
            frame = None
            try:
                for frame in c.decode(vs):
                    break
            except Exception:
                frame = None
            if frame is None:
                continue
            best = frame
            lum = _luma(frame)
            if 20 < lum < 235:                       # skip black / blown-out frames (fade-ins, studio logos)
                break
        return _encode_jpeg(best, width) if best is not None else None


# ----------------------------------------------------------------------------------------------- async queue
def state(fid: str) -> str:
    """'ready' | 'pending' | 'failed' | 'idle'"""
    if cached(fid) is not None:
        return "ready"
    if fid in _pending:
        return "pending"
    if _failed.get(fid, 0) > time.time():
        return "failed"
    return "idle"


def request(fid: str, url: str, hint_seconds: float = 0.0) -> str:
    """Queue a background generation (idempotent). Returns the resulting state."""
    global _sem
    st = state(fid)
    if st != "idle":
        return st
    if not ENABLED:
        return "failed"
    if _sem is None:
        _sem = asyncio.Semaphore(JOBS)
    _pending.add(fid)

    async def run():
        try:
            async with _sem:
                loop = asyncio.get_running_loop()
                data = await asyncio.wait_for(loop.run_in_executor(None, grab_frame, url, hint_seconds), JOB_TIMEOUT)
            if data:
                store(fid, data)
            else:
                _failed[fid] = time.time() + FAIL_TTL
        except Exception as e:
            LOGGER.info(f"[Explorer] thumbnail generation failed for {fid}: {e}")
            _failed[fid] = time.time() + FAIL_TTL
        finally:
            _pending.discard(fid)
            if len(_failed) > 5000:
                _failed.clear()

    asyncio.get_running_loop().create_task(run())
    return "pending"
