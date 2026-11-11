from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class OptimizationResult:
    instructions: str
    seed_instructions: str
    seed_score: float
    best_score: float
    artifact: Path
    rollouts: int


class _PoCProgram:
    def __init__(self, gym: Any, agent_class: type, taskset: Any, instructions: str):
        import dspy

        self._dspy = dspy
        self.gym = gym
        self.agent_class = agent_class
        self.taskset = taskset
        self.plan = dspy.Predict(dspy.Signature("task -> strategy", instructions))
        self.counter = {"rollouts": 0}

    def named_predictors(self) -> list[tuple[str, Any]]:
        return [("plan", self.plan)]

    def deepcopy(self) -> "_PoCProgram":
        clone = _PoCProgram.__new__(_PoCProgram)
        clone._dspy = self._dspy
        clone.gym = self.gym
        clone.agent_class = self.agent_class
        clone.taskset = self.taskset
        clone.plan = self.plan.deepcopy()
        clone.counter = self.counter
        return clone

    def __call__(self, task: str) -> Any:
        instructions = self.plan.signature.instructions
        agent = self.gym.agent(self.agent_class, instructions=instructions)
        rollout = agent.rollout(self.taskset, task)
        self.counter["rollouts"] += 1
        return self._dspy.Prediction(passed=rollout.passed, feedback=rollout.feedback, task=task)


def _metric(gold: Any, pred: Any, trace: Any = None, pred_name: Any = None, pred_trace: Any = None) -> Any:
    import dspy

    score = 1.0 if getattr(pred, "passed", False) else 0.0
    return dspy.Prediction(score=score, feedback=getattr(pred, "feedback", ""))


def optimize_instructions(
    gym: Any,
    agent_class: type,
    train_ids: list[str],
    seed_instructions: str,
    *,
    taskset_name: str | None = None,
    reflection_lm: Any = None,
    max_metric_calls: int = 60,
    valset_ids: list[str] | None = None,
) -> OptimizationResult:
    import dspy

    name = taskset_name or f"gepa-{agent_class.__name__.lower()}"
    taskset = gym.taskset(name, tasks=train_ids)
    valset_taskset = gym.taskset(f"{name}-val", tasks=valset_ids) if valset_ids else taskset
    trainset = [dspy.Example(task=task).with_inputs("task") for task in train_ids]
    valset = [dspy.Example(task=task).with_inputs("task") for task in (valset_ids or train_ids)]

    program = _PoCProgram(gym, agent_class, taskset, seed_instructions)
    reflection = reflection_lm or _reflection_lm(gym)
    optimizer = dspy.GEPA(metric=_metric, reflection_lm=reflection, max_metric_calls=max_metric_calls, track_stats=True)
    compiled = optimizer.compile(program, trainset=trainset, valset=valset)

    best = compiled.plan.signature.instructions
    seed_score = _score(program, valset)
    best_score = _score(compiled, valset_taskset and valset)
    artifact = Path(gym.work_root) / name / "optimized_instructions.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(
        json.dumps(
            {
                "agent_class": f"{agent_class.__module__}.{agent_class.__qualname__}",
                "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "train_ids": train_ids,
                "seed_instructions": seed_instructions,
                "instructions": best,
                "seed_score": seed_score,
                "best_score": best_score,
                "rollouts": program.counter["rollouts"],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return OptimizationResult(
        instructions=best,
        seed_instructions=seed_instructions,
        seed_score=seed_score,
        best_score=best_score,
        artifact=artifact,
        rollouts=program.counter["rollouts"],
    )


def _reflection_lm(gym: Any) -> Any:
    from cybergym_eval.sandbox import ModelAccess
    from cybergym_eval.server import start_server
    from cybergym_eval.shim import issue_key, shim_url, start_shim

    config = gym._config("reflection", [])
    start_server(config)
    start_shim(config)
    key = issue_key(config, "gepa-reflection")
    return ModelAccess(base_url=shim_url(config), api_key=key, model=config["model"]).dspy(max_tokens=16000)


def _score(program: Any, valset: list[Any]) -> float:
    if not valset:
        return 0.0
    passed = sum(1 for example in valset if getattr(program(example.task), "passed", False))
    return round(passed / len(valset), 4)


import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent))
from react_agent import DEFAULT_INSTRUCTIONS, ReActAgent


def optimize(gym, train_ids, **kwargs):
    return optimize_instructions(gym, ReActAgent, train_ids, DEFAULT_INSTRUCTIONS, **kwargs)
