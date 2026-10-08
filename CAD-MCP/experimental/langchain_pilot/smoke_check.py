"""冒烟：验证内存链能跨轮保留推理/中间结果，并驱动迭代代码生成。"""

from __future__ import annotations

import importlib.metadata as metadata
import json
import sys
from typing import Any, Dict


def _pkg_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "NOT_INSTALLED"


def run_smoke() -> Dict[str, Any]:
    report: Dict[str, Any] = {
        "ok": False,
        "mode": "sidecar_memory_iterative_codegen",
        "affects_main_cad_pipeline": False,
        "packages": {
            "langchain": _pkg_version("langchain"),
            "langchain-core": _pkg_version("langchain-core"),
        },
    }

    missing = [k for k, v in report["packages"].items() if v == "NOT_INSTALLED"]
    if missing:
        report["error"] = (
            "missing optional deps: "
            + ", ".join(missing)
            + ". Install with: python -m pip install -r requirements-langchain-optional.txt"
        )
        return report

    from experimental.langchain_pilot.iterative_codegen import run_demo_iterations
    from experimental.langchain_pilot.memory_store import get_session_memory
    import ast

    result = run_demo_iterations(
        goal="parametric hollow pipe segment helper",
        feedbacks=[
            "please add dn and length parameters",
            "also need wall thickness and hollow pipe modeling",
            "add start/end points, axis, and input validate",
        ],
        session_id="smoke_memory_iter",
        persist=True,
    )
    memory = get_session_memory("smoke_memory_iter")
    final_code = result.get("final_code") or ""

    code_parses = False
    try:
        ast.parse(final_code)
        code_parses = True
    except SyntaxError:
        code_parses = False

    checks = {
        "three_iterations": result.get("iterations") == 3,
        "three_memory_turns": result.get("memory_turns") == 3,
        "context_has_goal": "parametric hollow pipe" in memory.build_context_block(),
        "code_has_dn": "dn" in final_code,
        "code_has_wall": "wall_mm" in final_code,
        "code_has_start": "start" in final_code,
        "code_has_validate": "ValueError" in final_code,
        "code_parses": code_parses,
        "chat_history_nonempty": len(memory.chat_history.messages) >= 4,
        "session_file_written": bool(result.get("session_file")),
    }
    report["checks"] = checks
    report["result_summary"] = {
        "session_id": result.get("session_id"),
        "session_file": result.get("session_file"),
        "open_issues": result.get("open_issues"),
        "final_code_preview": final_code[:400],
    }
    report["ok"] = all(checks.values())
    if not report["ok"]:
        report["error"] = f"failed checks: {[k for k, v in checks.items() if not v]}"
    return report


def main() -> int:
    report = run_smoke()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
