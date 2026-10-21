"""Thumbnails for videos that Telegram has no thumbnail for (typically MKV files sent as documents).

The FIRST frame of the video is decoded straight from the video through the app's own /dl link: no seeking, only the first
few MB of the file are read, never the whole file. It is saved as a small JPEG in a disk cache. Uses PyAV, whose wheel bundles FFmpeg's libraries:
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
JOBS = max(1, int(os.getenv("EXPLORER_THUMB_JOBS", "3")))
JOB_TIMEOUT = 90
FAIL_TTL = 600                                   # a failed video is retried after 10 minutes

_sem: Optional[asyncio.Semaphore] = None
_pending: set = set()
_failed: Dict[str, float] = {}
errors: Dict[str, str] = {}                      # last failure reason per file (shown by the diagnose link)
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
    """Blocking: JPEG bytes of the first frame of the video (or None). Run it in a worker thread.
    No seeking: it just decodes from the start of the file. If the very first frame is black (fade-in) it looks at the next
    ~3 seconds of the opening for a real picture, and uses the first frame if there is none."""
    if av is None:
        return None
    options = {"reconnect": "1", "reconnect_streamed": "1", "reconnect_delay_max": "4", "probesize": "3000000", "analyzeduration": "3000000"}
    with av.open(url, mode="r", timeout=(25.0, 60.0), options=options) as c:
        vs = next((s for s in c.streams if s.type == "video"), None)
        if vs is None:
            return None
        first = None
        for n, frame in enumerate(c.decode(vs)):
            if first is None:
                first = frame
            if 20 < _luma(frame) < 235 or n >= 75:
                first = frame if 20 < _luma(frame) < 235 else first
                break
        return _encode_jpeg(first, width) if first is not None else None


def diagnose(url: str) -> dict:
    """Blocking: try to make a thumbnail and report exactly what happened (for the 'Diagnose' link)."""
    out = {"av_installed": AVAILABLE, "av_version": getattr(av, "__version__", None), "enabled": ENABLED}
    if not AVAILABLE:
        out["error"] = "The 'av' package is not installed (restart the app so it installs; see EXPLORER.md)"
        return out
    t = time.time()
    try:
        data = grab_frame(url)
        out.update(ok=bool(data), jpeg_bytes=len(data or b""), seconds=round(time.time() - t, 2))
        if not data:
            out["error"] = "The file has no video stream that could be decoded"
    except Exception as e:
        out.update(ok=False, seconds=round(time.time() - t, 2), error=f"{type(e).__name__}: {e}")
    return out


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
                errors.pop(fid, None)
            else:
                errors[fid] = "no decodable video stream"
                _failed[fid] = time.time() + FAIL_TTL
        except Exception as e:
            errors[fid] = f"{type(e).__name__}: {e}"
            LOGGER.warning(f"[Explorer] thumbnail generation failed for {fid}: {errors[fid]}")
            _failed[fid] = time.time() + FAIL_TTL
        finally:
            _pending.discard(fid)
            if len(_failed) > 5000:
                _failed.clear()

    asyncio.get_running_loop().create_task(run())
    return "pending"
