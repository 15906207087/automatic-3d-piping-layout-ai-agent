"""
会话级内存：对话历史 + 推理轨迹 + 中间结果工件。

基于 langchain_core 的 InMemoryChatMessageHistory，并扩展结构化工件槽位，
供迭代代码生成在下一轮读取上下文。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.chat_history import InMemoryChatMessageHistory
from langchain_core.messages import AIMessage, HumanMessage


@dataclass
class ReasoningTurn:
    """单轮推理与代码生成记录。"""

    iteration: int
    user_feedback: str
    reasoning: str
    code: str
    intermediate: Dict[str, Any] = field(default_factory=dict)
    issues_resolved: List[str] = field(default_factory=list)
    remaining_issues: List[str] = field(default_factory=list)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class SessionMemoryStore:
    """
    上下文感知内存。

    - chat_history: LangChain 消息历史（Human / AI）
    - reasoning_turns: 结构化推理与代码迭代轨迹
    - artifacts: 最新中间结果（如当前代码、参数草案、检查结论）
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.chat_history = InMemoryChatMessageHistory()
        self.reasoning_turns: List[ReasoningTurn] = []
        self.artifacts: Dict[str, Any] = {
            "goal": "",
            "latest_code": "",
            "latest_intermediate": {},
            "open_issues": [],
        }

    def set_goal(self, goal: str) -> None:
        self.artifacts["goal"] = goal
        self.chat_history.add_message(HumanMessage(content=f"[GOAL] {goal}"))

    def add_user_feedback(self, feedback: str) -> None:
        self.chat_history.add_message(HumanMessage(content=feedback))

    def record_turn(self, turn: ReasoningTurn) -> None:
        self.reasoning_turns.append(turn)
        self.artifacts["latest_code"] = turn.code
        self.artifacts["latest_intermediate"] = dict(turn.intermediate)
        self.artifacts["open_issues"] = list(turn.remaining_issues)
        summary = (
            f"[ITER {turn.iteration}] reasoning={turn.reasoning[:200]} | "
            f"resolved={turn.issues_resolved} | remaining={turn.remaining_issues}"
        )
        self.chat_history.add_message(AIMessage(content=summary))

    def build_context_block(self, max_turns: int = 5) -> str:
        """把近期推理历史与中间结果压成可供下一轮生成使用的上下文。"""
        goal = self.artifacts.get("goal") or ""
        latest_code = self.artifacts.get("latest_code") or ""
        open_issues = self.artifacts.get("open_issues") or []
        intermediate = self.artifacts.get("latest_intermediate") or {}
        recent = self.reasoning_turns[-max_turns:]

        lines: List[str] = [
            f"session_id: {self.session_id}",
            f"goal: {goal}",
            f"open_issues: {open_issues}",
            f"latest_intermediate: {json.dumps(intermediate, ensure_ascii=False)}",
            "recent_reasoning_turns:",
        ]
        for turn in recent:
            lines.append(
                f"- iter={turn.iteration}; feedback={turn.user_feedback}; "
                f"reasoning={turn.reasoning}; remaining={turn.remaining_issues}"
            )
        lines.append("latest_code:")
        lines.append(latest_code or "(empty)")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "artifacts": self.artifacts,
            "reasoning_turns": [asdict(t) for t in self.reasoning_turns],
            "chat_messages": [
                {"type": m.type, "content": m.content} for m in self.chat_history.messages
            ],
        }

    def save_json(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path


_SESSION_STORE: Dict[str, SessionMemoryStore] = {}


def get_session_memory(session_id: str) -> SessionMemoryStore:
    """供 RunnableWithMessageHistory 风格的按 session 取内存。"""
    if session_id not in _SESSION_STORE:
        _SESSION_STORE[session_id] = SessionMemoryStore(session_id=session_id)
    return _SESSION_STORE[session_id]


def reset_session_memory(session_id: Optional[str] = None) -> None:
    if session_id is None:
        _SESSION_STORE.clear()
    else:
        _SESSION_STORE.pop(session_id, None)
