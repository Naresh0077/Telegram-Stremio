"""Regression: the real media_streamer/prefetch_stream must return byte-exact data for ANY Range, including
ranges whose end byte lands exactly on a 1 MiB boundary (used to produce short bodies). Runs in its own process
because it patches ByteStreamer's Telegram layer."""
import os
import subprocess
import sys


def test_every_range_is_byte_exact():
    root = os.path.dirname(os.path.dirname(__file__))
    out = subprocess.run([sys.executable, "-W", "ignore", os.path.join(root, "tests", "range_check.py")],
                         cwd=root, capture_output=True, text=True, timeout=600).stdout
    assert "mismatches: 0" in out, out[-1500:]
