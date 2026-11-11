"""Author a normal CyberGym container harness from a trusted Python agent.

Only authoring happens on the host. Agent imports, construction and solve() happen
inside Docker. No Docker socket or host environment is passed to the container.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_PICKLE_LOCK = threading.RLock()
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_IMAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]*$")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, allow_nan=False)


def _hash(directory: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(directory.rglob('*')):
        if p.is_file() and '__pycache__' not in p.parts and p.name != 'docker-agent.json':
            h.update(p.relative_to(directory).as_posix().encode() + b'\0' + p.read_bytes())
    return h.hexdigest()


@dataclass(frozen=True)
class DockerHarness:
    """Built image and immutable source bundle; accepted by Gym.agent()."""
    directory: str
    name: str
    image: str
    image_id: str
    fingerprint: str
    protocol: str = 'cybergym.container.v1'

    def container_code(self, settings: dict[str, Any]) -> dict[str, Any]:
        _json(settings)
        root = Path(self.directory)
        if _hash(root) != self.fingerprint:
            raise ValueError('Container harness source changed after build; export/build a new bundle')
        return {
            'manifest': {'image': self.image_id, 'entry': ['python3', '/harness/docker_runtime.py']},
            'image_id': self.image_id,
            'harness_dir': root, 'common_dir': root,
            'settings': settings, 'code_sha256': self.fingerprint,
            'environment': {'PYTHONPATH': '/harness/_vendor:/harness/usercode:/harness-common'},
        }

    def run(
        self, workspace: str | Path, output: str | Path, *,
        settings: dict[str, Any] | None = None, timeout: int = 60,
        cpus: float = 1, memory_gb: float = 1, network: str = 'none',
        model_access: Any = None,
    ) -> dict[str, Any]:
        """Run a local smoke/development task, without submitting or grading it.

        Uses the evaluator's same container lifecycle/resource/timeout backend.
        Use gym.agent(self).rollout(...) for prepared, metered, graded tasks.
        """
        from cybergym_eval.config import RunSpec
        from cybergym_eval.worker import _run_harness

        workspace, output = Path(workspace).expanduser().resolve(), Path(output).expanduser().resolve()
        if not workspace.is_dir():
            raise ValueError('workspace must be an existing directory')
        if output.exists() and any(output.iterdir()):
            raise ValueError('output must be empty; every run requires a fresh artifact directory')
        output.mkdir(parents=True, exist_ok=True)
        code = self.container_code(settings or {})
        code['agent_log'] = str(output / 'agent.log')
        code['environment'].update({
            'OPENAI_BASE_URL': getattr(model_access, 'base_url', ''),
            'CYBERGYM_MODEL': getattr(model_access, 'model', ''),
        })
        config = {'work_root': str(output.parent), 'use_firewall': False,
                  'task_cpus': cpus, 'task_memory_gb': memory_gb, 'model': '',
                  'shim_port': 4120, 'container_network': network}
        spec = RunSpec('local', 'level2', self.name, uuid.uuid4().hex, str(output.parent))
        result = _run_harness(config, spec, code, getattr(model_access, 'api_key', ''), workspace, output, timeout)
        return {**result, 'execution_id': spec.execution_id}


class DockerAgent:
    """Wrap a class object, or use from_file() without importing the file on host.

    Agent interface: solve(task, box, lm), returning final bytes (or writing
    /output/poc and returning None). Constructor arguments are runtime settings.
    Dependencies must be installed in base_image or declared in requirements.
    """
    def __init__(self, agent_class: type):
        if not inspect.isclass(agent_class) or not callable(getattr(agent_class, 'solve', None)):
            raise TypeError('DockerAgent requires a class with solve(task, box, lm)')
        self.agent_class = agent_class
        self.file: Path | None = None
        self.source_root: Path | None = None
        self.class_name = agent_class.__qualname__

    @classmethod
    def from_file(cls, file: str | Path, class_name: str, *, source_root: str | Path | None = None) -> DockerAgent:
        """Copy one source file, or an explicitly selected Python source tree."""
        file = Path(file).expanduser().resolve(strict=True)
        if not file.is_file() or file.suffix != '.py':
            raise ValueError('file must be an existing Python file')
        if not class_name or any(not x.isidentifier() for x in class_name.split('.')):
            raise ValueError('class_name must be a dotted Python class name')
        root = Path(source_root).expanduser().resolve(strict=True) if source_root else None
        relative = file.relative_to(root) if root else Path(file.name)
        if any(not x.isidentifier() for x in relative.with_suffix('').parts):
            raise ValueError('source module path must contain valid Python identifiers')
        obj = cls.__new__(cls)
        obj.agent_class, obj.file, obj.source_root, obj.class_name = None, file, root, class_name
        return obj

    def build(
        self, directory: str | Path, *, name: str,
        base_image: str = 'cybergym-harness-base:1',
        requirements: tuple[str, ...] | list[str] = (), build_timeout: int = 900,
    ) -> DockerHarness:
        """Create an immutable standard harness directory and build its image.

        Never overwrite a source directory. Build failures preserve the exported
        bundle and build.log for diagnosis; retry with build_exported().
        """
        if not _NAME.fullmatch(name) or name in ('.', '..'):
            raise ValueError('name must be a safe harness name')
        if not _IMAGE.fullmatch(base_image):
            raise ValueError('base_image must be a Docker image reference without whitespace')
        requirements = list(requirements)
        if any(not isinstance(r, str) or not r or r.startswith('-') or any(c in r for c in '\r\n\0') for r in requirements):
            raise ValueError('requirements must be individual package requirement strings')
        destination = Path(directory).expanduser().absolute()
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(f'Will not overwrite existing harness: {destination}')
        if self.source_root and destination.resolve().is_relative_to(self.source_root):
            raise ValueError('Export directory must be outside the selected source_root')
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix='.docker-agent-', dir=destination.parent))
        try:
            shutil.copyfile(Path(__file__).with_name('docker_runtime.py'), staging / 'docker_runtime.py')
            if self.file is not None:
                code = staging / 'usercode'; code.mkdir()
                files = list(self.source_root.rglob('*.py')) if self.source_root else [self.file]
                total = 0
                for source in files:
                    if any(p in ('.venv', '.git', '__pycache__', '.secrets') for p in source.parts):
                        continue
                    if source.is_symlink() or not source.is_file():
                        raise ValueError(f'Only regular Python sources can be bundled: {source}')
                    total += source.stat().st_size
                    if total > 10 * 1024 * 1024 or len(files) > 5000:
                        raise ValueError('Python source bundle exceeds 10 MiB/5000 files')
                    relative = source.relative_to(self.source_root) if self.source_root else Path(source.name)
                    target = code / relative; target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
                relative = self.file.relative_to(self.source_root) if self.source_root else Path(self.file.name)
                descriptor = {'kind': 'file', 'module': '.'.join(relative.with_suffix('').parts), 'class_name': self.class_name}
            else:
                import cloudpickle
                module = inspect.getmodule(self.agent_class)
                with _PICKLE_LOCK:
                    registered = module is not None and module.__name__ in cloudpickle.list_registry_pickle_by_value()
                    if module is not None and not registered:
                        cloudpickle.register_pickle_by_value(module)
                    try:
                        (staging / 'agent.pkl').write_bytes(cloudpickle.dumps(self.agent_class))
                    finally:
                        if module is not None and not registered:
                            cloudpickle.unregister_pickle_by_value(module)
                descriptor = {'kind': 'pickle', 'python_version': f'{sys.version_info.major}.{sys.version_info.minor}'}
                vendor = staging / '_vendor' / 'cloudpickle'
                shutil.copytree(Path(cloudpickle.__file__).parent, vendor, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            (staging / 'agent.json').write_text(_json(descriptor) + '\n')
            (staging / 'requirements.txt').write_text('\n'.join(requirements) + '\n')
            dockerfile = f'FROM {base_image}\nCOPY . /harness\n'
            if requirements:
                dockerfile += 'RUN pip install --no-cache-dir -r /harness/requirements.txt\n'
            dockerfile += 'ENV PYTHONPATH=/harness/_vendor:/harness/usercode:/harness-common PYTHONDONTWRITEBYTECODE=1\nWORKDIR /workspace\n'
            (staging / 'Dockerfile').write_text(dockerfile)
            fingerprint = _hash(staging)
            image = f'cybergym-python/{name}:{fingerprint[:16]}'
            (staging / 'harness.toml').write_text(f'name = {json.dumps(name)}\nversion = "1"\nimage = {json.dumps(image)}\nentry = ["python3", "/harness/docker_runtime.py"]\n\n[settings]\n')
            # Fingerprint includes the generated manifest, excluding its receipt.
            fingerprint = _hash(staging)
            receipt = {'name': name, 'image': image, 'fingerprint': fingerprint}
            (staging / 'docker-agent.json').write_text(_json(receipt) + '\n')
            os.rename(staging, destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return build_exported(destination, timeout=build_timeout)


def build_exported(directory: str | Path, *, timeout: int = 900) -> DockerHarness:
    root = Path(directory).expanduser().resolve(strict=True)
    receipt = json.loads((root / 'docker-agent.json').read_text())
    if _hash(root) != receipt['fingerprint']:
        raise ValueError('Exported harness changed; export a new bundle instead')
    if not _IMAGE.fullmatch(receipt['image']):
        raise ValueError('Invalid image reference in build receipt')
    # Keep logs outside the hashed source bundle. Do not embed credentials/build args.
    log_path = root.parent / f'.{root.name}.build.log'
    with log_path.open('wb') as log:
        try:
            subprocess.run(['docker', 'build', '-t', receipt['image'], str(root)], stdout=log, stderr=subprocess.STDOUT, check=True, timeout=timeout)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            raise RuntimeError(f'Docker build failed or timed out; inspect {log_path}. No automatic retry was attempted.') from error
    result = subprocess.run(['docker', 'image', 'inspect', '--format', '{{.Id}}', receipt['image']], capture_output=True, text=True, check=True, timeout=30)
    return DockerHarness(str(root), receipt['name'], receipt['image'], result.stdout.strip(), receipt['fingerprint'])
