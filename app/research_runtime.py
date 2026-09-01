"""Runtime coordination for heavy historical research vs the live scanner.

The Railway service is intentionally a single small process.  A full 210-stock,
180-day research sweep and the live scanner both issue many Kite historical
requests and build sizeable pandas frames.  Running them at the same time can
push the worker into memory/CPU pressure and makes a host restart much more
likely.  This module gives those two workloads one shared heavy-work slot.

Research has priority once requested: ``begin_research`` marks it active before
it waits for a currently-running live scan to finish.  The background loop sees
that flag and yields until research ends.  Dashboard/API requests remain fully
available because only heavy scanning work is paused.
"""
from contextlib import contextmanager
import datetime as dt
import ctypes
import gc
import os
import threading

_state_lock = threading.Lock()
_heavy_lock = threading.Lock()
_live_lock_owner = False
_state = {
    "active": False,
    "mode": None,
    "started_at": None,
    "heartbeat_at": None,
    "stage": None,
    "symbol": None,
    "done": 0,
    "total": 0,
}


def _iso_now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def current_rss_mb():
    """Best-effort current resident memory, not peak memory."""
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    # Linux reports kB.
                    return round(float(line.split()[1]) / 1024.0, 1)
    except Exception:
        pass
    return None



def _load_libc():
    """Best-effort glibc handle for returning freed heap pages to Linux."""
    try:
        return ctypes.CDLL("libc.so.6")
    except Exception:
        return None


def release_memory_pressure():
    """Collect Python cycles and ask glibc to release free arenas to the OS.

    Historical pandas/NumPy work can free objects without reducing process RSS.
    Railway enforces memory at the process/container level, so this explicit trim
    keeps long research sweeps from accumulating allocator high-water memory.
    """
    gc.collect()
    libc = _load_libc()
    if libc is not None:
        try:
            libc.malloc_trim(0)
        except Exception:
            pass
    return current_rss_mb()

def begin_research(mode):
    now = _iso_now()
    with _state_lock:
        _state.update({
            "active": True,
            "mode": str(mode or "research"),
            "started_at": now,
            "heartbeat_at": now,
            "stage": "Waiting for research worker slot",
            "symbol": None,
            "done": 0,
            "total": 0,
        })


def heartbeat(*, stage=None, symbol=None, done=None, total=None):
    with _state_lock:
        _state["heartbeat_at"] = _iso_now()
        if stage is not None:
            _state["stage"] = str(stage)
        if symbol is not None:
            _state["symbol"] = str(symbol)
        if done is not None:
            _state["done"] = int(done)
        if total is not None:
            _state["total"] = int(total)


def end_research():
    with _state_lock:
        _state.update({
            "active": False,
            "mode": None,
            "heartbeat_at": _iso_now(),
            "stage": None,
            "symbol": None,
            "done": 0,
            "total": 0,
        })


def is_research_active():
    with _state_lock:
        return bool(_state["active"])


def snapshot():
    with _state_lock:
        out = dict(_state)
    out["rss_mb"] = current_rss_mb()
    out["pid"] = os.getpid()
    return out


@contextmanager
def research_slot():
    """Exclusive heavy-work slot for a historical research job."""
    _heavy_lock.acquire()
    try:
        yield
    finally:
        _heavy_lock.release()


def try_enter_live_scan():
    """Acquire the heavy slot for one live scan without blocking research.

    Returns True only if no research request is active and the slot was acquired.
    A second active check after acquisition closes the race where research starts
    between the first check and lock acquisition.
    """
    global _live_lock_owner
    if is_research_active():
        return False
    if not _heavy_lock.acquire(blocking=False):
        return False
    if is_research_active():
        _heavy_lock.release()
        return False
    _live_lock_owner = True
    return True


def live_scan_slot():
    """Named alias used by the background scanner/tests."""
    return try_enter_live_scan()


def exit_live_scan():
    global _live_lock_owner
    if _live_lock_owner:
        _live_lock_owner = False
        _heavy_lock.release()
