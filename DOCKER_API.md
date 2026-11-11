# Python agent → Docker harness

The shared API lives in `common/docker_api.py`; `common/docker_runtime.py` runs inside the container. The whole agent class runs inside Docker, not a host-side agent controlling a separate shell container. No Docker socket or ambient host credentials are mounted/passed.

Use `/home/anshul/cybergym-eval/.venv/bin/python` (Python 3.12, matching the default base image). Add `/home/anshul/cybergym-harnesses` to your script's import path or PYTHONPATH.

```python
from common import DockerAgent

class MyAgent:
    def __init__(self, greeting="hello"):
        self.greeting = greeting

    def solve(self, task, box, lm):
        result = box.run("printf hello", timeout=5)
        box.note(result.output)
        return self.greeting.encode()

harness = DockerAgent(MyAgent).build(
    "/home/anshul/cybergym-harnesses/harnesses/my-agent-v1",
    name="my-agent-v1",
)
```

Alternatively wrap a file without importing it on the host:

```python
harness = DockerAgent.from_file(
    "/path/to/agent.py", "MyAgent",
    # source_root="/path/to/python-package",  # optional: explicitly include Python helpers
).build(
    "/home/anshul/cybergym-harnesses/harnesses/my-agent-v1",
    name="my-agent-v1",
    # requirements=["dspy==3.4.1"],
    # base_image="my-dependency-image:version",
)
```

Only `.py` files from an explicitly selected source_root are included; symlink files are refused. Non-Python assets must be provided by your base image or task workspace. A class object uses trusted cloudpickle serialization, and its Python major/minor must match the image. Do not serialize credentials or untrusted class objects. File dependencies/third-party packages must be explicitly provided; they are not inferred from the host environment. RLM/Deno dependencies are not automatically installed.

## Direct local run (no grading or model calls unless the agent requests them)

```python
result = harness.run(
    "/absolute/existing/task-workspace",
    "/absolute/new/output-directory",
    settings={"greeting": "hello"},
    timeout=60, cpus=1, memory_gb=1,
)
assert result["exit_code"] == 0
```

Direct runs default to `network="none"`. They write `poc`, `trajectory.jsonl`, `agent_exit.json` and `agent.log` under the supplied fresh output directory. Network access and `model_access` are explicit opt-ins for direct runs. Direct runs do not issue metered keys, prepare CyberGym tasks, or grade outputs.

## Existing Gym API / evaluation integration

```python
from cybergym_eval import Gym

gym = Gym("/home/anshul/cybergym-work/my-development-run", held_out=test_ids)
tasks = gym.taskset("my-train-subset", tasks=train_ids)
agent = gym.agent(harness, greeting="hello")
rollouts = agent.rollouts(tasks)
```

This selects the shared evaluator execution core, which prepares tasks, issues metered shim access, launches the container with task limits, grades, records results and revokes the key. Existing `gym.agent(SomeClass)` keeps its original host-agent behavior for compatibility; pass the built `DockerHarness` to run the whole class in Docker.

The exported directory is also a normal CLI harness (`harness.toml`, Dockerfile, entry point). Reference its name under the existing evaluation config, then prepare/run using a **new label**. No benchmark runs are launched by building/exporting a harness.

## Reproducibility and failures

Builds refuse existing destinations. Source bundles are hashed and checked before running; images are pinned by ID for Gym/direct runs. Constructor settings contribute to Gym's candidate fingerprint. A failed build leaves the bundle and adjacent `.NAME.build.log`; `build_exported(directory)` retries only when explicitly called. Build/runtime dependency downloads require network and are not offline-tested.

A raised exception, missing/empty final output or bad class is reported as a nonzero exit. Return bytes/bytearray/string, or write a nonempty `/output/poc` and return None. Whole-agent timeout removes the container in the shared worker's finally cleanup. `box.run` has its own bounded-output command timeout.

Adding common authoring files changes the shared source-tree hash for future preparations. Existing frozen baseline artifacts remain unchanged; do not re-prepare an existing frozen label after modifying shared code—use a new label.

## Validation performed

Run harmless real-Docker acceptance tests:

```sh
cd /home/anshul
PYTHONDONTWRITEBYTECODE=1 LITELLM_LOCAL_MODEL_COST_MAP=True \
  cybergym-eval/.venv/bin/python -B cybergym-harnesses/tests/test_docker_api.py
```

Tests cover class/file execution in real containers, constructor settings, local tools, binary output, exception/missing-output reports, command/agent timeouts, cleanup, source-tamper rejection, Ray serialization and mocked Gym dispatch. No LLM calls, benchmark grading or test25 runs are performed. Live graded Gym/Ray end-to-end evaluation is not yet exercised for this new wrapper.
