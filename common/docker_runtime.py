from __future__ import annotations

import importlib
import inspect
import json
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HARNESS_DIR = Path("/harness")
MAX_OUTPUT_CHARS = 20_000
_CAPTURE_BYTES = MAX_OUTPUT_CHARS - 100


def _workspace_path() -> Path:
    return Path(os.environ.get("CYBERGYM_WORKSPACE") or "/workspace")


def _output_path() -> Path:
    return Path(os.environ.get("CYBERGYM_OUTPUT") or "/output")


@dataclass(frozen=True)
class Task:
    id: str = ""
    difficulty: str = "level2"
    time_limit_seconds: int = 0
    deadline: float = float("inf")

    def seconds_left(self) -> float:
        return self.deadline - time.time()


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    output: str
    timed_out: bool

    def __str__(self) -> str:
        return self.output if self.exit_code == 0 else f"[exit code {self.exit_code}]\n{self.output}"


@dataclass(frozen=True)
class ModelAccess:
    base_url: str
    api_key: str
    model: str

    def dspy(self, **kwargs: Any) -> Any:
        import dspy

        kwargs["cache"] = False
        return dspy.LM(
            f"openai/{self.model}",
            api_base=self.base_url,
            api_key=self.api_key,
            **kwargs,
        )


class Trajectory:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else _output_path() / "trajectory.jsonl"
        self.started = time.monotonic()
        self._lock = threading.Lock()

    def log(self, kind: str, **fields: Any) -> None:
        record = {
            "timestamp": time.time(),
            "t": round(time.monotonic() - self.started, 3),
            "type": kind,
            **fields,
        }
        line = json.dumps(record, ensure_ascii=False, default=str, separators=(",", ":"))
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")


class _BoundedCapture:
    """Keep the beginning and end of command output without buffering it all in RAM."""

    def __init__(self, limit: int = _CAPTURE_BYTES):
        self.limit = limit
        self.head_limit = limit // 2
        self.tail_limit = limit - self.head_limit
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def append(self, chunk: bytes) -> None:
        self.total += len(chunk)
        head_space = self.head_limit - len(self.head)
        if head_space > 0:
            self.head.extend(chunk[:head_space])
            chunk = chunk[head_space:]
        if chunk:
            self.tail.extend(chunk)
            excess = len(self.tail) - self.tail_limit
            if excess > 0:
                del self.tail[:excess]

    def text(self) -> str:
        if self.total <= self.limit:
            return bytes(self.head + self.tail).decode("utf-8", errors="replace")
        kept = len(self.head) + len(self.tail)
        marker = f"\n...[{self.total - kept} bytes omitted]...\n"
        return self.head.decode("utf-8", errors="replace") + marker + self.tail.decode("utf-8", errors="replace")


def _drain_output(stream: Any, capture: _BoundedCapture) -> None:
    try:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                return
            capture.append(chunk)
    except (OSError, ValueError):
        return


def _kill_process_group(process: subprocess.Popen[Any]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class Box:
    """Local command/file access for an agent running inside the harness container."""

    def __init__(
        self,
        task: Task | None = None,
        trajectory: Trajectory | None = None,
        workspace: str | Path | None = None,
        output: str | Path | None = None,
    ):
        self.task = task
        self.workspace = Path(workspace) if workspace is not None else _workspace_path()
        self.output = Path(output) if output is not None else _output_path()
        self.trajectory = trajectory or Trajectory(self.output / "trajectory.jsonl")
        self.commands = 0
        self.command_timeouts = 0
        self._count_lock = threading.Lock()

    def _path(self, path: str | Path) -> Path:
        target = Path(path)
        if not target.is_absolute():
            return self.workspace / target
        for default, override in ((Path("/workspace"), self.workspace), (Path("/output"), self.output)):
            try:
                return override / target.relative_to(default)
            except ValueError:
                continue
        return target

    def _record_command(self, **fields: Any) -> None:
        with self._count_lock:
            self.commands += 1
            if fields.get("timed_out"):
                self.command_timeouts += 1
            number = self.commands
        self.trajectory.log("command", command_number=number, **fields)

    @staticmethod
    def _timeout_value(timeout: float | int | None) -> float:
        if timeout is None:
            return math.inf
        value = float(timeout)
        if math.isnan(value) or value < 0:
            raise ValueError("timeout must be a non-negative number or None")
        return value

    def run(
        self,
        command: str,
        timeout: float | int | None = 300,
        workdir: str | Path = "/workspace",
    ) -> CommandResult:
        started = time.monotonic()
        process: subprocess.Popen[Any] | None = None
        reader: threading.Thread | None = None
        capture = _BoundedCapture()
        timed_out = False
        requested_timeout: float | None = None
        effective_timeout: float | None = None
        try:
            if not isinstance(command, str):
                raise TypeError("command must be a string")
            requested = self._timeout_value(timeout)
            remaining = self.task.seconds_left() if self.task is not None else math.inf
            if math.isnan(remaining):
                remaining = 0.0
            duration = min(requested, max(0.0, remaining))
            requested_timeout = None if math.isinf(requested) else requested
            effective_timeout = None if math.isinf(duration) else duration
            if duration <= 0:
                result = CommandResult(124, "[timed out before command started]\n", True)
                self._record_command(
                    command=command,
                    workdir=str(workdir),
                    exit_code=result.exit_code,
                    output=result.output,
                    timed_out=True,
                    timeout_seconds=requested_timeout,
                    effective_timeout_seconds=effective_timeout,
                    seconds=round(time.monotonic() - started, 3),
                )
                return result

            cwd = self._path(workdir)
            capture_deadline = None if math.isinf(duration) else time.monotonic() + duration
            process = subprocess.Popen(
                ["bash", "-lc", command],
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                start_new_session=True,
            )
            assert process.stdout is not None
            reader = threading.Thread(target=_drain_output, args=(process.stdout, capture), daemon=True)
            reader.start()

            wait_timeout = None if capture_deadline is None else max(0.0, capture_deadline - time.monotonic())
            try:
                process.wait(timeout=wait_timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_process_group(process)
                process.wait()

            # Do not leave descendants holding this command's pipe open indefinitely.
            reader.join(timeout=0.5)
            if reader.is_alive():
                _kill_process_group(process)
                reader.join(timeout=1.0)
            output = capture.text()
            exit_code = 124 if timed_out else int(process.returncode or 0)
            result = CommandResult(exit_code, output, timed_out)
            self._record_command(
                command=command,
                workdir=str(cwd),
                exit_code=exit_code,
                output=output,
                timed_out=timed_out,
                timeout_seconds=requested_timeout,
                effective_timeout_seconds=effective_timeout,
                seconds=round(time.monotonic() - started, 3),
            )
            return result
        except BaseException as error:
            if process is not None:
                try:
                    _kill_process_group(process)
                except OSError:
                    pass
                try:
                    process.wait(timeout=1)
                except (subprocess.TimeoutExpired, OSError):
                    pass
                if reader is not None:
                    reader.join(timeout=1)
            self._record_command(
                command=command if isinstance(command, str) else repr(command),
                workdir=str(workdir),
                exit_code=None,
                output=capture.text(),
                timed_out=timed_out,
                timeout_seconds=requested_timeout,
                effective_timeout_seconds=effective_timeout,
                seconds=round(time.monotonic() - started, 3),
                error=f"{type(error).__name__}: {error}",
            )
            raise
        finally:
            if process is not None and process.stdout is not None:
                try:
                    process.stdout.close()
                except OSError:
                    pass

    def read(self, path: str | Path) -> bytes:
        target = self._path(path)
        try:
            data = target.read_bytes()
        except BaseException as error:
            self.trajectory.log("read_error", path=str(target), error=f"{type(error).__name__}: {error}")
            raise
        self.trajectory.log("read", path=str(target), bytes=len(data))
        return data

    def write(self, path: str | Path, data: bytes | bytearray | str) -> None:
        target = self._path(path)
        if isinstance(data, str):
            payload = data.encode("utf-8")
        elif isinstance(data, (bytes, bytearray)):
            payload = bytes(data)
        else:
            raise TypeError("data must be bytes, bytearray, or str")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        except BaseException as error:
            self.trajectory.log("write_error", path=str(target), error=f"{type(error).__name__}: {error}")
            raise
        self.trajectory.log("write", path=str(target), bytes=len(payload))

    def note(self, text: str) -> None:
        self.trajectory.log("note", content=str(text))


def _python_version_pair(value: Any) -> tuple[int, int] | None:
    match = re.match(r"^\s*(\d+)\.(\d+)", str(value))
    return (int(match.group(1)), int(match.group(2))) if match else None


def _validate_agent_class(agent_class: Any) -> type:
    if not inspect.isclass(agent_class):
        raise TypeError("the agent descriptor did not resolve to a class")
    if not callable(getattr(agent_class, "solve", None)):
        raise TypeError("the agent class must define a callable solve(task, box, lm)")
    return agent_class


def load_agent() -> type:
    descriptor_path = HARNESS_DIR / "agent.json"
    descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
    if not isinstance(descriptor, dict):
        raise ValueError("/harness/agent.json must contain a JSON object")

    kind = descriptor.get("kind")
    if kind == "pickle":
        version = descriptor.get("python_version")
        metadata = descriptor.get("metadata")
        if version is None and isinstance(metadata, dict):
            version = metadata.get("python_version")
        expected = _python_version_pair(version)
        if expected is None:
            raise ValueError("pickle agent descriptor must include a valid python_version")
        actual = tuple(sys.version_info[:2])
        if expected != actual:
            raise RuntimeError(
                f"pickle agent requires Python {expected[0]}.{expected[1]}, "
                f"but the runtime is Python {actual[0]}.{actual[1]}"
            )
        # Also works through the standard CLI, which sets PYTHONPATH itself.
        sys.path.insert(0, str(HARNESS_DIR / "_vendor"))
        import cloudpickle

        with (HARNESS_DIR / "agent.pkl").open("rb") as handle:
            agent_class = cloudpickle.load(handle)
        return _validate_agent_class(agent_class)

    if kind == "file":
        module_name = descriptor.get("module")
        class_name = descriptor.get("class_name")
        if not isinstance(module_name, str) or not module_name or any(
            not part.isidentifier() for part in module_name.split(".")
        ):
            raise ValueError("file agent descriptor must include a dotted module name")
        if not isinstance(class_name, str) or not class_name or any(
            not part.isidentifier() for part in class_name.split(".")
        ):
            raise ValueError("file agent descriptor must include a nested class_name")

        usercode = HARNESS_DIR / "usercode"
        if not usercode.is_dir():
            raise FileNotFoundError(f"agent usercode directory does not exist: {usercode}")
        import_path = str(usercode.resolve())
        if import_path in sys.path:
            sys.path.remove(import_path)
        sys.path.insert(0, import_path)
        importlib.invalidate_caches()
        module = importlib.import_module(module_name)
        origin = getattr(module, "__file__", None)
        if origin is None:
            raise ImportError(f"agent module {module_name!r} did not resolve to a file in {usercode}")
        try:
            Path(origin).resolve().relative_to(usercode.resolve())
        except ValueError:
            raise ImportError(f"agent module {module_name!r} resolved outside {usercode}") from None
        agent_class: Any = module
        for part in class_name.split("."):
            agent_class = getattr(agent_class, part)
        return _validate_agent_class(agent_class)

    raise ValueError("agent descriptor kind must be 'pickle' or 'file'")


def _settings() -> dict[str, Any]:
    raw = os.environ.get("CYBERGYM_HARNESS_SETTINGS", "{}") or "{}"
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise TypeError("CYBERGYM_HARNESS_SETTINGS must be a JSON object")
    return value


def _task(started_at: float) -> Task:
    raw_limit = os.environ.get("CYBERGYM_TIME_LIMIT_S", "0") or "0"
    try:
        limit = int(raw_limit)
    except ValueError as error:
        raise ValueError("CYBERGYM_TIME_LIMIT_S must be a non-negative integer") from error
    if limit < 0:
        raise ValueError("CYBERGYM_TIME_LIMIT_S must be a non-negative integer")
    deadline = started_at + limit if limit else float("inf")
    return Task(
        id=os.environ.get("CYBERGYM_TASK_ID", ""),
        difficulty=os.environ.get("CYBERGYM_DIFFICULTY", "level2"),
        time_limit_seconds=limit,
        deadline=deadline,
    )


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_poc(result: Any, poc_path: Path) -> int:
    if result is None:
        if not poc_path.is_file() or poc_path.stat().st_size == 0:
            raise RuntimeError("agent returned None without writing a nonempty /output/poc")
        return poc_path.stat().st_size
    if isinstance(result, str):
        payload = result.encode("utf-8")
    elif isinstance(result, (bytes, bytearray)):
        payload = bytes(result)
    else:
        raise TypeError("agent.solve must return bytes, bytearray, str, or None")
    if not payload:
        raise RuntimeError("agent returned an empty final output")
    _atomic_write(poc_path, payload)
    return len(payload)


def _report_bytes(
    *,
    exit_code: int,
    error: str | None,
    started: float,
    task: Task | None,
    box: Box | None,
    poc_bytes: int | None = None,
) -> bytes:
    report: dict[str, Any] = {
        "exit_code": exit_code,
        "error": error,
        "timed_out": False,
        "oom_killed": False,
        "commands": box.commands if box is not None else 0,
        "command_timeouts": box.command_timeouts if box is not None else 0,
        "seconds": round(time.monotonic() - started, 3),
    }
    if task is not None:
        report["task_id"] = task.id
        report["difficulty"] = task.difficulty
    if poc_bytes is not None:
        report["poc_bytes"] = poc_bytes
    return (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def main() -> int:
    started_wall = time.time()
    started = time.monotonic()
    output = _output_path()
    workspace = _workspace_path()
    report_path = output / "agent_exit.json"
    trajectory = Trajectory(output / "trajectory.jsonl")
    task: Task | None = None
    box: Box | None = None
    poc_bytes: int | None = None

    try:
        output.mkdir(parents=True, exist_ok=True)
        workspace.mkdir(parents=True, exist_ok=True)
        report_path.unlink(missing_ok=True)
        # Do not mistake an artifact left by an earlier invocation for this agent's output.
        (output / "poc").unlink(missing_ok=True)
        task = _task(started_wall)
        trajectory.log("start", task_id=task.id, difficulty=task.difficulty)
        box = Box(task=task, trajectory=trajectory, workspace=workspace, output=output)
        agent_class = load_agent()
        settings = _settings()
        agent = agent_class(**settings)
        if not callable(getattr(agent, "solve", None)):
            raise TypeError("the agent instance must expose callable solve(task, box, lm)")
        lm = ModelAccess(
            base_url=os.environ.get("OPENAI_BASE_URL", ""),
            api_key=os.environ.get("OPENAI_API_KEY", ""),
            model=os.environ.get("CYBERGYM_MODEL", ""),
        )
        trajectory.log("agent_start", agent_class=f"{agent_class.__module__}.{agent_class.__qualname__}")
        result = agent.solve(task, box, lm)
        poc_bytes = _write_poc(result, output / "poc")
        trajectory.log("agent_end", poc_bytes=poc_bytes)
        _atomic_write(
            report_path,
            _report_bytes(
                exit_code=0,
                error=None,
                started=started,
                task=task,
                box=box,
                poc_bytes=poc_bytes,
            ),
        )
        trajectory.log("end", exit_code=0)
        return 0
    except BaseException as error:
        error_text = "".join(traceback.format_exception(type(error), error, error.__traceback__))[-8000:]
        try:
            trajectory.log("error", message=error_text)
        except BaseException:
            pass
        print(error_text, file=sys.stderr, flush=True)
        try:
            _atomic_write(
                report_path,
                _report_bytes(
                    exit_code=1,
                    error=error_text,
                    started=started,
                    task=task,
                    box=box,
                ),
            )
        except BaseException as report_error:
            print(
                f"could not write agent exit report: {type(report_error).__name__}: {report_error}",
                file=sys.stderr,
                flush=True,
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
