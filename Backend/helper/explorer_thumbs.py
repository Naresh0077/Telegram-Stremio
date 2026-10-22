"""Thumbnails for videos that Telegram has no thumbnail for (typically MKV files sent as documents).

The FIRST frame of the video is decoded through the app's own /dl link (only the first few MB are read) and saved as a small
JPEG in a disk cache. The decoding happens in a separate short-lived helper process (thumb_worker.py) that runs at low priority
with a memory cap, one at a time, and only while the machine has free memory: a crash or a huge video can never take the website
down, which matters on small servers (Oracle free tier etc.). Uses PyAV (package `av`, its wheel bundles FFmpeg's libraries).
If `av` is missing everything here quietly switches itself off.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import os
import sys
import tempfile
import time
from typing import Dict, Optional, Tuple

from Backend.logger import LOGGER

AVAILABLE = importlib.util.find_spec("av") is not None          # checked without importing it (no memory cost in the web app)
ENABLED = AVAILABLE and os.getenv("EXPLORER_THUMBS", "true").lower() == "true"
CACHE_DIR = os.getenv("EXPLORER_THUMB_DIR") or os.path.join(tempfile.gettempdir(), "explorer_thumbs")
CACHE_MAX_BYTES = int(os.getenv("EXPLORER_THUMB_CACHE_MB", "300")) * 1024 * 1024
JOBS = max(1, int(os.getenv("EXPLORER_THUMB_JOBS", "1")))
MAX_MB = int(os.getenv("EXPLORER_THUMB_MAX_MB", "450"))         # memory cap of the helper process
MIN_FREE_MB = int(os.getenv("EXPLORER_THUMB_MIN_FREE_MB", "200"))  # do not start a job below this much free RAM
MAX_QUEUE = 40
JOB_TIMEOUT = 90
FAIL_TTL = 600                                                  # a failed video is retried after 10 minutes
LOW_MEM_TTL = 60
WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "thumb_worker.py")

_sem: Optional[asyncio.Semaphore] = None
_pending: set = set()
_failed: Dict[str, float] = {}
errors: Dict[str, str] = {}                                     # last failure reason per file (shown by the diagnose link)
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


# ----------------------------------------------------------------------------------------------- the helper process
def free_mb() -> Optional[int]:
    """Memory still available to start new work (MB), or None if the system can't tell (non-Linux)."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError):
        pass
    return None


def _limits() -> None:                                          # runs in the helper process only, before it starts
    try:
        os.nice(15)                                             # the website always wins over thumbnail work
    except Exception:
        pass
    try:
        import resource
        cap = MAX_MB * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_DATA, (cap, cap))    # heap cap: a runaway job fails with MemoryError, not an OOM kill
    except Exception:
        pass


async def run_worker(url: str, timeout: float = JOB_TIMEOUT) -> Tuple[Optional[bytes], Optional[str]]:
    """Decode the first frame in a separate process. Returns (jpeg, None) or (None, reason)."""
    try:
        proc = await asyncio.create_subprocess_exec(sys.executable, WORKER, url, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE, preexec_fn=_limits)
    except Exception as e:
        return None, f"could not start the helper process: {type(e).__name__}: {e}"
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        return None, f"timed out after {int(timeout)} s (Telegram or the server was too slow)"
    if proc.returncode == 0 and out[:2] == b"\xff\xd8":
        return out, None
    lines = (err or b"").decode("utf-8", "replace").strip().splitlines()
    msg = (lines[-1] if lines else f"exit code {proc.returncode}")[:300]
    if proc.returncode is not None and proc.returncode < 0:
        msg = f"helper process crashed (signal {-proc.returncode}) - the website was not affected. {msg}".strip()
    return None, msg


def _version() -> Optional[str]:
    try:
        from importlib.metadata import version
        return version("av")
    except Exception:
        return None


async def diagnose(url: str) -> dict:
    """Try to make a thumbnail right now and report exactly what happened (for the 'Diagnose' link)."""
    out = {"av_installed": AVAILABLE, "av_version": _version(), "enabled": ENABLED, "free_memory_mb": free_mb(), "memory_cap_mb": MAX_MB}
    if not AVAILABLE:
        out["error"] = "The 'av' package is not installed (restart the app so it installs; see EXPLORER.md)"
        return out
    t = time.time()
    data, err = await run_worker(url)
    out.update(ok=bool(data), jpeg_bytes=len(data or b""), seconds=round(time.time() - t, 2))
    if err:
        out["error"] = err
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
    """Queue a background generation (idempotent). Returns 'ready' | 'pending' | 'busy' | 'failed'."""
    global _sem
    st = state(fid)
    if st != "idle":
        return st
    if not ENABLED:
        return "failed"
    if len(_pending) >= MAX_QUEUE:
        return "busy"                                           # the page simply asks again a few seconds later
    if _sem is None:
        _sem = asyncio.Semaphore(JOBS)
    _pending.add(fid)

    async def run():
        try:
            async with _sem:
                free = free_mb()
                if free is not None and free < MIN_FREE_MB:     # small server under pressure: skip, try again in a minute
                    errors[fid] = f"not enough free memory right now ({free} MB free, {MIN_FREE_MB} MB needed)"
                    _failed[fid] = time.time() + LOW_MEM_TTL
                    return
                data, err = await run_worker(url)
                await asyncio.sleep(0.3)                        # breathing space between jobs
            if data:
                store(fid, data)
                errors.pop(fid, None)
            else:
                errors[fid] = err or "unknown error"
                _failed[fid] = time.time() + FAIL_TTL
                LOGGER.warning(f"[Explorer] thumbnail generation failed for {fid}: {errors[fid]}")
        except Exception as e:
            errors[fid] = f"{type(e).__name__}: {e}"
            _failed[fid] = time.time() + FAIL_TTL
            LOGGER.warning(f"[Explorer] thumbnail generation failed for {fid}: {errors[fid]}")
        finally:
            _pending.discard(fid)
            if len(_failed) > 5000:
                _failed.clear()

    asyncio.get_running_loop().create_task(run())
    return "pending"
