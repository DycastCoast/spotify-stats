"""
Worked-out pages saved to disk, so a restart (every update on the cluster) doesn't mean
recalculating everything: rebuilding one person's timeline takes ~7 s, loading it back ~0.1 s.

Use it UNDER @lru_cache, on the functions whose last argument(s) are the "has anything
changed?" key (stats.latest_play_id):

    @lru_cache(maxsize=4)
    @disk_cached(versions=1)          # the last 1 argument is the version key
    def _skips(user_id, _latest): ...

- Memory first (lru_cache), then disk, then work it out (and save it).
- The file name includes a fingerprint of the code itself, so after an update with changed
  code, old saved results are never used (they might not fit the new pages).
- When a newer version is saved, older ones for the same arguments are deleted.
- Saved with pickle. Only ever files this app wrote itself, on its own storage: never load
  pickles from anywhere else, since loading one can run code.
"""
import hashlib
import os
import pickle
import threading
import time
from functools import wraps
from pathlib import Path

from db import DATA_DIR

CACHE_DIR = DATA_DIR / "cache"
MAX_AGE_DAYS = 30
ENABLED = os.environ.get("DISK_CACHE", "1") != "0"


def _code_fingerprint():
    """Changes whenever any of the app's Python files change (a new version)."""
    h = hashlib.sha1()
    for f in sorted(Path(__file__).parent.glob("*.py")):
        h.update(f.read_bytes())
    return h.hexdigest()[:10]


CODE = _code_fingerprint()


def _hash(value):
    return hashlib.sha1(repr(value).encode()).hexdigest()[:16]


def disk_cached(versions=1):
    def decorate(fn):
        name = f"{fn.__module__}.{fn.__qualname__}"

        @wraps(fn)
        def wrapper(*args):
            if not ENABLED:
                return fn(*args)
            stable = args[:-versions] if versions else args
            prefix = f"{name}-{_hash(stable)}-"
            path = CACHE_DIR / f"{prefix}{_hash((CODE, args))}.pkl"
            try:
                with open(path, "rb") as f:
                    return pickle.load(f)
            except FileNotFoundError:
                pass
            except Exception:                       # half-written or from incompatible code: redo it
                path.unlink(missing_ok=True)
            result = fn(*args)
            try:
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(f".{os.getpid()}-{threading.get_ident()}.tmp")
                with open(tmp, "wb") as f:
                    pickle.dump(result, f, protocol=pickle.HIGHEST_PROTOCOL)
                os.replace(tmp, path)                # atomic: a reader never sees half a file
                for old in CACHE_DIR.glob(f"{prefix}*.pkl"):
                    if old != path:
                        old.unlink(missing_ok=True)
            except OSError:
                pass                                 # disk full or read-only: still works, just slower
            return result
        return wrapper
    return decorate


def sweep():
    """Delete saved results nobody has asked for in a month (e.g. for filters no longer used)."""
    if not CACHE_DIR.exists():
        return 0
    cutoff = time.time() - MAX_AGE_DAYS * 86400
    n = 0
    for f in CACHE_DIR.glob("*"):
        try:
            if f.stat().st_atime < cutoff and f.stat().st_mtime < cutoff:
                f.unlink()
                n += 1
        except OSError:
            pass
    return n
