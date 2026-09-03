# Repository-specific agent instructions

This file provides guidance for the coding agent working on this repository.

## Project structure
- Frontend code is in `frontend/`
- Backend code is in `backend/`
- Tests mirror the source structure (`tests/` mirrors `backend/`)

## Conventions
- Use Python type hints on all function signatures.
- Run `make lint` and `make test` before committing.
- Follow the existing module layout — do not create top-level directories without discussion.

## Dependencies
- Python dependencies are managed with `uv`.
- Do not add new dependencies unless absolutely necessary.
- If a new dependency is required, add it to `pyproject.toml` and run `uv lock`.

## Testing
- Always run `make test` after changes.
- If tests fail, fix the implementation, do not modify tests unless the test itself is incorrect.
- Add tests for new functionality.

## Deployment
- This project is deployed via Docker.
- Do not modify Dockerfile or docker-compose.yml unless the task explicitly requires it.

## Communication
- If you are unsure about architecture decisions, inspect existing similar implementations first.
- Report any ambiguity in the implementation report.