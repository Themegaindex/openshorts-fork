import os
import sys

# Make the repo root importable so tests can import the app modules directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


import pytest


@pytest.fixture(autouse=True)
def _pin_cpu_video_encoder(monkeypatch):
    """Tests never touch the GPU, even on a dev box where NVENC works."""
    monkeypatch.setenv("VIDEO_ENCODER", "x264")
    monkeypatch.setenv("VIDEO_HWDECODE", "off")
    import video_encoding
    video_encoding.reset_state()
    yield
    video_encoding.reset_state()
