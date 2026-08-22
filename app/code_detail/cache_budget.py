"""Small shared byte-budget primitive for rebuildable process caches."""

import threading


class ByteBudget(object):
    """Thread-safe accounting shared by caches in one runtime process.

    The primitive deliberately only reserves bytes.  Cache owners remain
    responsible for deciding which LRU entry to evict before asking for a
    reservation, so unrelated cache locks are never held by this object.
    """

    def __init__(self, max_bytes):
        self.max_bytes = max(0, int(max_bytes))
        self._bytes = 0
        self._lock = threading.Lock()

    def try_acquire(self, size):
        size = max(0, int(size))
        with self._lock:
            if size > self.max_bytes or self._bytes + size > self.max_bytes:
                return False
            self._bytes += size
            return True

    def release(self, size):
        with self._lock:
            self._bytes = max(0, self._bytes - max(0, int(size)))

    def current_bytes(self):
        with self._lock:
            return int(self._bytes)

