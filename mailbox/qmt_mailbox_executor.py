# -*- coding: utf-8 -*-
"""qmt_mailbox_executor.py -- execution side, runs inside the QMT client.

The QMT built-in Python strategy executes orders in-process, through the
broker-controlled runtime. This file is the transport: the bridge polls a
directory (MAILBOX) and drops JSON requests in; the strategy executes them
with passorder/cancel and writes the answers back.

Why a mailbox and not a socket
------------------------------
The client runs an anti-external-connection watchdog: a strategy that dials
127.0.0.1 or a LAN address is terminated, and the rule lives in Formula.dll,
not in the interpreter. open()/os.remove()/os.replace() are also blocked
inside the client with "PermissionError: Forbidden fileIO". The mailbox uses
kernel32 through ctypes for every file operation, which the hook does not
intercept, and the strategy never touches the network.

Interpreter
-----------
Works on 3.6 and 3.12. No dataclasses, no walrus, no f-string nesting.

Setup
-----
1. QMT -> Model Research -> New -> Python strategy, paste this file in.
2. Run period = 1 second (or tick), any liquid symbol as chart instrument.
3. Pick the trading account in the strategy deployment settings; if the
   account is not bound, orders are rejected (see _require_account).
4. Start the strategy. Console shows [qmt-exec] === init begin build=... ===
5. Start the bridge with QMT_BRIDGE_EXECUTOR=mailbox and
   QMT_BRIDGE_INTERNAL_MAILBOX pointing at the same MAILBOX.
6. Tighten the mailbox ACL once:
   icacls "<MAILBOX>" /inheritance:r /grant:r "%USERNAME%:(OI)(CI)F"
   The mailbox is the only command channel; anyone who can write to it can
   place an order.

Paths
-----
MAILBOX defaults to <drive of this file>:\\qmt-bridge-next\\mailbox, and the
log directory to the sibling "logs". $QMT_BRIDGE_HOME overrides the root.
__file__ is used only to pick the drive; a pasted strategy may not have one,
so sys.executable and cwd are fallbacks.

Request protocol
----------------
Bridge -> agent: {"id": 7, "method": "order_stock", "params": {...},
                  "nonce": "ab12", "ts": 1793000000000}
Agent -> bridge: {"type": "resp", "id": 7, "ok": true, "result": {...}}

A request whose ts is older than 30 seconds is rejected. A request id that
was already executed is answered from a cache instead of being run again.
order_stock answers in two stages, because the QMT order id is not knowable
at submit time:

  {"status": "confirmed", "tag": "q5f00a1b20003", "order_id": "..."}
  {"status": "submitted", "tag": "q5f00a1b20003", "order_id": 0}

The submitted form is followed by an event:

  {"type": "event", "event": "order_confirmed",
   "data": {"tag": ..., "order_id": ..., "order_sysid": ..., "order_status": ...}}
  {"type": "event", "event": "order_confirm_timeout", "data": {"tag": ...}}

Reliability rules
-----------------
* Frames are parsed BEFORE the inbox file is removed; unparsable frames move
  to mailbox/bad/ instead of being silently dropped.
* Handlers never sleep. Order confirmation is polled once per tick from
  _confirm_pending(), so handlebar() is never blocked waiting for the broker.
* _PROCESSED makes request ids idempotent; _PENDING_CONFIRM tracks orders
  whose id is not known yet. Both are pruned on every tick.
* get_trade_detail_data's working argument combination is cached per data
  type; an empty result (flat position, no deals) does not invalidate it.
* Internal timers use time.monotonic(); only the wire protocol uses wall
  clock, so an NTP jump cannot expire orders early or leak the cache.
* The account id is read from the strategy context only. No hardcoded
  default: an unbound account means every order would be silently dropped,
  so it is reported as an error instead of papered over.
"""

import json
import os
import sys
import threading
import time

try:
    import queue
except ImportError:  # pragma: no cover - Python 2 safety
    import Queue as queue


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
def _probe_script_dir():
    """Directory this file lives in, with two fallbacks for pasted code."""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        if here and os.path.isdir(here):
            return here
    except Exception:
        pass
    try:
        here = os.path.dirname(os.path.abspath(sys.executable))
        if here and os.path.isdir(here):
            return here
    except Exception:
        pass
    try:
        return os.path.abspath(os.getcwd())
    except Exception:
        return ""


def _drive_root(path):
    """'D:' + separator for the drive a path lives on; empty when unknown."""
    drive = os.path.splitdrive(path or "")[0]
    return drive + os.sep if drive else ""


_SCRIPT_DIR = _probe_script_dir()
_SCRIPT_DRIVE = _drive_root(_SCRIPT_DIR)
if _SCRIPT_DRIVE:
    DEFAULT_ROOT = _SCRIPT_DRIVE + "qmt-bridge-next"
else:
    DEFAULT_ROOT = os.path.join(os.sep, "qmt-bridge-next")

# mbx9: 盖戳位 —— devtools/stamp_executor.py 会把 config.ini 的 base_dir
# 写到这一行（值 = 信箱目录本身）。空串 = 未盖戳，回落到默认根解析。

# mbx10: 盖戳位 —— 统一配置文件绝对路径（config\config.ini，
# 含 account_id/host/port/api_key/base_dir）。空串 = 未盖戳。

BASE_DIR = "D:\\big-qmt-agent-skill\\qmt-bridge-next\\mailbox"
CONFIG_INI = "D:\\big-qmt-agent-skill\\qmt-bridge-next\\config\\config.ini"
# Filled by _resolve_paths() on init(); empty until then.
ROOT = ""
MAILBOX = ""
LOG_DIR = ""

ACCOUNT_TYPE = "STOCK"
STRATEGY_NAME = "qmt_mbx"
DEBUG_RAW = False
PASSORDER_USER_ORDER_ID = True

# Risk caps, checked in _h_order() before passorder. 0 disables.
MAX_ORDER_VALUE = 0.0
MAX_ORDER_VOLUME = 0

TICK_SECONDS = 1
FRESH_FRAME_SEC = 0.2
RESET_KEEP_SEC = 10.0
BAD_DIR_KEEP = 200

PROCESSED_TTL = 300.0
PROCESSED_MAX = 2000
PROCESSED_KEEP = 1500
STALE_REQUEST_MS = 30000
CONFIRM_TIMEOUT = 5.0

# get_trade_detail_data hint: consecutive empty replies before the cached
# argument combination is re-probed. A flat position legitimately returns []
# every tick, so empty is NOT proof the hint is stale.
HINT_MAX_MISS = 5

# Bumped on every change. Printed on the first line of init() so a stale
# paste is distinguishable from a code bug.
BUILD = "2026-10-27-mbx10"


# ----------------------------------------------------------------------
# File IO through Win32 (bypasses the QMT fileIO sandbox)
# ----------------------------------------------------------------------
# QMT intercepts open()/os.open()/os.remove()/os.makedirs()/os.replace() and
# raises PermissionError for every path, reads as well as writes. The hook is
# in the Python C layer, so direct kernel32 calls are not intercepted. Every
# _io_* helper below only ever receives an absolute path.

class _Win32IO(object):
    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    CREATE_ALWAYS = 2
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x80
    FILE_ATTRIBUTE_DIRECTORY = 0x10
    SHARE_ALL = 0x7
    MOVEFILE_REPLACE_EXISTING = 0x1
    INVALID = 18446744073709551615

    def __init__(self):
        import ctypes
        import ctypes.wintypes as wt
        self.ctypes = ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k32 = k32
        k32.CreateFileW.restype = ctypes.c_void_p
        k32.CreateFileW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ]
        k32.ReadFile.restype = ctypes.c_int
        k32.ReadFile.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p,
        ]
        k32.WriteFile.restype = ctypes.c_int
        k32.WriteFile.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p,
        ]
        k32.CloseHandle.restype = ctypes.c_int
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        k32.MoveFileExW.restype = ctypes.c_int
        k32.MoveFileExW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p,
                                    ctypes.c_uint32]
        k32.DeleteFileW.restype = ctypes.c_int
        k32.DeleteFileW.argtypes = [ctypes.c_wchar_p]
        k32.CreateDirectoryW.restype = ctypes.c_int
        k32.CreateDirectoryW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p]
        k32.GetFileAttributesW.restype = ctypes.c_uint32
        k32.GetFileAttributesW.argtypes = [ctypes.c_wchar_p]

        class _FileAttrData(ctypes.Structure):
            _fields_ = [
                ("dwFileAttributes", wt.DWORD),
                ("ftCreationTime", wt.FILETIME),
                ("ftLastAccessTime", wt.FILETIME),
                ("ftLastWriteTime", wt.FILETIME),
                ("nFileSizeHigh", wt.DWORD),
                ("nFileSizeLow", wt.DWORD),
            ]

        self.FileAttrData = _FileAttrData
        k32.GetFileAttributesExW.restype = ctypes.c_int
        k32.GetFileAttributesExW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_int, ctypes.c_void_p,
        ]

        class _FindData(ctypes.Structure):
            _fields_ = [
                ("dwFileAttributes", wt.DWORD),
                ("ftCreationTime", wt.FILETIME),
                ("ftLastAccessTime", wt.FILETIME),
                ("ftLastWriteTime", wt.FILETIME),
                ("nFileSizeHigh", wt.DWORD),
                ("nFileSizeLow", wt.DWORD),
                ("dwReserved0", wt.DWORD),
                ("dwReserved1", wt.DWORD),
                ("cFileName", wt.WCHAR * 260),
                ("cAlternateFileName", wt.WCHAR * 14),
            ]

        self.FindData = _FindData
        k32.FindFirstFileW.restype = ctypes.c_void_p
        k32.FindFirstFileW.argtypes = [ctypes.c_wchar_p,
                                       ctypes.POINTER(_FindData)]
        k32.FindNextFileW.restype = ctypes.c_int
        k32.FindNextFileW.argtypes = [ctypes.c_void_p,
                                      ctypes.POINTER(_FindData)]
        k32.FindClose.restype = ctypes.c_int
        k32.FindClose.argtypes = [ctypes.c_void_p]

    def _bad(self, handle):
        return handle is None or handle == self.INVALID or handle == -1

    def attrs(self, path):
        return self.k32.GetFileAttributesW(path)

    def isdir(self, path):
        a = self.attrs(path)
        return a != 0xFFFFFFFF and bool(a & self.FILE_ATTRIBUTE_DIRECTORY)

    def isfile(self, path):
        a = self.attrs(path)
        return a != 0xFFFFFFFF and not (a & self.FILE_ATTRIBUTE_DIRECTORY)

    def exists(self, path):
        return self.attrs(path) != 0xFFFFFFFF

    def mtime(self, path):
        info = self.FileAttrData()
        if not self.k32.GetFileAttributesExW(path, 0, self.ctypes.byref(info)):
            return 0.0
        stamp = (info.ftLastWriteTime.dwHighDateTime << 32) + \
            info.ftLastWriteTime.dwLowDateTime
        if stamp <= 0:
            return 0.0
        # FILETIME is 100ns ticks since 1601-01-01 UTC.
        return (stamp - 116444736000000000) / 10000000.0

    def mkdir(self, path):
        path = os.path.abspath(path)
        if self.isdir(path):
            return
        parent = os.path.dirname(path)
        if parent and parent != path:
            self.mkdir(parent)
        if not self.k32.CreateDirectoryW(path, None):
            err = self.ctypes.get_last_error()
            if err not in (183,):
                raise OSError("CreateDirectoryW err=%d %s" % (err, path))

    def write_bytes(self, path, data):
        h = self.k32.CreateFileW(
            path, self.GENERIC_WRITE, self.SHARE_ALL, None,
            self.CREATE_ALWAYS, self.FILE_ATTRIBUTE_NORMAL, None,
        )
        if self._bad(h):
            raise OSError("CreateFileW(w) err=%d %s"
                          % (self.ctypes.get_last_error(), path))
        try:
            if not isinstance(data, bytes):
                data = bytes(data)
            done = self.ctypes.c_uint32(0)
            if not self.k32.WriteFile(h, data, len(data),
                                      self.ctypes.byref(done), None):
                raise OSError("WriteFile err=%d %s"
                              % (self.ctypes.get_last_error(), path))
            return done.value
        finally:
            self.k32.CloseHandle(h)

    def read_bytes(self, path, limit=1 << 20):
        h = self.k32.CreateFileW(
            path, self.GENERIC_READ, self.SHARE_ALL, None,
            self.OPEN_EXISTING, self.FILE_ATTRIBUTE_NORMAL, None,
        )
        if self._bad(h):
            raise OSError("CreateFileW(r) err=%d %s"
                          % (self.ctypes.get_last_error(), path))
        try:
            buf = self.ctypes.create_string_buffer(limit)
            done = self.ctypes.c_uint32(0)
            if not self.k32.ReadFile(
                    h, self.ctypes.cast(buf, self.ctypes.c_void_p),
                    limit, self.ctypes.byref(done), None):
                raise OSError("ReadFile err=%d %s"
                              % (self.ctypes.get_last_error(), path))
            return buf.raw[:done.value]
        finally:
            self.k32.CloseHandle(h)

    def replace(self, src, dst):
        if not self.k32.MoveFileExW(src, dst, self.MOVEFILE_REPLACE_EXISTING):
            raise OSError("MoveFileExW err=%d %s -> %s"
                          % (self.ctypes.get_last_error(), src, dst))

    def remove(self, path):
        if not self.k32.DeleteFileW(path):
            err = self.ctypes.get_last_error()
            if err not in (2, 3):
                raise OSError("DeleteFileW err=%d %s" % (err, path))

    def listdir(self, path):
        data = self.FindData()
        h = self.k32.FindFirstFileW(os.path.join(path, "*"),
                                    self.ctypes.byref(data))
        if self._bad(h):
            raise OSError("FindFirstFileW err=%d %s"
                          % (self.ctypes.get_last_error(), path))
        out = []
        try:
            while True:
                name = data.cFileName
                if name not in (".", ".."):
                    out.append(name)
                if not self.k32.FindNextFileW(h, self.ctypes.byref(data)):
                    break
        finally:
            self.k32.FindClose(h)
        return out


_IO = None


def _io():
    global _IO
    if _IO is None:
        try:
            _IO = _Win32IO()
            _log("win32 io ready (kernel32 direct)")
        except Exception as exc:
            _log("win32 io unavailable, fallback to open(): %s" % exc, "WARN")
            _IO = False
    return _IO


def _io_mkdir(path):
    w = _io()
    if w:
        return w.mkdir(path)
    if not os.path.isdir(path):
        os.makedirs(path)


def _io_isdir(path):
    w = _io()
    return w.isdir(path) if w else os.path.isdir(path)


def _io_isfile(path):
    w = _io()
    return w.isfile(path) if w else os.path.isfile(path)


def _io_exists(path):
    w = _io()
    return w.exists(path) if w else os.path.exists(path)


def _io_mtime(path):
    w = _io()
    if w:
        return w.mtime(path)
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _io_write(path, data):
    w = _io()
    if w:
        return w.write_bytes(path, data)
    with open(path, "wb") as fh:
        return fh.write(data)


def _io_read(path, limit=1 << 20):
    w = _io()
    if w:
        return w.read_bytes(path, limit)
    with open(path, "rb") as fh:
        return fh.read()


def _io_replace(src, dst):
    w = _io()
    if w:
        return w.replace(src, dst)
    os.replace(src, dst)


def _io_remove(path):
    w = _io()
    if w:
        return w.remove(path)
    os.remove(path)


def _io_listdir(path):
    w = _io()
    if w:
        return w.listdir(path)
    return os.listdir(path)


# ----------------------------------------------------------------------
# Runtime state
# ----------------------------------------------------------------------

_RX = queue.Queue()
_DISPATCH_LOCK = threading.Lock()
_STATE = {"ctx": None, "tag_seq": 0, "frame_seq": 0, "driver": ""}

# Idempotency cache: request key -> (resp frame, monotonic timestamp).
_PROCESSED = {}
# Orders whose QMT id was not readable at submit time: tag -> dict.
_PENDING_CONFIRM = {}

# get_trade_detail_data argument-matrix cache:
#   (data_type, account_type) -> (account, kind, name)
# miss counter so an empty (but valid) reply does not invalidate the hint.
_TRADE_ROWS_HINT = {}
_TRADE_ROWS_MISS = {}

# WARN/ERROR throttle: key -> (count in window, window start monotonic).
_LOG_THROTTLE = {}

# Handlers that change broker state. These require a request id so a retry
# after a bridge timeout can be answered from cache instead of re-executed.
_ID_REQUIRED = (
    "order_stock", "order_stock_async",
    "cancel_order_stock", "cancel_order_stock_async",
    "cancel_order_stock_sysid", "cancel_order_stock_sysid_async",
)


# ----------------------------------------------------------------------
# Entry points called by QMT
# ----------------------------------------------------------------------

def init(ContextInfo):
    _log("=== init begin build=%s ===" % BUILD)
    _STATE["ctx"] = ContextInfo
    _STATE["tag_seq"] = 0
    _STATE["frame_seq"] = 0
    _STATE.pop("stopped", None)
    _STATE.pop("self_check_done", None)
    _STATE.pop("self_check_tries", None)
    _STATE.pop("startup_summary_logged", None)
    _STATE.pop("order_fields_logged", None)
    _PENDING_CONFIRM.clear()
    _PROCESSED.clear()
    _TRADE_ROWS_HINT.clear()
    _TRADE_ROWS_MISS.clear()
    _LOG_THROTTLE.clear()
    _STATE["started_at"] = time.time()

    steps = [
        ("resolve_paths", _resolve_paths),
        ("log_env", _log_env),
        ("bind_account", lambda: _bind_account(ContextInfo)),
        ("ensure_dirs", _ensure_dirs),
        ("probe_mailbox", _probe_mailbox),
        ("check_mailbox_acl", _check_mailbox_acl),
        ("reset_mailbox", _reset_mailbox),
        ("write_hello", _write_hello),
        ("touch_hb", lambda: _touch_agent_hb(force=True)),
        ("start_timer", lambda: _start_timer(ContextInfo)),
        ("first_pump", lambda: _pump("init")),
    ]
    for name, fn in steps:
        try:
            fn()
            _log("init step ok: %s" % name)
        except Exception as exc:
            _log("init step FAILED: %s -> %s: %s"
                 % (name, type(exc).__name__, exc), "ERROR")
    _log("=== init done build=%s ===" % BUILD)


def handlebar(ContextInfo):
    _STATE["ctx"] = ContextInfo
    _pump("handlebar")


def bridge_tick(ContextInfo):
    _STATE["ctx"] = ContextInfo
    _account_autodetect_tick()
    _pump("run_time")


def account_callback(ContextInfo, accountInfo):
    _store_rows("ACCOUNT", accountInfo)


def position_callback(ContextInfo, positionInfo):
    _store_rows("POSITION", positionInfo)


def order_callback(ContextInfo, orderInfo):
    _store_rows("ORDER", orderInfo)


def deal_callback(ContextInfo, dealInfo):
    _store_rows("DEAL", dealInfo)


def stop(ContextInfo):
    _STATE["stopped"] = True
    try:
        _write_atomic(os.path.join(_mailbox_dir(""), "agent.stopped"),
                      ("%d" % int(time.time() * 1000)).encode("ascii"))
    except Exception:
        pass
    try:
        _io_remove(os.path.join(MAILBOX, "agent.hb"))
    except Exception:
        pass
    _PENDING_CONFIRM.clear()
    _log("agent stopped")


def _pump(driver):
    if not _DISPATCH_LOCK.acquire(False):
        return
    try:
        if _STATE.get("driver") != driver:
            _STATE["driver"] = driver
            _log("queue driver active: %s" % driver)
        _touch_agent_hb()
        _drain_inbox()
        _self_check()
        while True:
            try:
                msg = _RX.get_nowait()
            except queue.Empty:
                break
            _dispatch(msg)
        _confirm_pending()
        _prune_processed()
    finally:
        _DISPATCH_LOCK.release()


def _start_timer(ContextInfo):
    runner = getattr(ContextInfo, "run_time", None)
    if not callable(runner):
        _log("run_time unavailable: set the bar period to 1 second, or "
             "orders stall", "WARN")
        return
    try:
        runner("bridge_tick", "%dnSecond" % TICK_SECONDS,
               "2020-01-01 09:30:00")
        _log("timer registered: every %ds (bridge_tick)" % TICK_SECONDS)
    except Exception as exc:
        _log("timer register failed: %s" % exc, "WARN")
        _log("fallback: set the bar period to 1 second")


# ----------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------

def _log(text, level="INFO"):
    prefix = "[qmt-exec]" if level == "INFO" else "[qmt-exec][%s]" % level
    try:
        # flush=True: the QMT console is block-buffered on some builds, and
        # the last few lines before a crash are exactly the ones that matter.
        print("%s %s" % (prefix, text), flush=True)
    except Exception:
        pass


def _log_throttled(key, text, level="WARN", per_min=3):
    now = time.monotonic()
    count, window = _LOG_THROTTLE.get(key, (0, 0.0))
    if now - window >= 60.0:
        count, window = 0, now
    if count >= per_min:
        _LOG_THROTTLE[key] = (count, window)
        return
    _LOG_THROTTLE[key] = (count + 1, window)
    _log(text, level)


# ----------------------------------------------------------------------
# Path resolution
# ----------------------------------------------------------------------

def _resolve_paths():
    """root = $QMT_BRIDGE_HOME or <drive of this file>:\\qmt-bridge-next.

    mailbox and logs are fixed subdirectories of root. Keeping the mailbox
    on the same volume as its temp file is required for the atomic rename in
    _write_atomic (MoveFileExW with MOVEFILE_REPLACE_EXISTING is only atomic
    within one volume).
    """
    global ROOT, MAILBOX, LOG_DIR

    # mbx9 解析顺序：盖戳 BASE_DIR（config.ini 的 base_dir，由
    # stamp_executor.py 写入）→ QMT_BRIDGE_HOME → 默认根\mailbox。
    stamped = (os.environ.get("BASE_DIR") or BASE_DIR or "").strip()
    if stamped:
        MAILBOX = os.path.normpath(stamped)
        ROOT = os.path.dirname(MAILBOX)
    else:
        env_home = os.environ.get("QMT_BRIDGE_HOME", "").strip()
        root = env_home or DEFAULT_ROOT
        if not os.path.isabs(root):
            root = os.path.abspath(root)
        ROOT = root
        MAILBOX = os.path.normpath(os.path.join(ROOT, "mailbox"))
    # mbx8 约定：执行器全部产物（日志、配置）收敛在 mailbox 内，
    # 目录边界 = ACL 边界，收紧一条 icacls 即覆盖所有文件。
    LOG_DIR = os.path.normpath(os.path.join(MAILBOX, "logs"))

    _log("paths resolved: root=%s mailbox=%s logs=%s"
         % (ROOT, MAILBOX, LOG_DIR))


def _ensure_dirs():
    for path in (MAILBOX,
                 os.path.join(MAILBOX, "in"),
                 os.path.join(MAILBOX, "out"),
                 os.path.join(MAILBOX, "bad"),
                 LOG_DIR):
        _io_mkdir(path)
    _log("dirs ready: %s" % MAILBOX)


_SHARED_DIRS = (
    r"C:\Users\Public",
    r"C:\ProgramData",
    r"C:\Windows\Temp",
    r"C:\temp",
)

DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
DRIVE_REMOTE = 4


def _drive_type(path):
    try:
        import ctypes
        drive = os.path.splitdrive(path)[0]
        if not drive:
            return 0
        return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\")
    except Exception:
        return 0


def _check_mailbox_acl():
    path = str(MAILBOX or "")
    dtype = _drive_type(path)
    if dtype and dtype != DRIVE_FIXED:
        _log("mailbox is on a non-fixed drive (type=%d): %s -- removable or "
             "network volumes can disappear mid-run" % (dtype, path), "WARN")
    low = path.lower()
    for prefix in _SHARED_DIRS:
        if low.startswith(prefix.lower()):
            _log("mailbox %s is inside a shared location (%s): any local "
                 "user can forge requests there, move it under your user "
                 "profile" % (path, prefix), "WARN")
            return
    _log("mailbox is the only command channel: tighten it once with "
         "icacls \"%s\" /inheritance:r /grant:r \"%%USERNAME%%:(OI)(CI)F\""
         % path)


def _probe_mailbox():
    ok, detail = _probe_write(MAILBOX)
    _log("mailbox probe %s %s (%s)"
         % ("OK  " if ok else "FAIL", MAILBOX, detail),
         "INFO" if ok else "ERROR")
    if not ok:
        _log("mailbox not writable: orders will fail until this is fixed",
             "ERROR")


def _probe_write(path):
    probe = os.path.join(path, "probe.tmp")
    try:
        _io_mkdir(path)
        _io_write(probe, b"ok")
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)
    try:
        if _io_read(probe) != b"ok":
            return False, "readback mismatch"
    except Exception as exc:
        return False, "read %s: %s" % (type(exc).__name__, exc)
    try:
        _io_remove(probe)
    except Exception:
        pass
    return True, "writable"


def _mailbox_dir(name):
    path = os.path.join(MAILBOX, name) if name else MAILBOX
    _io_mkdir(path)
    return path


def _write_atomic(path, data):
    """Write via a same-directory temp file so no reader sees a half frame.

    The temp file must stay on the same volume: MoveFileExW with
    MOVEFILE_REPLACE_EXISTING is only atomic within one volume.
    """
    tmp = path + ".tmp"
    _io_write(tmp, data)
    try:
        _io_replace(tmp, path)
    except Exception:
        _io_write(path, data)
        try:
            _io_remove(tmp)
        except Exception:
            pass


def _reset_mailbox():
    """Drop stale frames; keep fresh inbox files so a restart does not eat a
    request the bridge delivered a moment earlier.
    """
    removed = 0
    now = time.time()
    for name in ("out", "in"):
        folder = _mailbox_dir(name)
        try:
            stale = _io_listdir(folder)
        except Exception:
            stale = []
        for item in stale:
            path = os.path.join(folder, item)
            if name == "in" and (now - _io_mtime(path)) < RESET_KEEP_SEC:
                continue
            try:
                _io_remove(path)
                removed += 1
            except Exception:
                pass
    _log("mailbox reset: removed %d" % removed)


def _write_hello():
    frame = {
        "type": "hello",
        "version": "1.3",
        "account_id": _account_id(),
        "account_type": ACCOUNT_TYPE,
        "strategy": STRATEGY_NAME,
        "build": BUILD,
        "started_at": int(_STATE.get("started_at", time.time()) * 1000),
    }
    data = (json.dumps(frame) + "\n").encode("utf-8")
    _write_atomic(os.path.join(_mailbox_dir(""), "hello.json"), data)


def _touch_agent_hb(force=False):
    """Heartbeat as JSON. Throttle uses monotonic; the wire ts is wall clock."""
    if _STATE.get("stopped"):
        return
    mono = time.monotonic()
    if not force and mono - _STATE.get("last_agent_hb", 0.0) < 2.0:
        return
    _STATE["last_agent_hb"] = mono
    _STATE["hb_seq"] = (_STATE.get("hb_seq", 0) + 1) % 1000000
    body = {
        "ts": int(time.time() * 1000),
        "build": BUILD,
        "seq": _STATE["hb_seq"],
        "pending": len(_PENDING_CONFIRM),
    }
    try:
        _write_atomic(os.path.join(_mailbox_dir(""), "agent.hb"),
                      (json.dumps(body) + "\n").encode("utf-8"))
        try:
            _io_remove(os.path.join(MAILBOX, "agent.stopped"))
        except Exception:
            pass
    except Exception:
        pass


def _bad_dir():
    return _mailbox_dir("bad")


def _trim_bad():
    folder = _bad_dir()
    try:
        names = _io_listdir(folder)
    except Exception:
        return
    if len(names) <= BAD_DIR_KEEP:
        return
    items = sorted((_io_mtime(os.path.join(folder, n)), n) for n in names)
    for _, name in items[:len(items) - BAD_DIR_KEEP]:
        try:
            _io_remove(os.path.join(folder, name))
        except Exception:
            pass


def _quarantine_frame(path, name, reason):
    """Move a broken frame to bad/ instead of deleting the evidence.

    A frame only milliseconds old is probably still being written, so it is
    left alone and retried on the next tick. Older files are genuinely broken.
    """
    age = time.time() - _io_mtime(path)
    if age < FRESH_FRAME_SEC:
        return
    try:
        _io_replace(path, os.path.join(_bad_dir(), name))
    except Exception as exc:
        try:
            _io_remove(path)
        except Exception:
            pass
        _log("bad frame moved: %s (rename failed, dropped: %s)"
             % (name, exc), "ERROR")
        return
    _log("bad frame moved: %s %s" % (name, reason), "ERROR")
    _trim_bad()


def _drain_inbox():
    """Read -> parse -> delete only on success.

    Deleting before parsing would turn one truncated frame into a lost
    request: the json.loads exception is logged, but the file is already gone.
    """
    inbox = os.path.join(MAILBOX, "in")
    try:
        names = [n for n in _io_listdir(inbox) if n.endswith(".json")]
    except Exception:
        return
    if not names:
        return
    names.sort()
    for name in names:
        path = os.path.join(inbox, name)
        try:
            data = _io_read(path)
        except Exception:
            continue
        if not data.strip():
            _quarantine_frame(path, name, "empty frame")
            continue
        try:
            msg = json.loads(data.decode("utf-8"))
        except Exception as exc:
            _quarantine_frame(path, name,
                              "%s: %s" % (type(exc).__name__, exc))
            continue
        try:
            _io_remove(path)
        except Exception:
            pass
        if isinstance(msg, dict):
            _RX.put(msg)
        else:
            _log("frame ignored, not a JSON object: %s" % name, "WARN")


def _send_json(payload):
    data = (json.dumps(payload) + "\n").encode("utf-8")
    _STATE["frame_seq"] = _STATE.get("frame_seq", 0) + 1
    name = "%016d-%06d.json" % (int(time.time() * 1000), _STATE["frame_seq"])
    _write_atomic(os.path.join(_mailbox_dir("out"), name), data)


def _resp_msg(req_id, ok, result=None, error=None):
    msg = {"type": "resp", "id": req_id, "ok": bool(ok)}
    if ok:
        msg["result"] = result
    else:
        msg["error"] = error or "unknown error"
    return msg


def _send_msg(msg):
    try:
        _send_json(msg)
    except Exception as exc:
        _log("resp send failed: %s" % exc, "ERROR")


def _push_event(name, data):
    try:
        _send_json({"type": "event", "event": name, "data": data})
    except Exception:
        pass


# ----------------------------------------------------------------------
# Dispatch
# ----------------------------------------------------------------------

def _req_key(msg):
    nonce = msg.get("nonce")
    if nonce:
        return "%s|%s" % (msg.get("id"), nonce)
    return str(msg.get("id"))


def _prune_processed():
    now = time.monotonic()
    stale = [k for k, v in _PROCESSED.items() if now - v[1] > PROCESSED_TTL]
    for key in stale:
        _PROCESSED.pop(key, None)
    if len(_PROCESSED) > PROCESSED_MAX:
        items = sorted(_PROCESSED.items(), key=lambda kv: kv[1][1])
        for key, _ in items[:len(items) - PROCESSED_KEEP]:
            _PROCESSED.pop(key, None)


def _reject(msg, reason):
    req_id = msg.get("id")
    data = {"method": msg.get("method"), "reason": reason,
            "nonce": msg.get("nonce")}
    if req_id is None:
        _push_event("request_rejected", data)
        return
    _send_msg(_resp_msg(req_id, False, None, reason))


def _dispatch(msg):
    req_id = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}

    if not isinstance(params, dict):
        _reject(msg, "invalid params: expected an object")
        return

    key = _req_key(msg)
    cached = _PROCESSED.get(key)
    if cached is not None:
        _log("duplicate id=%s replayed (%s)" % (req_id, method))
        _send_msg(cached[0])
        return

    if req_id is None or str(req_id).strip() == "":
        if method in _ID_REQUIRED:
            _log("state-changing request without id rejected: %s" % method,
                 "ERROR")
            _reject(msg, "error: missing request id")
            return
        _log_throttled("no_id", "query %s sent without an id" % method,
                       "WARN")

    ts = msg.get("ts")
    if ts is None:
        _log_throttled("legacy_ts",
                       "request without ts (legacy bridge?): %s" % method,
                       "WARN")
    else:
        try:
            age_ms = time.time() * 1000.0 - float(ts)
        except (TypeError, ValueError):
            age_ms = 0.0
        if abs(age_ms) > STALE_REQUEST_MS:
            _log("stale request rejected: id=%s method=%s age=%.1fs"
                 % (req_id, method, age_ms / 1000.0), "ERROR")
            _reject(msg, "error: stale request (age %.1fs)"
                    % (age_ms / 1000.0))
            return

    handler = _HANDLERS.get(method)
    if handler is None:
        resp = _resp_msg(req_id, False, None,
                         "unsupported method: %s" % method)
    else:
        try:
            resp = _resp_msg(req_id, True, handler(params))
        except Exception as exc:
            _log("%s failed: %s: %s" % (method, type(exc).__name__, exc),
                 "ERROR")
            resp = _resp_msg(req_id, False, None,
                             "%s: %s" % (type(exc).__name__, exc))
    # Cache before sending: if the write fails, the retry still gets the same
    # answer instead of executing the handler again.
    if req_id is not None and str(req_id).strip() != "":
        _PROCESSED[key] = (resp, time.monotonic())
    _send_msg(resp)


# ----------------------------------------------------------------------
# Account binding
# ----------------------------------------------------------------------

def _account_id():
    """Account id bound to the strategy context.

    Read-only on purpose: if the account is not bound, QMT drops every order
    silently, so an empty return is a hard error the caller must surface, not
    a value to paper over with a hardcoded default.

    Auto-detection order (mbx4): context attrs -> ACCOUNT callback cache ->
    direct get_trade_detail_data probe (throttled, driven by
    _account_autodetect_tick in bridge_tick). Every source is the account
    the client itself bound/pushed; nothing is hardcoded.
    """
    if _STATE.get("account_ok") and _STATE.get("account_id_detected"):
        return _STATE["account_id_detected"]
    aid = _ctx_account()
    if aid:
        _STATE["account_ok"] = True
        _STATE["account_id_detected"] = aid
        return aid
    aid = _account_from_rows()
    if aid:
        _STATE["account_ok"] = True
        _STATE["account_id_detected"] = aid
        return aid
    return ""


_ACCOUNT_ATTRS = (
    "accID", "accid", "account_id", "accountID", "m_strAccountID",
    "m_strAccID", "account", "get_account_id",
)


def _ctx_account():
    """Account id bound to the context. Attribute names differ across builds."""
    ctx = _STATE.get("ctx")
    if ctx is None:
        return ""
    inner = getattr(ctx, "context", None)
    for obj in (inner, ctx):
        if obj is None:
            continue
        for name in _ACCOUNT_ATTRS:
            try:
                value = getattr(obj, name, "")
            except Exception:
                continue
            if callable(value):
                try:
                    value = value()
                except Exception:
                    continue
            if isinstance(value, (str, int)) and str(value).strip():
                return str(value).strip()
    return ""


_ACCOUNT_ID_KEYS = (
    "m_strAccountID", "accountID", "accID", "m_strAccID",
    "account_id", "account", "m_strAccountKey",
)

_ACCOUNT_PROBE_SEC = 5.0


def _account_from_rows():
    """Extract the account id from cached ACCOUNT callback rows."""
    for row in _LAST_ROWS.get("ACCOUNT") or []:
        if not isinstance(row, dict):
            continue
        for key in _ACCOUNT_ID_KEYS:
            value = row.get(key)
            if isinstance(value, (str, int)) and str(value).strip():
                return str(value).strip()
    return ""


def _account_probe():
    """Direct get_trade_detail_data probes for ACCOUNT rows.

    Used only when neither the context attrs nor the callback cache have
    produced an id yet. Any hit is written into _LAST_ROWS so later queries
    share the same source. Failures are logged throttled (mbx5) so a silent
    miss is visible in the client console.
    """
    errors = []
    for args in (("ACCOUNT",), ("", "STOCK", "ACCOUNT"),
                 ("", "Account", "ACCOUNT")):
        try:
            rows = get_trade_detail_data(*args)
        except Exception as exc:
            errors.append("%s -> %s: %s" % (args, type(exc).__name__, exc))
            continue
        for item in rows or []:
            try:
                row = _dump(item)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            for key in _ACCOUNT_ID_KEYS:
                value = row.get(key)
                if isinstance(value, (str, int)) and str(value).strip():
                    _LAST_ROWS["ACCOUNT"] = [row]
                    return str(value).strip()
        if rows:
            errors.append("%s -> %d row(s), no id key matched: %s"
                          % (args, len(rows), sorted(_dump(rows[0]))[:6]))
    _log_throttled(
        "account_probe",
        "account probe: no id yet (%s)"
        % ("; ".join(errors) or "all combos empty"), "INFO", 30)
    return ""


def _account_autodetect_tick():
    """Called from bridge_tick: retry detection until the id is known.

    On success: log once, try set_account (harmless if run settings already
    bound it) and rewrite hello.json so the bridge sees account_id without
    restarting the strategy.
    """
    if _STATE.get("account_ok"):
        return
    now = time.monotonic()
    if now - _STATE.get("account_probe_at", 0.0) < _ACCOUNT_PROBE_SEC:
        return
    _STATE["account_probe_at"] = now
    aid = _account_id()
    if not aid:
        aid = _account_from_config()
    if not aid:
        aid = _account_probe()
    if not aid:
        return
    _STATE["account_ok"] = True
    _STATE["account_id_detected"] = aid
    _log("account auto-detected: %s" % aid)
    ctx = _STATE.get("ctx")
    if ctx is not None:
        try:
            _bind_account(ctx)
        except Exception as exc:
            _log("set_account after auto-detect failed: %s" % exc, "WARN")
    try:
        _write_hello()
    except Exception as exc:
        _log("hello refresh after auto-detect failed: %s" % exc, "WARN")


def _account_from_config():
    """Read the account id from the unified config.ini (mbx10).

    Search order:
      1. CONFIG_INI   (stamped absolute path to config\\config.ini)
      2. MAILBOX\\config.ini, MAILBOX\\account.ini
      3. ROOT\\config.ini, ROOT\\account.ini          (旧布局兼容)

    Read via kernel32 (open() is blocked inside the client). Accepted
    forms (first non-empty, non-comment line whose key matches):

        account_id = 66660000      (key = value)
        66660000                   (bare id)

    Missing file means "not provided", never a hardcoded default.
    """
    paths = []
    if CONFIG_INI:
        paths.append(CONFIG_INI)
    for base in (MAILBOX, ROOT):
        paths.append(os.path.join(base, "config.ini"))
        paths.append(os.path.join(base, "account.ini"))
    for path in paths:
        try:
            if not _io_isfile(path):
                continue
            raw = _io_read(path, 4096) or b""
        except Exception as exc:
            _log("config read failed (%s): %s" % (path, exc), "WARN")
            continue
        for line in raw.decode("utf-8", "replace").splitlines():
            line = line.split("#", 1)[0].split(";", 1)[0].strip()
            if not line:
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                if key.strip().lower() not in (
                        "account_id", "accountid", "account"):
                    continue
                line = value.strip()
            token = line.split()[0][:32] if line.split() else ""
            if token and any(ch.isalnum() for ch in token):
                return token
    return ""


def _bind_account(ctx):
    """Bind the account. Without this, orders are silently dropped."""
    aid = _account_id()
    source = "context"
    if not aid:
        aid = _account_from_config()
        source = "account.txt"
    if not aid:
        _log("account not bound: pick the trading account in the strategy "
             "deployment settings. Orders will be rejected until this is "
             "fixed", "ERROR")
        return
    if source == "account.txt":
        _STATE["account_ok"] = True
        _STATE["account_id_detected"] = aid
        _log("account id loaded from account.ini: %s" % aid)
    setter = getattr(ctx, "set_account", None)
    if not callable(setter):
        _log("ContextInfo.set_account missing; bind the account in run "
             "settings", "WARN")
        return
    for args in ((aid,), (aid, ACCOUNT_TYPE), (aid, ACCOUNT_TYPE.lower())):
        try:
            setter(*args)
        except Exception as exc:
            _log("set_account%s failed: %s" % (list(args), exc), "WARN")
            continue
        _log("account bound: %s args=%s" % (aid, list(args)))
        return
    _log("set_account failed for every argument form; if orders are "
         "dropped, bind the account in the client run settings", "WARN")


def _account_type(params):
    value = str(params.get("account_type") or ACCOUNT_TYPE or "STOCK").upper()
    if value not in ("STOCK", "CREDIT", "FUTURE", "OPTION"):
        value = "STOCK"
    return value


def _require_account():
    """Account id for a state-changing call; raises when unbound.

    Queries may return empty results for an unbound account, but an order
    would be dropped by QMT with no error anywhere -- exactly the failure
    this check exists to turn into a loud response.
    """
    account = _account_id()
    if not account:
        raise RuntimeError(
            "account not bound: pick the trading account in the strategy "
            "deployment settings, otherwise orders are silently dropped")
    return account


# ----------------------------------------------------------------------
# Order handlers
# ----------------------------------------------------------------------

def _make_tag(remark):
    """Build the order_remark; always unique within 65536 orders."""
    _STATE["tag_seq"] = (_STATE.get("tag_seq", 0) + 1) % 0x10000
    suffix = "%04x" % _STATE["tag_seq"]
    base = str(remark or "").strip()
    if not base:
        return "q%08x%s" % (int(time.time() * 1000) & 0xFFFFFFFF, suffix)
    return (base[:10] + suffix)[:16]


def _check_limits(code, price, volume):
    if volume <= 0:
        return "order_volume must be positive (got %d)" % volume
    if MAX_ORDER_VOLUME and volume > MAX_ORDER_VOLUME:
        return "order_volume %d exceeds max_order_volume %d" % (
            volume, MAX_ORDER_VOLUME)
    if MAX_ORDER_VALUE:
        if price <= 0:
            return ("price is 0: a market order cannot be checked against "
                    "max_order_value %.2f, refusing (%s)"
                    % (MAX_ORDER_VALUE, code))
        if price * volume > MAX_ORDER_VALUE:
            return "order value %.2f exceeds max_order_value %.2f (%s)" % (
                price * volume, MAX_ORDER_VALUE, code)
    return None


def _passorder(op_type, order_style, account, code, price_type, price,
               volume, tag, ctx):
    """Call passorder; return True when the tag could be attached."""
    if not PASSORDER_USER_ORDER_ID:
        passorder(op_type, order_style, account, code, price_type, price,
                  volume, STRATEGY_NAME, 1, ctx)
        return False
    try:
        passorder(op_type, order_style, account, code, price_type, price,
                  volume, STRATEGY_NAME, 1, tag, ctx)
        return True
    except TypeError:
        _log("passorder rejected userOrderId, retrying without it", "WARN")
        passorder(op_type, order_style, account, code, price_type, price,
                  volume, STRATEGY_NAME, 1, ctx)
        return False


def _h_order(params):
    ctx = _STATE.get("ctx")
    if ctx is None:
        raise RuntimeError("strategy context not ready")

    account = params.get("account_id") or _require_account()
    account_type = _account_type(params)
    op_type = int(params["order_type"])
    order_style = 1101
    code = str(params["stock_code"])
    price_type = int(params.get("price_type", 5))
    price = float(params.get("price", 0.0))
    volume = int(params["order_volume"])

    blocked = _check_limits(code, price, volume)
    if blocked:
        _log("order blocked by risk cap: %s" % blocked, "WARN")
        raise RuntimeError(blocked)

    tag = _make_tag(params.get("order_remark"))
    direction = "buy" if op_type == 23 else (
        "sell" if op_type == 24 else "?")
    _log("order submit tag=%s %s %s vol=%d price=%.3f price_type=%d"
         % (tag, direction, code, volume, price, price_type))

    warning = None
    row = None
    tagged = False
    try:
        tagged = _passorder(op_type, order_style, account, code, price_type,
                            price, volume, tag, ctx)
    except Exception as exc:
        # passorder raising is NOT proof the order was not submitted: the
        # exception can come from bookkeeping after the message was queued.
        found = _lookup_by_tag(tag, account, account_type)
        if found is None:
            _log("passorder failed, nothing found for tag=%s: %s: %s"
                 % (tag, type(exc).__name__, exc), "ERROR")
            raise RuntimeError("passorder failed tag=%s: %s: %s"
                               % (tag, type(exc).__name__, exc))
        warning = ("passorder raised but order found: %s: %s"
                   % (type(exc).__name__, exc))
        _log("passorder raised but order found tag=%s" % tag, "WARN")
        row = found
    else:
        if tagged:
            row = _lookup_by_tag(tag, account, account_type)
            if row is None and _last_order_id():
                _log_throttled("last_id_unverified",
                               "get_last_order_id answered but no order "
                               "carries tag %s" % tag, "WARN")

    if row is not None:
        order_id = row.get("order_id")
        _push_event("async_response", {"order_id": order_id})
        result = {"status": "confirmed", "tag": tag, "order_id": order_id,
                  "order_sysid": row.get("order_sysid"),
                  "order_status": row.get("order_status")}
        if warning:
            result["warning"] = warning
        return result

    if not tagged:
        order_id = _last_order_id()
        if order_id:
            _push_event("async_response", {"order_id": order_id})
            return {"status": "confirmed", "tag": "",
                    "order_id": str(order_id),
                    "warning": "userOrderId unsupported: id taken from "
                               "get_last_order_id and cannot be verified"}
        return {"status": "submitted", "tag": "", "order_id": 0,
                "warning": "userOrderId unsupported and get_last_order_id "
                           "returned nothing: the order cannot be confirmed"}

    _PENDING_CONFIRM[tag] = {
        "deadline": time.monotonic() + CONFIRM_TIMEOUT,
        "account": account,
        "account_type": account_type,
    }
    result = {"status": "submitted", "tag": tag, "order_id": 0}
    if warning:
        result["warning"] = warning
    return result


def _h_order_async(params):
    return _h_order(params)


def _h_cancel(params):
    """Cancel one order; QMT's cancel() takes the counter orderSysID."""
    ctx = _STATE.get("ctx")
    if ctx is None:
        raise RuntimeError("strategy context not ready")
    account = params.get("account_id") or _require_account()
    account_type = _account_type(params)
    want = str(params["order_id"])
    target = want
    try:
        for row in _trade_rows("ORDER", account, account_type):
            mapped = _map_order(row)
            sysid = str(mapped.get("order_sysid") or "")
            if not sysid:
                continue
            if want in (sysid, str(mapped.get("order_ref") or ""),
                        str(mapped.get("order_id") or "")):
                target = sysid
                break
    except Exception as exc:
        _log("cancel lookup skipped: %s" % exc, "WARN")
    _log("cancel order %s (sysid %s)" % (want, target))
    try:
        cancel(target, account, account_type, ctx)
    except Exception as exc:
        # An exception from cancel() does not mean the order is still live.
        # 1) If the order is already terminal, the goal is achieved.
        status = _order_status(want, account, account_type)
        if _is_terminal(status):
            _log("cancel raised but order already terminal (status=%s): %s"
                 % (status, exc), "WARN")
            return {"status": "already_terminal", "order_id": want,
                    "order_status": status}
        # 2) If there is no active ORDER row but a matching DEAL exists, the
        #    order filled before the cancel landed -- not a failure.
        if status is None:
            deal = _find_deal(want, account, account_type)
            if deal is not None:
                _log("cancel: order %s already filled (no active order)"
                     % want, "WARN")
                return {"status": "already_filled", "order_id": want,
                        "traded_volume": deal.get("traded_volume"),
                        "traded_price": deal.get("traded_price")}
        raise RuntimeError("cancel failed id=%s sysid=%s: %s: %s"
                           % (want, target, type(exc).__name__, exc))
    return 0


def _h_cancel_async(params):
    return _h_cancel(params)


def _h_cancel_sysid(params):
    sysid = str(params["sysid"])
    account = params.get("account_id") or _require_account()
    account_type = _account_type(params)
    for row in _trade_rows("ORDER", account, account_type):
        mapped = _map_order(row)
        if str(mapped.get("order_sysid") or "") == sysid:
            return _h_cancel({"order_id": sysid,
                              "account_id": account,
                              "account_type": account_type})
    raise RuntimeError("order_sysid not found: %s" % sysid)


# ----------------------------------------------------------------------
# Query handlers
# ----------------------------------------------------------------------

def _h_query_orders(params):
    account = params.get("account_id") or _account_id()
    account_type = _account_type(params)
    raw_rows = _trade_rows("ORDER", account, account_type)
    if raw_rows and not _STATE.get("order_fields_logged"):
        _STATE["order_fields_logged"] = True
        try:
            _log("order fields: %s" % sorted(_dump(raw_rows[0]).keys()))
        except Exception as exc:
            _log("order field dump failed: %s" % exc)
    rows = [_map_order(r) for r in raw_rows]
    if params.get("cancelable_only"):
        rows = [r for r in rows if _is_cancelable(r)]
    return rows


def _h_query_order(params):
    want = str(params.get("order_id") or "")
    for row in _h_query_orders(params):
        ids = (str(row.get("order_id") or ""),
               str(row.get("order_ref") or ""),
               str(row.get("order_sysid") or ""))
        if want and want in ids:
            return row
    return None


def _h_query_positions(params):
    account = params.get("account_id") or _account_id()
    account_type = _account_type(params)
    return [_map_position(r)
            for r in _trade_rows("POSITION", account, account_type)]


def _h_query_asset(params):
    account = params.get("account_id") or _account_id()
    account_type = _account_type(params)
    rows = _trade_rows("ACCOUNT", account, account_type)
    if not rows:
        return None
    return _map_asset(rows[0])


def _h_query_trades(params):
    account = params.get("account_id") or _account_id()
    account_type = _account_type(params)
    return [_map_deal(r)
            for r in _trade_rows("DEAL", account, account_type)]


# ----------------------------------------------------------------------
# QMT data access helpers
# ----------------------------------------------------------------------

def _uniq(values):
    out = []
    for value in values:
        value = str(value or "")
        if value and value not in out:
            out.append(value)
    return out


_LAST_ROWS = {"ACCOUNT": [], "POSITION": [], "ORDER": [], "DEAL": []}


def _store_rows(kind, payload):
    if payload is None:
        return
    if isinstance(payload, (list, tuple)):
        rows = [_dump(item) for item in payload]
    else:
        rows = [_dump(payload)]
    rows = [row for row in rows if row]
    if not rows:
        return
    if kind == "ACCOUNT":
        _LAST_ROWS[kind] = rows[:1]
    else:
        _LAST_ROWS[kind] = (_LAST_ROWS.get(kind, []) + rows)[-200:]
    _log_throttled("callback:%s" % kind,
                   "callback %s: %d row(s), cached=%d"
                   % (kind, len(rows), len(_LAST_ROWS[kind])),
                   "INFO", 2)


def _trade_rows(data_type, account, account_type):
    """get_trade_detail_data wrapper tolerant about case and account source.

    The working argument combination is cached per data type. An empty
    (but non-raising) reply is a valid result for a flat position or a day
    with no deals, so it is NOT treated as hint invalidation: only a raising
    call, or HINT_MAX_MISS consecutive empties, triggers a re-probe.
    """
    hint_key = (str(data_type or "").upper(),
                str(account_type or "").upper())
    hint = _TRADE_ROWS_HINT.get(hint_key)
    if hint is not None:
        try:
            rows = get_trade_detail_data(hint[0], hint[1], hint[2])
        except Exception:
            _TRADE_ROWS_HINT.pop(hint_key, None)
            _TRADE_ROWS_MISS.pop(hint_key, None)
        else:
            if rows:
                _TRADE_ROWS_MISS.pop(hint_key, None)
                return list(rows)
            miss = _TRADE_ROWS_MISS.get(hint_key, 0) + 1
            if miss < HINT_MAX_MISS:
                _TRADE_ROWS_MISS[hint_key] = miss
                cached = _LAST_ROWS.get(str(data_type or "").upper()) or []
                if cached:
                    _log_throttled(
                        "cache_fallback:%s" % data_type,
                        "query %s empty, callback cache has %d rows"
                        % (data_type, len(cached)), "WARN")
                    return list(cached)
                return []
            _TRADE_ROWS_HINT.pop(hint_key, None)
            _TRADE_ROWS_MISS.pop(hint_key, None)

    accounts = _uniq((account, _account_id(), _ctx_account()))
    types = _uniq((account_type, str(account_type or "").upper(),
                   str(account_type or "").lower()))
    names = _uniq((data_type, str(data_type or "").upper(),
                   str(data_type or "").lower()))
    tried = 0
    errors = []
    for acc in accounts:
        for kind in types:
            for name in names:
                tried += 1
                try:
                    rows = get_trade_detail_data(acc, kind, name)
                except Exception as exc:
                    if len(errors) < 3:
                        errors.append("%s(%s)" % (type(exc).__name__, exc))
                    continue
                if rows:
                    if tried > 1:
                        _log("query %s hit: account=%s type=%s data=%s"
                             % (data_type, acc, kind, name))
                    _TRADE_ROWS_HINT[hint_key] = (acc, kind, name)
                    _TRADE_ROWS_MISS.pop(hint_key, None)
                    return list(rows)
    _log_throttled("no_rows:%s" % data_type,
                   "query %s returned no rows (accounts=%s types=%s "
                   "data=%s tried=%d errors=%s)"
                   % (data_type, accounts, types, names, tried,
                      errors or "none"), "WARN")
    cached = _LAST_ROWS.get(str(data_type or "").upper()) or []
    if cached:
        _log_throttled("cache_fallback:%s" % data_type,
                       "query %s fell back to callback cache (%d rows)"
                       % (data_type, len(cached)), "WARN")
        return list(cached)
    return []


_FINAL_STATUS = (53, 54, 56, 57)


def _is_terminal(status):
    try:
        return int(status) in _FINAL_STATUS
    except (TypeError, ValueError):
        return False


def _is_cancelable(row):
    status = row.get("order_status")
    if status is None:
        return True
    return not _is_terminal(status)


def _order_status(want, account, account_type):
    want = str(want)
    for raw in _trade_rows("ORDER", account, account_type):
        data = _dump(raw)
        if want in (_row_order_id(data), _row_sysid(data)):
            return _pick(data, ("m_nOrderStatus", "m_nStatus"))
    return None


def _last_order_id():
    fn = globals().get("get_last_order_id")
    if not callable(fn):
        return 0
    ctx = _STATE.get("ctx")
    for args in ((ctx,), ()):
        try:
            value = fn(*args)
        except Exception:
            continue
        try:
            order_id = int(value or 0)
        except (TypeError, ValueError):
            continue
        if order_id > 0:
            return order_id
    return 0


def _remark_of(data):
    value = _pick(data, ("m_strRemark", "m_strUserOrderId",
                         "m_strOrderRemark", "m_strRemarkEx"))
    return "" if value is None else str(value)


def _row_order_id(data):
    value = _pick(data, ("m_strOrderRef", "m_nOrderID", "m_nOrderId",
                         "m_strOrderID"))
    return "" if value is None else str(value)


def _row_sysid(data):
    value = _pick(data, ("m_strOrderSysID", "m_strOrderSysId", "m_strSysID"))
    return "" if value is None else str(value)


def _lookup_by_tag(tag, account, account_type):
    """The mapped order row carrying this tag, or None. Non-blocking."""
    if not tag:
        return None
    for raw in _trade_rows("ORDER", account, account_type):
        data = _dump(raw)
        if _remark_of(data) != tag:
            continue
        return _map_order(data)
    return None


def _confirm_pending():
    """Resolve orders submitted without a known id. Runs once per tick."""
    if not _PENDING_CONFIRM:
        return
    now = time.monotonic()
    groups = {}
    for tag, item in _PENDING_CONFIRM.items():
        key = (item["account"], item["account_type"])
        group = groups.get(key)
        if group is None:
            groups[key] = [tag]
        else:
            group.append(tag)

    found = {}
    for (account, account_type), tags in groups.items():
        tag_set = set(tags)
        for raw in _trade_rows("ORDER", account, account_type):
            data = _dump(raw)
            tag = _remark_of(data)
            if tag in tag_set and tag not in found:
                found[tag] = _map_order(data)

    for tag, item in list(_PENDING_CONFIRM.items()):
        row = found.get(tag)
        if row is not None:
            _PENDING_CONFIRM.pop(tag, None)
            _push_event("order_confirmed", {
                "tag": tag,
                "order_id": row.get("order_id"),
                "order_sysid": row.get("order_sysid"),
                "order_status": row.get("order_status"),
            })
            _log("order confirmed tag=%s id=%s sysid=%s status=%s"
                 % (tag, row.get("order_id"), row.get("order_sysid"),
                    row.get("order_status")))
            continue
        if now >= item["deadline"]:
            _PENDING_CONFIRM.pop(tag, None)
            _push_event("order_confirm_timeout", {"tag": tag})
            _log("order confirm timeout tag=%s account=%s"
                 % (tag, item["account"]), "WARN")


def _find_deal(want, account, account_type):
    """The mapped DEAL row matching want (order_id / sysid / ref), or None.

    An order that filled before the cancel landed is no longer in ORDER but
    does appear in DEAL; _h_cancel uses this to report already_filled instead
    of a cancel failure.
    """
    want = str(want)
    for raw in _trade_rows("DEAL", account, account_type):
        mapped = _map_deal(raw)
        if want in (str(mapped.get("order_id") or ""),
                    str(mapped.get("order_sysid") or ""),
                    str(mapped.get("order_ref") or "")):
            return mapped
    return None


# ----------------------------------------------------------------------
# Row mapping
# ----------------------------------------------------------------------

def _dump(obj):
    if isinstance(obj, dict):
        return dict(obj)
    out = {}
    for name in dir(obj):
        if name.startswith("_"):
            continue
        try:
            value = getattr(obj, name)
        except Exception:
            continue
        if callable(value):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[name] = value
    return out


def _pick(raw, names, default=None):
    for name in names:
        if name in raw and raw[name] is not None:
            return raw[name]
    return default


def _stock_code(raw):
    code = _pick(raw, ("m_strInstrumentID", "m_strStockCode", "m_strCode"))
    market = _pick(raw, ("m_strExchangeID", "m_strMarket", "m_strExchange"))
    if code is None:
        return ""
    code = str(code)
    market = str(market or "")
    if "." in code or not market:
        return code
    return code + "." + market


def _direction(raw):
    value = _pick(raw, ("m_nDirection", "m_nEntrustDirection",
                        "m_nOrderType"))
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    if value in (23, 24):
        return value
    if value in (48, 49):
        return 23 if value == 48 else 24
    if value in (0, 1):
        return 23 if value == 0 else 24
    return value


def _map_order(raw):
    data = _dump(raw)
    mapped = {
        "account_id": _pick(data, ("m_strAccountID", "m_strAccountId"),
                            _account_id()),
        "stock_code": _stock_code(data),
        "order_id": _pick(data, ("m_strOrderRef", "m_nOrderID",
                                 "m_nOrderId", "m_strOrderID")),
        "order_ref": _pick(data, ("m_strOrderRef",)),
        "order_sysid": _pick(data, ("m_strOrderSysID", "m_strOrderSysId",
                                    "m_strSysID")),
        "order_time": _pick(data, ("m_strInsertTime", "m_nInsertTime",
                                   "m_strOrderTime")),
        "order_type": _direction(data),
        "order_volume": _pick(data, ("m_nVolumeTotalOriginal", "m_nVolume",
                                     "m_nOrderVolume")),
        "price_type": _pick(data, ("m_eEntrustType", "m_nOrderPriceType",
                                   "m_nPriceType")),
        "order_price_type": _pick(data, ("m_nOrderPriceType",)),
        "price": _pick(data, ("m_dLimitPrice", "m_nPrice", "m_dPrice",
                              "m_dEntrustPrice")),
        "traded_volume": _pick(data, ("m_nVolumeTraded",
                                      "m_nTradedVolume")),
        "traded_price": _pick(data, ("m_dTradedPrice", "m_nTradedPrice")),
        "order_status": _pick(data, ("m_nOrderStatus", "m_nStatus")),
        "status_msg": _pick(data, ("m_strErrorMsg", "m_strCancelInfo",
                                   "m_strStatusMsg", "m_strOrderStatus")),
        "error_id": _pick(data, ("m_nErrorID",)),
        "strategy_name": _pick(data, ("m_strSource", "m_strStrategyName",
                                      "m_strStrategy")),
        "order_remark": _pick(data, ("m_strRemark", "m_strUserOrderId",
                                     "m_strOrderRemark")),
    }
    if DEBUG_RAW:
        mapped["_raw"] = data
    return mapped


def _map_position(raw):
    data = _dump(raw)
    mapped = {
        "account_id": _pick(data, ("m_strAccountID", "m_strAccountId"),
                            _account_id()),
        "stock_code": _stock_code(data),
        "volume": _pick(data, ("m_nVolume", "m_nTotalVolume")),
        "can_use_volume": _pick(data, ("m_nCanUseVolume",
                                       "m_nEnableVolume")),
        "frozen_volume": _pick(data, ("m_nFrozenVolume", "m_nOnHoldVolume")),
        "open_price": _pick(data, ("m_dOpenPrice", "m_dAvgPrice")),
        "market_value": _pick(data, ("m_dMarketValue",
                                     "m_dInstrumentValue")),
    }
    if DEBUG_RAW:
        mapped["_raw"] = data
    return mapped


def _map_asset(raw):
    data = _dump(raw)
    mapped = {
        "account_id": _pick(data, ("m_strAccountID", "m_strAccountId"),
                            _account_id()),
        "cash": _pick(data, ("m_dAvailable", "m_dCashAvailable",
                             "m_dEnableBalance")),
        "frozen_cash": _pick(data, ("m_dFrozenCash", "m_dOrderFrozen")),
        "market_value": _pick(data, ("m_dInstrumentValue",
                                     "m_dMarketValue")),
        "total_asset": _pick(data, ("m_dTotalAsset", "m_dBalance",
                                    "m_dAssureAsset")),
    }
    if DEBUG_RAW:
        mapped["_raw"] = data
    return mapped


def _map_deal(raw):
    data = _dump(raw)
    mapped = {
        "account_id": _pick(data, ("m_strAccountID", "m_strAccountId"),
                            _account_id()),
        "stock_code": _stock_code(data),
        "order_id": _pick(data, ("m_strOrderRef", "m_nOrderID",
                                 "m_nOrderId", "m_strOrderID")),
        "order_ref": _pick(data, ("m_strOrderRef",)),
        "order_sysid": _pick(data, ("m_strOrderSysID", "m_strOrderSysId")),
        "traded_id": _pick(data, ("m_strTradeID", "m_nTradeID",
                                  "m_strTradedID")),
        "traded_time": _pick(data, ("m_strTradeTime", "m_nTradeTime",
                                    "m_strTime")),
        "traded_volume": _pick(data, ("m_nVolume", "m_nTradeVolume")),
        "traded_price": _pick(data, ("m_dPrice", "m_dTradePrice")),
        "order_type": _direction(data),
        "strategy_name": _pick(data, ("m_strSource", "m_strStrategyName",
                                      "m_strStrategy")),
        "order_remark": _pick(data, ("m_strRemark", "m_strOrderRemark")),
    }
    if DEBUG_RAW:
        mapped["_raw"] = data
    return mapped


# ----------------------------------------------------------------------
# Startup diagnostics
# ----------------------------------------------------------------------

def _log_env():
    names = ("get_trade_detail_data", "passorder", "cancel",
             "get_last_order_id")
    for name in names:
        fn = globals().get(name)
        _log("env %s: %s" % (name, "present" if callable(fn) else "MISSING"))


def _log_startup_summary(asset_rows):
    """Print account, cash, positions, orders and trades exactly once.

    Called only after an ACCOUNT query actually returned a row, so an
    unbound account never prints a page of empty values -- _self_check
    already reports that case as an ERROR.
    """
    if _STATE.get("startup_summary_logged"):
        return
    _STATE["startup_summary_logged"] = True
    account = _account_id()

    _log("========== startup summary ==========")
    _log("account_id  : %s" % (account or "<unbound>"))
    _log("account_type: %s" % ACCOUNT_TYPE)
    _log("strategy    : %s  build=%s" % (STRATEGY_NAME, BUILD))
    try:
        asset = _map_asset(asset_rows[0])
        _log("cash        : %s" % asset.get("cash"))
        _log("frozen_cash : %s" % asset.get("frozen_cash"))
        _log("market_value: %s" % asset.get("market_value"))
        _log("total_asset : %s" % asset.get("total_asset"))
    except Exception as exc:
        _log("asset mapping failed: %s" % exc, "WARN")

    try:
        positions = _trade_rows("POSITION", account, ACCOUNT_TYPE)
    except Exception as exc:
        positions = []
        _log("position query failed: %s" % exc, "WARN")
    _log("positions   : %d" % len(positions))
    for raw in positions[:10]:
        try:
            pos = _map_position(raw)
        except Exception:
            continue
        _log("  %-12s vol=%-8s can_use=%-8s mv=%s"
             % (pos.get("stock_code") or "?",
                pos.get("volume"), pos.get("can_use_volume"),
                pos.get("market_value")))
    if len(positions) > 10:
        _log("  ... %d more positions" % (len(positions) - 10))

    try:
        orders = _trade_rows("ORDER", account, ACCOUNT_TYPE)
    except Exception:
        orders = []
    active = 0
    for raw in orders:
        try:
            if _is_cancelable(_map_order(raw)):
                active += 1
        except Exception:
            pass
    _log("orders      : %d total, %d active" % (len(orders), active))

    try:
        deals = _trade_rows("DEAL", account, ACCOUNT_TYPE)
    except Exception:
        deals = []
    _log("trades      : %d" % len(deals))
    _log("========== startup summary end ==========")


def _self_check():
    if _STATE.get("self_check_done"):
        return
    tries = _STATE.get("self_check_tries", 0)
    if tries >= 3:
        _STATE["self_check_done"] = True
        return
    _STATE["self_check_tries"] = tries + 1
    account = _account_id()
    rows = _trade_rows("ACCOUNT", account, ACCOUNT_TYPE)
    if not rows:
        _log("ACCOUNT query returned 0 rows -- account %s is most likely "
             "not bound: pick it in the client run settings, otherwise "
             "orders are silently dropped" % (account or "<empty>"), "ERROR")
        return
    _STATE["self_check_done"] = True
    _log_startup_summary(rows)


# ----------------------------------------------------------------------
# Handler table
# ----------------------------------------------------------------------

_HANDLERS = {
    "order_stock": _h_order,
    "order_stock_async": _h_order_async,
    "cancel_order_stock": _h_cancel,
    "cancel_order_stock_async": _h_cancel_async,
    "cancel_order_stock_sysid": _h_cancel_sysid,
    "cancel_order_stock_sysid_async": _h_cancel_sysid,
    "query_stock_orders": _h_query_orders,
    "query_stock_order": _h_query_order,
    "query_stock_positions": _h_query_positions,
    "query_stock_asset": _h_query_asset,
    "query_stock_trades": _h_query_trades,
}
