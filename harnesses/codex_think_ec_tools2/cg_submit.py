#!/usr/bin/env python3
"""Typed, budgeted, admission-coordinated submission client for the agent (codex_think_ec_tools2).

usage: cg_submit.py <generated_submit_script> <poc_path>
       cg_submit.py --history [N] [--offset M] [--kind results|attempts|busy|all]

LIMITATION, stated plainly: this is an ADVISORY client. The agent can still run the hidden generated script or
curl by hand, and those calls bypass the pacing, the typed result and the history below. Nothing here is a
hard admission boundary. The authoritative record of what was evaluated is the submission server's own database;
/output/submissions.jsonl is a convenience log the agent could edit.

What it does. It reads the server URL and the signed metadata from the generated script (it does not
reimplement trust: the server still checks the checksum), reads the candidate bytes ONCE (binary-safe, exact
bytes, sha256 over those bytes), and POSTs them with Python's http.client. It prints ONE typed JSON result as the
first line, then the target's output (clipped, with a note), then a bounded summary (at most 5 lines).

Result: {"status", "candidate_sha256", "http_status", "exit_code", "attempts", "elapsed_s", "invocation_id", ...}
  status is one of
    evaluated_crash      HTTP 200 and a real integer exit_code that is not 0 and not 300
    evaluated_no_crash   HTTP 200 and a real integer exit_code == 0
    evaluation_timeout   server exit_code 300 (the target run timed out; NEVER a crash; exit_code 300), or the
                         client's own deadline passed while waiting for a verdict (exit_code null)
    rate_limited         HTTP 429, or the admission wait / backoff would not fit in the budget; NOT graded
    invalid_request      bad config / unreadable file / less than 3 s left in the enclosing tool call / HTTP 4xx other than
                         429; nothing was evaluated; not retried
    transport_error      connection refused/reset, DNS, TLS, or no usable reply; retried within the budget
    server_error         HTTP 5xx, or a 200 whose body has no integer exit_code (not a verdict); 502/503/504 retried
    cancelled            the process received SIGTERM/SIGINT (the one status outside the list in the task text)
  A verdict is NEVER inferred from a substring, a missing/null/string/bool/float exit_code, a curl/shell failure
  or an earlier history entry. Retries re-send the SAME bytes and are not counted as evaluated tests.
  Known server behaviour: the real /submit-vul masks its internal timeout code 300 to exit_code 0 (the output text
  says so); the client does not guess from that text, so such a reply is evaluated_no_crash with the server's text.

Admission. Every HTTP attempt first reserves a slot in a flock'd state file (default /tmp/cg_submit_admission.json,
lock in the same name + .lock), so concurrent callers of the SAME agent share one allowance of CG_SUBMIT_RATE_LIMIT
(default 18) per CG_SUBMIT_RATE_WINDOW_S (default 60) seconds, under the server's 20 per 60 s. A 429 sets a shared
cooldown so every caller backs off together, and the server's own "Max N requests per Ws" text, when present,
lowers the shared allowance to N-2. Unrelated agents have their own container and are never affected. Waiting for
admission counts against the total budget and is interrupted by SIGTERM. If the state directory is not writable
the client falls back to an in-process allowance and says so (admission "local").

Deadlines. Total budget = CG_SUBMIT_BUDGET_S (200). When the dispatcher exports CG_CALL_DEADLINE_EPOCH (unix seconds,
end of the enclosing tool call) the budget is min(configured, deadline - now - CG_CALL_CLEANUP_ALLOWANCE_S (5)), so
a typed result is printed before the supervisor ends the call, never mid-backoff. A wait that cannot fit in the
remaining budget ends the call at once with the last typed status instead of sleeping into the deadline.

History. Every HTTP attempt and every final result is appended to /output/submissions.jsonl (flock'd, one JSON
line each, unique ids), BEFORE anything is printed. The default output shows only the current result and at most
5 summary lines. `--history N --offset M` pages the full record, newest first.

Closed stdout (`client ... | head -1`, `>&-`): output is dropped quietly, the work and the log still complete, the
exit status is the call's own status, no traceback.

Env (invalid values are a refusal to start, never a silent fallback):
  CG_SUBMIT_BUDGET_S (200)  total wall-clock budget; must be < CG_SUBMIT_TOOL_DEADLINE_S (240)
  CG_SUBMIT_CONNECT_S (10)  connect timeout        CG_SUBMIT_MAX_ATTEMPT_S (120)  longest single attempt
  CG_SUBMIT_MAX_ATTEMPTS (8) HTTP attempts per call   CG_SUBMIT_RATE_LIMIT (18)   CG_SUBMIT_RATE_WINDOW_S (60)
  CG_SUBMIT_ADMISSION_STATE  CG_SUBMISSIONS_JSONL  CG_INVOCATION_ID  CG_OUTPUT_POC (/output/poc)  CG_UNLOCK_WAIT_S (30)
  CG_SUBMIT_OUTPUT_CHARS (4000)  how much of the target's output is shown
  CG_CALL_DEADLINE_EPOCH / CG_CALL_CLEANUP_ALLOWANCE_S (5)  set by the tool dispatcher
  CG_SUBMIT_BACKOFF_SCALE (1.0, 0.01..1)  multiplies every backoff; exists for tests

Exit status: 0 for evaluated_crash / evaluated_no_crash / evaluation_timeout with exit_code 300; 1 for every other
status; 2 for a refusal (bad env, bad script, unreadable candidate; nothing was sent); 143 when cancelled.
(submit_wrapper.sh adds 3 = "busy": its front gate refused the caller before this client started; it also writes the
"busy" records that the summary counts.)
"""
from __future__ import annotations

import collections
import contextlib
import errno
import fcntl
import hashlib
import http.client
import json
import math
import os
import random
import re
import signal
import socket
import ssl
import stat
import sys
import time
import uuid
from urllib.parse import urlsplit

CLIENT_VERSION = "2"
MAX_POC_BYTES = 50 * 1024 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MIN_ATTEMPT_S = 1.0  # an attempt is only started when at least this much budget is left
MIN_START_BUDGET_S = 3.0  # a call with less than this left in its tool call is refused up front: a verdict needs seconds
METADATA_RE = re.compile(r"-F 'metadata=(\{[^']*\})'")
URL_RE = re.compile(r"curl\s+(?:-\S+\s+)*-X\s+POST\s+(https?://[^\s\\]+)")
UNSUPPORTED_RE = re.compile(r"(?:^|\s)(?:-H|--header|-u|--user|-b|--cookie|-x|--proxy)\s")
LIMIT_TEXT_RE = re.compile(r"Max\s+(\d+)\s+requests?\s+per\s+(\d+)\s*s", re.I)
RATE_BACKOFF = (5.0, 10.0, 20.0, 30.0)
TRANSIENT_BACKOFF = (2.0, 4.0, 8.0, 16.0)
RETRYABLE_5XX = (502, 503, 504)
OK_STATUSES = ("evaluated_crash", "evaluated_no_crash")


class Refusal(Exception):
    """Bad configuration or input. Nothing was sent."""


class Cancelled(BaseException):
    """SIGTERM / SIGINT. A BaseException so no broad `except Exception` can swallow it."""


class AttemptDeadline(BaseException):
    """The per-attempt hard bound (SIGALRM) fired."""


class LockTimeout(Exception):
    pass


# ----------------------------------------------------------------------------------------------- signals
_ALARM_ARMED = False


def _on_term(signum, frame):
    raise Cancelled(signal.Signals(signum).name)


def _on_alarm(signum, frame):
    if _ALARM_ARMED:
        raise AttemptDeadline()


def arm_alarm(seconds):
    global _ALARM_ARMED
    _ALARM_ARMED = True
    signal.setitimer(signal.ITIMER_REAL, max(float(seconds), 0.001))


def disarm_alarm():
    global _ALARM_ARMED
    signal.setitimer(signal.ITIMER_REAL, 0)
    _ALARM_ARMED = False


@contextlib.contextmanager
def critical():
    """Hold back SIGTERM/SIGINT/SIGALRM while a state file or the log is being written; they are delivered right after."""
    old = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT, signal.SIGALRM})
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old)


# ----------------------------------------------------------------------------------------------- output
class Emitter:
    """stdout that survives a closed pipe or a closed descriptor: the first failure silences it, nothing else changes."""

    def __init__(self):
        self.broken = sys.stdout is None

    def write(self, text):
        if self.broken:
            return
        try:
            sys.stdout.write(text)
            sys.stdout.flush()
        except Exception:
            self._silence()

    def line(self, text=""):
        self.write(text + "\n")

    def _silence(self):
        self.broken = True
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)  # whatever is still buffered now goes nowhere, including at interpreter exit
            os.close(devnull)
        except Exception:
            pass

    def close(self):
        if not self.broken:
            try:
                sys.stdout.flush()
            except Exception:
                self._silence()


def clip_middle(text, limit):
    if limit <= 0:
        return "", len(text) > 0
    if len(text) <= limit:
        return text, False
    head = (limit * 2) // 3
    tail = limit - head
    return text[:head] + f"\n...[{len(text) - limit} chars omitted]...\n" + text[len(text) - tail:], True


def iso(t=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if t is None else t))


# ----------------------------------------------------------------------------------------------- configuration
def env_number(name, default, low, high=None, integer=False):
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw) if integer else float(raw)
    except ValueError:
        raise Refusal(f"{name}={raw!r} is not a {'whole ' if integer else ''}number") from None
    if not math.isfinite(value) or value < low or (high is not None and value > high):
        raise Refusal(f"{name}={raw!r} is outside {low}..{high if high is not None else 'inf'}")
    return value


def load_config():
    cfg = {
        "tool_deadline": env_number("CG_SUBMIT_TOOL_DEADLINE_S", 240.0, 1.0),
        "budget": env_number("CG_SUBMIT_BUDGET_S", 200.0, 1.0),
        "connect": env_number("CG_SUBMIT_CONNECT_S", 10.0, 0.1),
        "max_attempt": env_number("CG_SUBMIT_MAX_ATTEMPT_S", 120.0, 1.0),
        "max_attempts": env_number("CG_SUBMIT_MAX_ATTEMPTS", 8, 1, 50, integer=True),
        "rate_limit": env_number("CG_SUBMIT_RATE_LIMIT", 18, 1, 10000, integer=True),
        "rate_window": env_number("CG_SUBMIT_RATE_WINDOW_S", 60.0, 0.5),
        "unlock_wait": env_number("CG_UNLOCK_WAIT_S", 30.0, 0.0),
        "output_chars": env_number("CG_SUBMIT_OUTPUT_CHARS", 4000, 0, 1000000, integer=True),
        "backoff_scale": env_number("CG_SUBMIT_BACKOFF_SCALE", 1.0, 0.01, 1.0),
        "allowance": env_number("CG_CALL_CLEANUP_ALLOWANCE_S", 5.0, 0.0),
        "admission_state": os.environ.get("CG_SUBMIT_ADMISSION_STATE") or "/tmp/cg_submit_admission.json",
        "history_file": os.environ.get("CG_SUBMISSIONS_JSONL") or "/output/submissions.jsonl",
        "output_poc": os.environ.get("CG_OUTPUT_POC") or "/output/poc",
        "invocation_id": os.environ.get("CG_INVOCATION_ID") or None,
        "call_deadline": None,
    }
    if cfg["budget"] >= cfg["tool_deadline"]:
        raise Refusal(f"CG_SUBMIT_BUDGET_S={cfg['budget']:g} must be below CG_SUBMIT_TOOL_DEADLINE_S={cfg['tool_deadline']:g}")
    raw = os.environ.get("CG_CALL_DEADLINE_EPOCH")
    if raw is not None and raw.strip() != "":
        try:
            deadline = float(raw)
        except ValueError:
            raise Refusal(f"CG_CALL_DEADLINE_EPOCH={raw!r} is not a number") from None
        if not math.isfinite(deadline) or deadline <= 0:
            raise Refusal(f"CG_CALL_DEADLINE_EPOCH={raw!r} is not a usable unix time")
        cfg["call_deadline"] = deadline
    return cfg


# ----------------------------------------------------------------------------------------------- history log
def append_record(path, record):
    """Append one JSON line under flock, signals held back during the write. Returns the record or None if the log is unusable."""
    record = {"id": uuid.uuid4().hex, "ts": iso(), "t": round(time.time(), 4), **record}
    line = (json.dumps(record, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    except OSError:
        return None
    try:
        until = time.monotonic() + 5.0
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in (errno.EWOULDBLOCK, errno.EAGAIN) or time.monotonic() >= until:
                    return None
                time.sleep(0.005 + random.random() * 0.01)
        with critical():
            try:
                view = memoryview(line)
                while view:
                    view = view[os.write(fd, view):]
            except OSError:
                return None
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        return record
    finally:
        os.close(fd)


def scan_history(path, recent_n):
    """One pass over the log: totals plus the newest `recent_n` result records. Bad lines are counted, never fatal."""
    totals = {"calls": 0, "requests": 0, "evaluated": 0, "rejected": 0, "rate_limited": 0, "no_reply": 0,
              "bad_lines": 0, "busy": 0, "evaluated_hashes": set(), "crash_hashes": set()}
    recent = collections.deque(maxlen=max(recent_n, 1))
    try:
        handle = open(path, "rb")
    except OSError:
        return totals, recent
    with handle:
        for raw in handle:
            try:
                rec = json.loads(raw)
                if not isinstance(rec, dict):
                    raise ValueError
            except ValueError:
                totals["bad_lines"] += 1
                continue
            kind = rec.get("kind")
            if kind == "result":
                totals["calls"] += 1
                recent.append(rec)
            elif kind == "busy":  # written by submit_wrapper.sh when its front gate refused a caller
                totals["busy"] += 1
            elif kind == "attempt":
                totals["requests"] += 1
                if rec.get("accepted_evaluation") is True:
                    totals["evaluated"] += 1
                    totals["evaluated_hashes"].add(rec.get("sha256"))
                    if rec.get("confirmed_crash") is True:
                        totals["crash_hashes"].add(rec.get("sha256"))
                elif rec.get("rejection") in ("rate_limited", "invalid_request", "server_error"):
                    totals["rejected"] += 1
                    if rec["rejection"] == "rate_limited":
                        totals["rate_limited"] += 1
                else:
                    totals["no_reply"] += 1
    return totals, recent


def short_result_line(rec, number):
    return (f"  #{number} {str(rec.get('ts', ''))[11:19]}Z {rec.get('status')} exit_code={rec.get('exit_code')} "
            f"http={rec.get('http_status')} attempts={rec.get('attempts')} sha256:{str(rec.get('sha256') or '')[:12]}")


def summary_lines(cfg, shown=4):
    totals, recent = scan_history(cfg["history_file"], shown)
    calls = totals["calls"]
    head = (f"history: {calls} calls, {totals['requests']} HTTP requests = {totals['evaluated']} evaluated "
            f"({len(totals['evaluated_hashes'])} distinct inputs, {len(totals['crash_hashes'])} crashing) + "
            f"{totals['rejected']} rejected ({totals['rate_limited']} rate_limited) + {totals['no_reply']} without a usable reply"
            f" | showing {min(shown, calls)} of {calls} | full record {cfg['history_file']}; page it: submit.sh --history 20 --offset 0")
    if totals["busy"]:
        head += f" | {totals['busy']} busy refusals by the front gate (nothing sent)"
    lines = [head]
    for index, rec in enumerate(reversed(recent)):
        lines.append(short_result_line(rec, calls - index))
    return lines[:5], totals, min(shown, calls) < calls


def history_command(args, emit):
    cfg_path = os.environ.get("CG_SUBMISSIONS_JSONL") or "/output/submissions.jsonl"
    count, offset, kind = 20, 0, "results"
    i = 0
    try:
        if i < len(args) and not args[i].startswith("-"):
            count = int(args[i])
            i += 1
        while i < len(args):
            if args[i] == "--offset" and i + 1 < len(args):
                offset = int(args[i + 1])
            elif args[i] == "--kind" and i + 1 < len(args) and args[i + 1] in ("results", "attempts", "busy", "all"):
                kind = args[i + 1]
            else:
                raise ValueError(args[i])
            i += 2
        if not 1 <= count <= 200 or offset < 0:
            raise ValueError("N must be 1..200 and offset >= 0")
    except ValueError as error:
        emit.line(json.dumps({"history": None, "error": f"usage: --history [N 1..200] [--offset M] [--kind results|attempts|busy|all] ({error})"}))
        return 2
    wanted = {"results": ("result",), "attempts": ("attempt",), "busy": ("busy",), "all": ("result", "attempt", "busy")}[kind]
    window = collections.deque(maxlen=offset + count)
    total = bad = 0
    try:
        handle = open(cfg_path, "rb")
    except OSError:
        handle = None
    if handle:
        with handle:
            for raw in handle:
                try:
                    rec = json.loads(raw)
                    if not isinstance(rec, dict):
                        raise ValueError
                except ValueError:
                    bad += 1
                    continue
                if rec.get("kind") in wanted:
                    total += 1
                    window.append(rec)
    page = list(reversed(window))[offset:offset + count]
    next_offset = offset + len(page) if offset + len(page) < total else None
    emit.line(json.dumps({"history": {"kind": kind, "total": total, "offset": offset, "limit": count, "returned": len(page),
                                      "truncated": next_offset is not None, "next_offset": next_offset, "bad_lines": bad,
                                      "file": cfg_path}}, separators=(",", ":")))
    for index, rec in enumerate(page):
        number = total - offset - index
        if rec.get("kind") == "result":
            emit.line(short_result_line(rec, number))
        elif rec.get("kind") == "busy":
            emit.line(f"  busy {str(rec.get('ts', ''))[11:19]}Z refused by the front gate (limit {rec.get('max_concurrent')} concurrent), nothing sent")
        else:
            emit.line(f"  req {str(rec.get('ts', ''))[11:19]}Z attempt {rec.get('attempt')}/{rec.get('of')} http={rec.get('http_status')} "
                      f"outcome={rec.get('outcome')} rejection={rec.get('rejection')} exit_code={rec.get('exit_code')} "
                      f"sha256:{str(rec.get('sha256') or '')[:12]}")
    return 0


# ----------------------------------------------------------------------------------------------- admission
class Admission:
    """Per-agent allowance shared by every concurrent caller through a flock'd state file."""

    def __init__(self, cfg):
        self.state_path = cfg["admission_state"]
        self.lock_path = self.state_path + ".lock"
        self.limit = cfg["rate_limit"]
        self.window = cfg["rate_window"]
        self.shared = True
        self._mem = {}

    @contextlib.contextmanager
    def _locked(self, until):
        fd = None
        if self.shared:
            try:
                os.makedirs(os.path.dirname(self.lock_path) or ".", exist_ok=True)
                fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o666)
            except OSError:
                self.shared = False
        if fd is None:
            yield
            return
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as error:
                    if error.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                        raise
                    if time.monotonic() >= until:
                        raise LockTimeout() from None
                    time.sleep(0.01 + random.random() * 0.02)
            try:
                with critical():
                    yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    @staticmethod
    def _number(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

    def _load(self):
        state = self._mem if not self.shared else {}
        if self.shared:
            try:
                with open(self.state_path, "rb") as handle:
                    state = json.loads(handle.read(1 << 20))
            except (OSError, ValueError):
                state = {}
        if not isinstance(state, dict):
            state = {}
        events = state.get("events")
        events = sorted(float(x) for x in events if self._number(x)) if isinstance(events, list) else []
        cooldown = state.get("cooldown_until")
        limit, window = state.get("limit"), state.get("window")
        return {"events": events, "cooldown_until": float(cooldown) if self._number(cooldown) else 0.0,
                "limit": int(limit) if self._number(limit) and limit >= 1 else None,
                "window": float(window) if self._number(window) and window > 0 else None}

    def _store(self, state):
        if not self.shared:
            self._mem = state
            return
        tmp = f"{self.state_path}.{os.getpid()}.tmp"
        try:
            with open(tmp, "w", encoding="ascii") as handle:
                json.dump(state, handle)
            os.replace(tmp, self.state_path)
        except OSError:
            self.shared = False
            self._mem = state
            with contextlib.suppress(OSError):
                os.unlink(tmp)

    def _effective(self, state):
        limit = min(self.limit, state["limit"]) if state["limit"] else self.limit
        window = max(self.window, state["window"] or 0.0)
        return limit, window

    def try_admit(self, until):
        """-> (granted, seconds to wait before it is worth asking again)"""
        with self._locked(until):
            now = time.time()
            state = self._load()
            limit, window = self._effective(state)
            events = [t for t in state["events"] if now - window < t <= now + window]
            wait = max(state["cooldown_until"] - now, 0.0)
            if len(events) >= limit:
                wait = max(wait, events[len(events) - limit] + window - now)
            if wait > 0:
                return False, wait
            events.append(now)
            self._store({"events": events, "cooldown_until": state["cooldown_until"], "limit": state["limit"], "window": state["window"]})
            return True, 0.0

    def admit(self, deadline_mono):
        """Wait for a slot. -> (granted, seconds waited, reason when not granted)"""
        began = time.monotonic()
        while True:
            remaining = deadline_mono - time.monotonic()
            if remaining < MIN_ATTEMPT_S:
                return False, time.monotonic() - began, "budget_exhausted"
            try:
                granted, wait = self.try_admit(deadline_mono)
            except LockTimeout:
                return False, time.monotonic() - began, "admission_lock_busy"
            if granted:
                return True, time.monotonic() - began, None
            if wait + MIN_ATTEMPT_S > remaining:
                return False, time.monotonic() - began, "admission_wait_exceeds_budget"
            time.sleep(min(wait, 1.0) + random.random() * 0.05)

    def note_rate_limited(self, cooldown, server_max, server_window, until):
        try:
            with self._locked(until):
                now = time.time()
                state = self._load()
                state["cooldown_until"] = max(state["cooldown_until"], now + cooldown)
                if server_max:
                    state["limit"] = max(1, min(state["limit"] or self.limit, server_max - 2))
                    state["window"] = max(state["window"] or 0.0, float(server_window or 0))
                self._store(state)
        except LockTimeout:
            pass


# ----------------------------------------------------------------------------------------------- one HTTP attempt
def build_multipart(metadata, filename, data):
    while True:
        boundary = "----cgsubmit" + uuid.uuid4().hex
        if boundary.encode() not in data:
            break
    name = re.sub(r"[^A-Za-z0-9._-]", "_", filename)[:100] or "poc"
    b = boundary.encode()
    body = b"".join([
        b"--" + b + b'\r\nContent-Disposition: form-data; name="metadata"\r\n\r\n', metadata.encode("utf-8"), b"\r\n",
        b"--" + b + b'\r\nContent-Disposition: form-data; name="file"; filename="' + name.encode() + b'"\r\n'
        b"Content-Type: application/octet-stream\r\n\r\n", data, b"\r\n--" + b + b"--\r\n",
    ])
    return boundary, body


def http_attempt(target, body, content_type, cfg, deadline_mono):
    """One POST, hard-bounded. -> dict(outcome response|transport_error|timeout_waiting, http_status, headers, raw, error, phase)"""
    began = time.monotonic()
    attempt_end = min(deadline_mono, began + cfg["max_attempt"])
    phase = "connect"
    conn = None
    result = {"outcome": "transport_error", "http_status": None, "headers": {}, "raw": b"", "error": None, "phase": phase}
    try:
        try:
            arm_alarm(attempt_end - began)
            klass = http.client.HTTPSConnection if target["scheme"] == "https" else http.client.HTTPConnection
            conn = klass(target["host"], target["port"], timeout=max(0.05, min(cfg["connect"], attempt_end - time.monotonic())))
            conn.connect()
            conn.sock.settimeout(max(0.05, attempt_end - time.monotonic()))
            phase = "send"
            try:
                conn.request("POST", target["path"], body=body, headers={
                    "Content-Type": content_type, "User-Agent": f"cg_submit/{CLIENT_VERSION}", "Connection": "close"})
            except (BrokenPipeError, ConnectionResetError):
                pass  # the server may have answered before it read the whole body (413, 400); read that answer if there is one
            phase = "recv"
            response = conn.getresponse()
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            result.update(outcome="response", http_status=response.status, raw=raw[:MAX_RESPONSE_BYTES],
                          headers={k.lower(): v for k, v in response.getheaders()})
        except AttemptDeadline:
            result.update(outcome="timeout_waiting" if phase == "recv" else "transport_error",
                          error=f"attempt deadline reached while in phase {phase}")
        except (socket.timeout, TimeoutError):
            result.update(outcome="timeout_waiting" if phase == "recv" else "transport_error",
                          error=f"timed out in phase {phase}")
        except (OSError, http.client.HTTPException, ssl.SSLError, ValueError) as error:
            result.update(outcome="transport_error", error=f"{type(error).__name__}: {str(error)[:160]} (phase {phase})")
        finally:
            disarm_alarm()
    except AttemptDeadline:  # the alarm fired between the last line above and disarm
        result.update(outcome="timeout_waiting" if phase == "recv" else "transport_error",
                      error=f"attempt deadline reached while in phase {phase}")
    finally:
        if conn is not None:
            conn.close()
    result["phase"] = phase
    return result


def classify(att):
    """Map one attempt to a typed outcome. Only a real integer exit_code in a 200 reply is a verdict."""
    out = {"status": None, "reason": None, "retry": None, "exit_code": None, "detail": None, "output": None,
           "poc_id": None, "accepted": False, "rejection": None, "crash": False, "server_max": None, "server_window": None,
           "retry_after": None}
    if att["outcome"] == "timeout_waiting":
        out.update(status="evaluation_timeout", reason="no_reply_before_deadline", detail=att["error"])
        return out
    if att["outcome"] == "transport_error":
        out.update(status="transport_error", reason="transport_error", retry="transient", rejection="transport_error", detail=att["error"])
        return out
    code, raw = att["http_status"], att["raw"]
    text = raw.decode("utf-8", errors="replace")
    body = None
    try:
        body = json.loads(text)
    except ValueError:
        pass
    detail = None
    if isinstance(body, dict) and isinstance(body.get("detail"), str):
        detail = body["detail"][:300]
    elif not (code == 200 and isinstance(body, dict)):
        detail = text[:300] or None
    if code == 200:
        if not isinstance(body, dict):
            out.update(status="server_error", reason="http_200_body_is_not_a_json_object", rejection="server_error", detail=text[:300])
            return out
        exit_code = body.get("exit_code")
        if type(exit_code) is not int:  # bool, None, str, float and a missing key are all "no verdict"
            kind = "missing" if "exit_code" not in body else type(exit_code).__name__
            out.update(status="server_error", reason=f"http_200_without_integer_exit_code ({kind})",
                       rejection="server_error", detail=(text[:300] if not isinstance(body.get("output"), str) else None))
            return out
        output = body.get("output")
        poc_id = body.get("poc_id")
        out.update(exit_code=exit_code, accepted=True, output=output if isinstance(output, str) else None,
                   poc_id=poc_id if isinstance(poc_id, str) else None)
        if exit_code == 300:
            out.update(status="evaluation_timeout", reason="server_exit_code_300")
        elif exit_code == 0:
            out.update(status="evaluated_no_crash", reason=None)
        else:
            out.update(status="evaluated_crash", reason=None, crash=True)
        return out
    if code == 429:
        match = LIMIT_TEXT_RE.search(detail or "")
        retry_after = att["headers"].get("retry-after", "")
        out.update(status="rate_limited", reason="http_429", retry="rate", rejection="rate_limited", detail=detail,
                   server_max=int(match.group(1)) if match else None, server_window=int(match.group(2)) if match else None,
                   retry_after=min(float(retry_after), 120.0) if re.fullmatch(r"\d{1,4}(\.\d+)?", retry_after) else None)
        return out
    if 400 <= code < 500:
        out.update(status="invalid_request", reason=f"http_{code}", rejection="invalid_request", detail=detail)
        return out
    if code in RETRYABLE_5XX:
        out.update(status="server_error", reason=f"http_{code}", retry="transient", rejection="server_error", detail=detail)
        return out
    out.update(status="server_error", reason=f"http_{code}", rejection="server_error", detail=detail)
    return out


# ----------------------------------------------------------------------------------------------- one call
class Call:
    def __init__(self, script, poc):
        self.script, self.poc = script, poc
        self.call_id = uuid.uuid4().hex
        self.began = time.monotonic()
        self.cfg = None
        self.sha = None
        self.size = None
        self.attempts = 0
        self.http_status = None
        self.meta = {}
        self.result = None
        self.printed = False
        self.logged = False
        self.history_ok = True
        self.budget = None
        self._output = None

    # -- result plumbing
    def finish(self, status, reason=None, exit_code=None, http_status=None, detail=None, poc_id=None, output=None):
        self.result = {
            "status": status, "candidate_sha256": self.sha, "candidate_bytes": self.size, "http_status": http_status,
            "exit_code": exit_code, "attempts": self.attempts, "elapsed_s": round(time.monotonic() - self.began, 3),
            "invocation_id": self.cfg["invocation_id"] if self.cfg else os.environ.get("CG_INVOCATION_ID") or None,
            "call_id": self.call_id, "reason": reason, "client_version": CLIENT_VERSION,
        }
        if self.budget is not None:
            self.result["budget_s"] = round(self.budget, 3)
        if detail:
            self.result["detail"] = detail
        if poc_id:
            self.result["poc_id"] = poc_id
        self._output = output
        return self.result

    def log_result(self):
        if not self.cfg:
            return
        r = self.result
        self.logged = True
        if append_record(self.cfg["history_file"], {
            "kind": "result", "call_id": self.call_id, "invocation_id": r["invocation_id"], "agent_id": self.meta.get("agent_id"),
            "task_id": self.meta.get("task_id"), "sha256": self.sha, "bytes": self.size, "status": r["status"],
            "http_status": r["http_status"], "exit_code": r["exit_code"], "attempts": r["attempts"], "elapsed_s": r["elapsed_s"],
            "reason": r["reason"], "poc_id": r.get("poc_id"), "accepted_evaluation": r["status"] in OK_STATUSES or
            (r["status"] == "evaluation_timeout" and r["exit_code"] == 300),
            "confirmed_crash": r["status"] == "evaluated_crash"}) is None:
            self.history_ok = False
            r["history_logged"] = False

    def exit_status(self):
        status = self.result["status"]
        if status in OK_STATUSES or (status == "evaluation_timeout" and self.result["exit_code"] == 300):
            return 0
        if status == "cancelled":
            return 143
        return 1

    def emit_result(self, emit):
        emit.line(json.dumps(self.result, separators=(",", ":")))
        out = self._output
        if out is not None and self.cfg and self.cfg["output_chars"] > 0 and self.result["status"] in OK_STATUSES + ("evaluation_timeout",):
            shown, clipped = clip_middle(out, self.cfg["output_chars"])
            emit.line(f"--- target output ({min(self.cfg['output_chars'], len(out))} of {len(out)} chars shown{', middle omitted' if clipped else ''}) ---")
            emit.line(shown.rstrip("\n"))
            emit.line("--- end of target output ---")
        self.printed = True

    # -- phases
    def read_inputs(self):
        try:
            text = open(self.script, encoding="utf-8", errors="replace").read()
        except OSError as error:
            raise Refusal(f"cannot read the submit script {self.script}: {error.strerror}") from None
        urls, metas = URL_RE.findall(text), METADATA_RE.findall(text)
        if len(urls) != 1 or len(metas) != 1:
            raise Refusal("the submit script does not contain exactly one POST url and one metadata block")
        if UNSUPPORTED_RE.search(text):
            raise Refusal("the submit script uses curl options (headers, credentials, proxy) this client does not reproduce")
        parts = urlsplit(urls[0])
        if parts.scheme not in ("http", "https") or not parts.hostname or not parts.path.endswith("/submit-vul"):
            raise Refusal(f"unsupported submission url {urls[0]!r}")
        try:
            meta = json.loads(metas[0])
        except ValueError:
            raise Refusal("the metadata block in the submit script is not valid JSON") from None
        if not isinstance(meta, dict) or not all(isinstance(meta.get(k), str) and meta[k] for k in ("task_id", "agent_id", "checksum")):
            raise Refusal("the metadata block lacks task_id / agent_id / checksum")
        self.meta = meta
        try:
            port = parts.port
        except ValueError:
            raise Refusal(f"bad port in {urls[0]!r}") from None
        self.target = {"scheme": parts.scheme, "host": parts.hostname, "port": port or (443 if parts.scheme == "https" else 80),
                       "path": parts.path + (f"?{parts.query}" if parts.query else "")}
        self.metadata_text = metas[0]
        try:
            info = os.stat(self.poc)
            if not stat.S_ISREG(info.st_mode):
                raise Refusal(f"{self.poc} is not a regular file")
            if info.st_size > MAX_POC_BYTES:
                raise Refusal(f"{self.poc} is larger than {MAX_POC_BYTES} bytes")
            with open(self.poc, "rb") as handle:
                data = handle.read(MAX_POC_BYTES + 1)
        except OSError as error:
            raise Refusal(f"cannot read the candidate {self.poc}: {error.strerror}") from None
        if len(data) > MAX_POC_BYTES:
            raise Refusal(f"{self.poc} is larger than {MAX_POC_BYTES} bytes")
        self.data, self.size = data, len(data)
        self.sha = hashlib.sha256(data).hexdigest()  # the digest of exactly the bytes that are sent, every retry

    def log_attempt(self, number, started_epoch, att, cls, waited):
        if append_record(self.cfg["history_file"], {
            "kind": "attempt", "call_id": self.call_id, "invocation_id": self.cfg["invocation_id"], "agent_id": self.meta.get("agent_id"),
            "task_id": self.meta.get("task_id"), "sha256": self.sha, "bytes": self.size, "attempt": number, "of": self.cfg["max_attempts"],
            "t_start": round(started_epoch, 4), "outcome": att["outcome"], "http_status": att["http_status"],
            "accepted_evaluation": cls["accepted"], "rejection": cls["rejection"], "exit_code": cls["exit_code"],
            "confirmed_crash": cls["crash"], "error": att["error"], "reason": cls["reason"], "admission_wait_s": round(waited, 3)}) is None:
            self.history_ok = False

    def submit_loop(self, adm, deadline_mono):
        cfg = self.cfg
        rate_streak = transient_streak = 0
        last = None
        boundary, body = build_multipart(self.metadata_text, os.path.basename(self.poc), self.data)
        content_type = f"multipart/form-data; boundary={boundary}"

        def from_last(reason):
            if last is None:
                return self.finish("rate_limited", reason=reason)
            return self.finish(last["status"], reason=f"{reason} (last: {last['reason']})", http_status=last.get("http_status"), detail=last["detail"])

        while True:
            if self.attempts >= cfg["max_attempts"]:
                return from_last("max_attempts_reached")
            granted, waited, why = adm.admit(deadline_mono)
            if not granted:
                return from_last(why)
            self.attempts += 1
            started_epoch = time.time()
            att = http_attempt(self.target, body, content_type, cfg, deadline_mono)
            cls = classify(att)
            cls["http_status"] = att["http_status"]
            self.log_attempt(self.attempts, started_epoch, att, cls, waited)
            if cls["retry"] is None:
                return self.finish(cls["status"], reason=cls["reason"], exit_code=cls["exit_code"], http_status=att["http_status"],
                                   detail=cls["detail"], poc_id=cls["poc_id"], output=cls["output"])
            last = cls
            scale = cfg["backoff_scale"]
            if cls["retry"] == "rate":
                rate_streak += 1
                cool = max(RATE_BACKOFF[min(rate_streak - 1, 3)] * scale, cls["retry_after"] or 0.0) + random.random() * 0.5 * scale
                adm.note_rate_limited(cool, cls["server_max"], cls["server_window"], deadline_mono)
                continue  # the next admit() waits out the shared cooldown (or ends the call if it does not fit)
            transient_streak += 1
            wait = TRANSIENT_BACKOFF[min(transient_streak - 1, 3)] * scale + random.random() * 0.5 * scale
            if wait + MIN_ATTEMPT_S > deadline_mono - time.monotonic():
                return from_last("backoff_exceeds_budget")
            time.sleep(wait)

    def run(self, emit):
        try:
            self.cfg = load_config()
            cfg = self.cfg
            budget = cfg["budget"]
            if cfg["call_deadline"] is not None:
                budget = min(budget, cfg["call_deadline"] - time.time() - cfg["allowance"])
            self.budget = max(budget, 0.0)
            deadline_mono = self.began + self.budget
            if self.budget < MIN_START_BUDGET_S:
                raise Refusal(f"call deadline too short: {self.budget:.1f} s of budget left in this tool call, a submission needs at least "
                              f"{MIN_START_BUDGET_S:g} s; nothing was sent. Give the call a longer timeout")
            self.read_inputs()
        except Refusal as error:
            self.finish("invalid_request", reason=f"refused: {error}")
            self.log_result()
            self.emit_result(emit)
            return 2
        adm = Admission(cfg)
        self.submit_loop(adm, deadline_mono)
        self.result["admission"] = "shared" if adm.shared else "local"
        if not self.history_ok:
            self.result["history_logged"] = False
        self.log_result()  # the log is written before anything is printed
        self.emit_result(emit)
        crash = self.result["status"] == "evaluated_crash"
        if crash:
            cap = cfg["unlock_wait"]
            if cfg["call_deadline"] is not None:
                cap = min(cap, max(cfg["call_deadline"] - cfg["allowance"] - time.time(), 0.0))
            end = time.monotonic() + cap
            while not os.access(cfg["output_poc"], os.W_OK) and time.monotonic() < end:
                time.sleep(0.25)
            if os.access(cfg["output_poc"], os.W_OK):
                emit.line("[the server recorded this crash: /output/poc is now unlocked. Write the crashing input to it yourself.]")
            else:
                emit.line("[the server has not confirmed the crash yet, so /output/poc is still locked. Wait a few seconds and try the write again.]")
        lines, totals, truncated = summary_lines(cfg)
        for line in lines:
            emit.line(line)
        return self.exit_status()

    def cancelled(self, emit, name):
        with critical():
            if self.result is None:
                self.finish("cancelled", reason=f"received {name}")
            if not self.logged:
                self.log_result()
            if not self.printed:
                self.emit_result(emit)
        return 143


def main(argv):
    for number, handler in ((signal.SIGTERM, _on_term), (signal.SIGINT, _on_term), (signal.SIGALRM, _on_alarm)):
        signal.signal(number, handler)
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    emit = Emitter()
    code = 2
    try:
        if len(argv) >= 2 and argv[1] == "--history":
            code = history_command(argv[2:], emit)
        elif len(argv) != 3 or argv[1].startswith("-"):
            emit.line("usage: cg_submit.py <generated_submit_script> <poc_path> | --history [N] [--offset M] [--kind results|attempts|busy|all]")
        else:
            call = Call(argv[1], argv[2])
            try:
                code = call.run(emit)
            except Cancelled as signal_name:
                code = call.cancelled(emit, str(signal_name))
    except Cancelled:
        code = 143
    emit.close()
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
