# Generated MCP tool schemas

When a tool response model or registry contract changes, regenerate and commit
the affected files under `schemas/tools/` before handing off the change:

```bash
UV_CACHE_DIR=.uv-cache uv run python scripts/generate_tool_schemas.py
```

Verify that generated schemas are current:

```bash
git status --porcelain -- schemas/tools
```
