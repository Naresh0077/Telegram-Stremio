"""Standalone helper: `python thumb_worker.py <url-or-file>` writes a JPEG of the FIRST frame of the video to stdout.

It deliberately runs as its own short-lived process (started by explorer_thumbs.py):
  * if FFmpeg / the file ever crashes or runs out of memory, only this helper dies, never the website;
  * all of its memory is returned to the system the moment it exits;
  * it imports nothing from the app, so it starts in a fraction of a second.
No seeking: it just decodes from the start of the file, so only the first few MB are read.
"""
import sys
from fractions import Fraction

try:
    import av
except Exception:                                   # pragma: no cover - depends on the environment
    av = None

THUMB_WIDTH = 480


def _luma(frame) -> float:
    plane = bytes(frame.reformat(width=64, height=36, format="yuvj420p").planes[0])
    sample = plane[::7] or b"\x00"
    return sum(sample) / len(sample)


def _encode_jpeg(frame, width: int = THUMB_WIDTH) -> bytes:
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
    return b"".join(bytes(p) for p in ctx.encode(small)) + b"".join(bytes(p) for p in ctx.encode(None))


def grab_frame(url: str, width: int = THUMB_WIDTH):
    """JPEG bytes of the first frame (or None). If the very first frame is black (a fade-in) the next ~3 seconds of the
    opening are checked for a real picture; with none, the first frame is used."""
    if av is None:
        return None
    options = {"reconnect": "1", "reconnect_streamed": "1", "reconnect_delay_max": "4", "probesize": "3000000", "analyzeduration": "3000000"}
    with av.open(url, mode="r", timeout=(25.0, 60.0), options=options) as c:
        vs = next((s for s in c.streams if s.type == "video"), None)
        if vs is None:
            return None
        try:
            vs.codec_context.thread_count = 1             # smallest memory / CPU footprint
        except Exception:
            pass
        first = None
        for n, frame in enumerate(c.decode(vs)):
            if first is None:
                first = frame
            if 20 < _luma(frame) < 235:
                first = frame
                break
            if n >= 75:
                break
        return _encode_jpeg(first, width) if first is not None else None


def main(argv) -> int:
    if av is None:
        sys.stderr.write("the 'av' package is not installed")
        return 3
    if len(argv) < 2:
        sys.stderr.write("usage: thumb_worker.py <url-or-file>")
        return 4
    try:
        av.logging.set_level(av.logging.PANIC)           # keep stderr for real errors only
        data = grab_frame(argv[1])
    except Exception as e:
        sys.stderr.write(f"{type(e).__name__}: {e}")
        return 1
    if not data:
        sys.stderr.write("no decodable video stream")
        return 2
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
