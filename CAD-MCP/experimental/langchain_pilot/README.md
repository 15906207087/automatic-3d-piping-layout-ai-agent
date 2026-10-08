# LangChain Pilot — memory + iterative codegen

Sidecar demo only. Does **not** hook into CAD recognize / JSON write / draw / check / redraw.

## What it does

1. `SessionMemoryStore` keeps:
   - LangChain `InMemoryChatMessageHistory`
   - structured reasoning turns
   - intermediate artifacts (latest code, open issues, etc.)
2. `IterativeCodegenRunner` each round:
   - reads memory context
   - generates next code draft
   - writes reasoning + intermediate results back to memory
3. Session JSON is saved under `sessions/` for inspection.

## Run

From `CAD-MCP` root, with venv activated:

```text
python -m pip install -r requirements-langchain-optional.txt
python -m experimental.langchain_pilot
```

Or run a custom iteration loop in Python:

```python
from experimental.langchain_pilot import run_demo_iterations

print(run_demo_iterations(goal="my helper", feedbacks=["add dn", "add wall"]))
```
