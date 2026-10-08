# experimental — sidecar only

This directory is isolated from the CAD main pipeline and is NOT auto-loaded by
`src/server.py`, parametric draw scripts, check, or redraw flows.

| Path | Purpose |
|------|---------|
| `langchain_pilot/` | Memory-backed iterative code generation demo |

Main flow remains: recognize -> shared JSON -> script draw -> check -> redraw
via Cursor + MCP + `src/`.
