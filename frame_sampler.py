"""Read sampled frames in one forward pass instead of one seek per sample.

Port of upstream openshorts frame_sampler.read_at (MIT, commit a4c92ca).
cap.set(CAP_PROP_POS_FRAMES, i) + cap.read() decodes again from the previous
keyframe for every sample; grab() walks forward and decodes each frame at most
once. The frame returned for an index is the same.
"""
import queue
import threading

try:
    import cv2
    POS_FRAMES = cv2.CAP_PROP_POS_FRAMES
except Exception:  # lightweight CI without OpenCV; the enum value is 1
    POS_FRAMES = 1

# Past this many frames ahead one seek is cheaper than decoding forward
# (about one GOP of a libx264/NVENC cut).
MAX_FORWARD = 300


def read_by_seek(cap, indices):
    """Legacy reader: one seek + read per index."""
    for index in indices:
        cap.set(POS_FRAMES, int(index))
        ok, frame = cap.read()
        yield frame if ok else None


def read_at(cap, indices, max_forward=MAX_FORWARD):
    """Frame for each index, in order; None where a read fails.

    cap must be freshly opened. A step back or a jump beyond max_forward seeks
    like before; max_forward <= 0 is the legacy seek-per-sample reader. A
    repeated index yields the same array again (treat frames as read-only).
    """
    if max_forward <= 0:
        yield from read_by_seek(cap, indices)
        return
    position = 0
    last_index, last_frame = None, None
    for index in indices:
        index = int(index)
        if index == last_index:
            yield last_frame
            continue
        if index < position or index - position > max_forward:
            cap.set(POS_FRAMES, index)
            position = index
        frame = None
        while position < index:
            if not cap.grab():
                break
            position += 1
        if position == index:
            ok, frame = cap.read()
            position += 1
            if not ok:
                frame = None
        last_index, last_frame = index, frame
        yield frame


class FramePrefetcher:
    """Decode the next frames on a worker thread; same frames, same order.

    OpenCV releases the GIL while decoding and every read() returns a new
    array, so decoding overlaps detection, composition and the encoder pipe.
    """
    _END = object()

    def __init__(self, cap, depth):
        self._cap = cap
        self._queue = queue.Queue(maxsize=max(1, int(depth)))
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="reframe-decode", daemon=True)
        self._thread.start()

    def _put(self, item):
        while not self._stop.is_set():
            try:
                self._queue.put(item, timeout=0.2)
                return True
            except queue.Full:
                continue
        return False

    def _run(self):
        try:
            while not self._stop.is_set():
                ok, frame = self._cap.read()
                if not ok:
                    break
                if not self._put(frame):
                    return
        except BaseException as exc:
            self._put(exc)
            return
        self._put(self._END)

    def read(self):
        item = self._queue.get()
        if item is self._END:
            self._queue.put(item)          # stay at the end for repeated calls
            return False, None
        if isinstance(item, BaseException):
            raise item
        return True, item

    def close(self):
        """Stop the worker; True when it has exited (safe to release the capture)."""
        self._stop.set()
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass
        self._thread.join(timeout=5)
        return not self._thread.is_alive()
