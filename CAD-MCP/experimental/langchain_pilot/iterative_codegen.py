"""
上下文感知的迭代代码生成链。

默认使用离线 LocalDraftGenerator（无需 API Key），从 SessionMemoryStore
读取推理历史与中间结果，再写回新一轮代码与工件。

可选：设置环境变量 LANGCHAIN_PILOT_USE_LLM=1 且配置好模型时，可替换生成器；
当前旁路试点仍默认离线，避免影响主 CAD 流程。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.runnables import RunnableLambda, RunnablePassthrough

from experimental.langchain_pilot.memory_store import (
    ReasoningTurn,
    SessionMemoryStore,
    get_session_memory,
    reset_session_memory,
)


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_]+", "_", text.strip().lower()).strip("_")
    return cleaned[:48] or "helper"


@dataclass
class GenerationResult:
    reasoning: str
    code: str
    intermediate: Dict[str, Any]
    issues_resolved: List[str]
    remaining_issues: List[str]


class LocalDraftGenerator:
    """
    离线迭代生成器：根据 goal + memory 上下文 + 本轮反馈，增量完善代码草稿。

    这不是真实 CAD 脚本执行器；只演示「读内存 -> 生成 -> 写回内存」。
    """

    # key -> (param_signature | None, body-only feature, description)
    # Non-default params first, then defaulted params, to keep generated code valid Python.
    FEATURE_RULES = (
        ("dn", "dn: int", False, "support DN parameter"),
        ("length", "length_mm: float", False, "support length parameter"),
        ("wall", "wall_mm: float", False, "support wall thickness"),
        ("start", "start: tuple[float, float, float]", False, "support start point"),
        ("end", "end: tuple[float, float, float]", False, "support end point"),
        ("axis", 'axis: str = "z"', False, "support axis direction"),
        ("hollow", "hollow: bool = True", False, "model as hollow pipe"),
        ("validate", None, True, "validate inputs"),
    )

    def generate(
        self,
        *,
        goal: str,
        context_block: str,
        feedback: str,
        iteration: int,
        open_issues: List[str],
        latest_code: str,
    ) -> GenerationResult:
        feedback_l = feedback.lower()
        wanted: List[str] = []
        for key, _sig, _body_only, _desc in self.FEATURE_RULES:
            if key in feedback_l or any(key in issue.lower() for issue in open_issues):
                wanted.append(key)

        # Keep previously implemented features detected from latest code / memory.
        already = set()
        for key, sig, body_only, _desc in self.FEATURE_RULES:
            marker = "ValueError" if body_only else (sig or "").split(":")[0].strip()
            if latest_code and marker and marker in latest_code:
                already.add(key)
                if key not in wanted:
                    wanted.append(key)

        if not wanted:
            wanted = ["dn", "length"]

        # validate needs length available in signature.
        if "validate" in wanted and "length" not in wanted:
            wanted.insert(0, "length")

        fn_name = f"draft_{_slug(goal)}"
        params = [
            sig
            for key, sig, body_only, _ in self.FEATURE_RULES
            if key in wanted and not body_only and sig
        ]
        body_lines = [
            '    """Sidecar draft only; not wired into CAD-MCP main pipeline."""',
            f"    features = {wanted!r}",
            "    meta = {",
            f"        'goal': {goal!r},",
            f"        'iteration': {iteration},",
            f"        'feedback': {feedback!r},",
            "    }",
        ]
        if "validate" in wanted:
            body_lines.append("    if length_mm <= 0:")
            body_lines.append("        raise ValueError('length_mm must be > 0')")
        body_lines.append("    return {'features': features, 'meta': meta, 'params_echo': {")
        for key, sig, body_only, _ in self.FEATURE_RULES:
            if key in wanted and not body_only and sig:
                name = sig.split(":")[0].strip().split("=")[0].strip()
                body_lines.append(f"        '{name}': {name},")
        body_lines.append("    }}")

        code = (
            f"# Auto-draft iteration={iteration}\n"
            f"def {fn_name}({', '.join(params)}):\n"
            + "\n".join(body_lines)
            + "\n"
        )

        remaining: List[str] = []
        for key, _sig, _body_only, desc in self.FEATURE_RULES:
            if key in feedback_l and key not in wanted:
                remaining.append(desc)
        for issue in open_issues:
            key_hit = next(
                (k for k, _, _, _ in self.FEATURE_RULES if k in issue.lower()),
                None,
            )
            if key_hit and key_hit not in wanted and issue not in remaining:
                remaining.append(issue)

        reasoning = (
            f"Read memory context ({len(context_block)} chars). "
            f"Iteration {iteration} applies feedback into features={wanted}."
        )
        intermediate = {
            "function_name": fn_name,
            "features": wanted,
            "param_count": len(params),
            "feedback": feedback,
            "used_memory": bool(context_block.strip()),
        }
        return GenerationResult(
            reasoning=reasoning,
            code=code,
            intermediate=intermediate,
            issues_resolved=sorted(set(wanted)),
            remaining_issues=remaining,
        )


class IterativeCodegenRunner:
    """把「读内存 -> 生成 -> 写回」封装成可重复调用的迭代器。"""

    def __init__(
        self,
        memory: SessionMemoryStore,
        generator: Optional[LocalDraftGenerator] = None,
    ) -> None:
        self.memory = memory
        self.generator = generator or LocalDraftGenerator()
        self._chain = self._build_chain()

    def _build_chain(self):
        def _prepare(payload: Dict[str, Any]) -> Dict[str, Any]:
            feedback = str(payload.get("feedback") or "").strip()
            if not feedback:
                raise ValueError("feedback is required")
            self.memory.add_user_feedback(feedback)
            return {
                "feedback": feedback,
                "context_block": self.memory.build_context_block(),
                "goal": self.memory.artifacts.get("goal") or "",
                "open_issues": list(self.memory.artifacts.get("open_issues") or []),
                "latest_code": self.memory.artifacts.get("latest_code") or "",
                "iteration": len(self.memory.reasoning_turns) + 1,
            }

        def _generate(state: Dict[str, Any]) -> Dict[str, Any]:
            result = self.generator.generate(
                goal=state["goal"],
                context_block=state["context_block"],
                feedback=state["feedback"],
                iteration=state["iteration"],
                open_issues=state["open_issues"],
                latest_code=state["latest_code"],
            )
            turn = ReasoningTurn(
                iteration=state["iteration"],
                user_feedback=state["feedback"],
                reasoning=result.reasoning,
                code=result.code,
                intermediate=result.intermediate,
                issues_resolved=result.issues_resolved,
                remaining_issues=result.remaining_issues,
            )
            self.memory.record_turn(turn)
            return {
                "iteration": turn.iteration,
                "reasoning": turn.reasoning,
                "code": turn.code,
                "intermediate": turn.intermediate,
                "issues_resolved": turn.issues_resolved,
                "remaining_issues": turn.remaining_issues,
                "context_preview": state["context_block"][:500],
            }

        return RunnablePassthrough() | RunnableLambda(_prepare) | RunnableLambda(_generate)

    def step(self, feedback: str) -> Dict[str, Any]:
        return self._chain.invoke({"feedback": feedback})


def run_demo_iterations(
    goal: str = "parametric hollow pipe segment helper",
    feedbacks: Optional[List[str]] = None,
    session_id: Optional[str] = None,
    persist: bool = True,
) -> Dict[str, Any]:
    """
    演示：多轮反馈驱动的上下文感知代码迭代，并把会话内存落盘到 experimental 目录。
    """
    feedbacks = feedbacks or [
        "please add dn and length parameters",
        "also need wall thickness and hollow pipe modeling",
        "add start/end points, axis, and input validate",
    ]
    sid = session_id or f"iter_codegen_{uuid.uuid4().hex[:8]}"
    reset_session_memory(sid)
    memory = get_session_memory(sid)
    memory.set_goal(goal)
    runner = IterativeCodegenRunner(memory)

    steps: List[Dict[str, Any]] = []
    for fb in feedbacks:
        steps.append(runner.step(fb))

    out: Dict[str, Any] = {
        "ok": True,
        "mode": "sidecar_memory_iterative_codegen",
        "affects_main_cad_pipeline": False,
        "session_id": sid,
        "iterations": len(steps),
        "steps": steps,
        "final_code": memory.artifacts.get("latest_code"),
        "open_issues": memory.artifacts.get("open_issues"),
        "memory_turns": len(memory.reasoning_turns),
    }

    if persist:
        base = Path(__file__).resolve().parent / "sessions"
        path = memory.save_json(base / f"{sid}.json")
        out["session_file"] = str(path)

    return out
