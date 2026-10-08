"""
LangChain 旁路试点：带内存的上下文感知迭代代码生成。

约束：
- 不导入 cad_controller / server / parametric_models
- 不读写 CAD 图纸，不改 shared JSON，不触发出图
- 仅在显式执行本包入口时运行
"""

from experimental.langchain_pilot.iterative_codegen import (
    IterativeCodegenRunner,
    run_demo_iterations,
)
from experimental.langchain_pilot.memory_store import SessionMemoryStore

__all__ = [
    "PILOT_NAME",
    "PILOT_MODE",
    "SessionMemoryStore",
    "IterativeCodegenRunner",
    "run_demo_iterations",
]

PILOT_NAME = "langchain_pilot"
PILOT_MODE = "sidecar_memory_iterative_codegen"
