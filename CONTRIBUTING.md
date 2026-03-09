# Contributing to IncidentFlow MCP

Thank you for your interest in contributing to **IncidentFlow MCP**.

We welcome contributions from the community. This document explains how to set up the project locally, how to submit changes, and how to report issues.

---

# Development Setup

## 1. Fork the repository

Click **Fork** on GitHub and clone your fork:

```bash
git clone https://github.com/YOUR_USERNAME/incidentflow-mcp.git
cd incidentflow-mcp
```

## 2. Create a virtual environment

We recommend using Python virtual environments.

```bash
python -m venv .venv
source .venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

## 3. Run the MCP server

Example:

```bash
python -m server.main
```

Or using Docker:

```bash
docker compose up
```

---

# Project Structure

```
server/
  app/        # core MCP server
  tools/      # AI tools exposed to MCP clients
  auth/       # OAuth and authentication logic
  schemas/    # Pydantic schemas
  config/     # configuration

cli/          # command line interface
examples/     # integration examples
docs/         # documentation
tests/        # unit and integration tests
```

---

# Adding a New Tool

Tools are located in:

```
server/tools/
```

Example tool:

```python
from mcp import Tool

class ExampleTool(Tool):
    name = "example_tool"
    description = "Example MCP tool"

    async def run(self, input):
        return {"result": "ok"}
```

Register the tool in the tool registry so it becomes available to MCP clients.

---

# Running Tests

Install development dependencies:

```bash
pip install -r requirements-dev.txt
```

Run tests:

```bash
pytest
```

---

# Code Style

We use the following tools:

- `black` for formatting
- `ruff` for linting
- `pytest` for tests

Run formatting:

```bash
black .
```

Run linting:

```bash
ruff .
```

---

# Pull Request Process

1. Create a feature branch:

```bash
git checkout -b feature/my-feature
```

2. Make your changes.

3. Run tests and linters.

4. Commit changes:

```bash
git commit -m "Add new MCP tool"
```

5. Push branch:

```bash
git push origin feature/my-feature
```

6. Open a Pull Request.

---

# Reporting Issues

If you find a bug or have a feature request, please open a GitHub issue and include:

- Steps to reproduce
- Expected behavior
- Actual behavior
- Logs or screenshots if relevant

---

# Security Issues

Please do not open public issues for security vulnerabilities.

Follow the process described in [SECURITY.md](SECURITY.md).