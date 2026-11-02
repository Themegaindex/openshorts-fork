import shutil
import subprocess
import threading

import pytest

import frame_sampler


class FakeCapture:
    """Frame i is the int i; counts seeks and decoded frames like cv2."""

    def __init__(self, frame_count, fail_at=()):
        self.frame_count = frame_count
        self.fail_at = set(fail_at)
        self.position = 0
        self.seeks = []
        self.decoded = 0

    def set(self, prop, value):
        assert prop == frame_sampler.POS_FRAMES
        self.seeks.append(int(value))
        self.position = int(value)
        return True

    def grab(self):
        if self.position >= self.frame_count:
            return False
        self.position += 1
        self.decoded += 1
        return True

    def read(self):
        if self.position >= self.frame_count or self.position in self.fail_at:
            self.position += 1
            return False, None
        frame = self.position
        self.position += 1
        self.decoded += 1
        return True, frame


def test_forward_pass_returns_the_same_frames_as_seeking_without_seeks():
    indices = [0, 7, 30, 31, 120, 299]
    legacy = list(frame_sampler.read_by_seek(FakeCapture(400), indices))
    cap = FakeCapture(400)
    assert list(frame_sampler.read_at(cap, indices)) == legacy == indices
    assert cap.seeks == []


def test_step_back_and_long_jump_fall_back_to_a_seek():
    cap = FakeCapture(2000)
    frames = list(frame_sampler.read_at(cap, [10, 5, 900, 905], max_forward=300))
    assert frames == [10, 5, 900, 905]
    assert cap.seeks == [5, 900]


def test_repeated_index_reuses_the_frame_and_failed_reads_are_none():
    cap = FakeCapture(50, fail_at={20})
    assert list(frame_sampler.read_at(cap, [3, 3, 20, 25, 60])) == [3, 3, None, 25, None]


def test_max_forward_zero_is_the_legacy_seek_per_sample_reader():
    cap = FakeCapture(100)
    assert list(frame_sampler.read_at(cap, [4, 9], max_forward=0)) == [4, 9]
    assert cap.seeks == [4, 9]


class StreamCapture:
    def __init__(self, frames, error=None):
        self.frames = list(frames)
        self.error = error
        self.reads = 0

    def read(self):
        self.reads += 1
        if self.frames:
            return True, self.frames.pop(0)
        if self.error is not None:
            raise self.error
        return False, None


def test_prefetcher_keeps_order_and_stays_at_the_end():
    reader = frame_sampler.FramePrefetcher(StreamCapture(range(25)), depth=4)
    frames = []
    while True:
        ok, frame = reader.read()
        if not ok:
            break
        frames.append(frame)
    assert frames == list(range(25))
    assert reader.read() == (False, None)
    assert reader.close() is True


def test_prefetcher_reraises_a_decoder_error_on_the_main_thread():
    reader = frame_sampler.FramePrefetcher(StreamCapture([1], error=RuntimeError("decode broke")), depth=2)
    assert reader.read() == (True, 1)
    with pytest.raises(RuntimeError, match="decode broke"):
        reader.read()
    assert reader.close() is True


def test_prefetcher_close_stops_a_blocked_worker():
    cap = StreamCapture(range(10_000))
    reader = frame_sampler.FramePrefetcher(cap, depth=2)
    assert reader.read() == (True, 0)
    assert reader.close() is True
    reads_after_close = cap.reads
    threading.Event().wait(0.3)
    assert cap.reads == reads_after_close


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_real_video_forward_pass_matches_seek_reads(tmp_path):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    video = tmp_path / "gop.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
        "-i", "testsrc2=size=160x90:rate=25", "-t", "8",
        "-c:v", "libx264", "-g", "50", "-pix_fmt", "yuv420p", str(video),
    ], check=True)
    indices = [0, 13, 37, 60, 61, 149, 199]

    def frames(reader, **kwargs):
        cap = cv2.VideoCapture(str(video))
        try:
            return list(reader(cap, indices, **kwargs))
        finally:
            cap.release()

    legacy = frames(frame_sampler.read_by_seek)
    forward = frames(frame_sampler.read_at)
    assert all(frame is not None for frame in legacy)
    for old, new in zip(legacy, forward):
        assert np.array_equal(old, new)
