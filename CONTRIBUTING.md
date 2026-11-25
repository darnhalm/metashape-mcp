# Contributing

Contributions are welcome, especially focused safety fixes, tests, Metashape
version compatibility work, and reproducible workflows.

## Development setup

1. Install Python 3.11 or newer and `uv`.
2. Run `uv sync --frozen`.
3. Run `uv run python -m unittest discover -s tests -v`.

The fast tests do not require Metashape. Any change that calls the Metashape
API must also be validated in Metashape Professional on a copy of a project.
Never use irreplaceable source data for acceptance testing.

## Design rules

- Prefer a small typed workflow over exposing every Python API method.
- Read-only inspection must remain separate from mutating operations.
- Mutating workflows require validation, logging, and a restore point or an
  explicitly documented reason why full rollback is technically impossible.
- Long processing should use `prepare -> start -> wait -> inspect ->
  accept/reject` and work on a copied chunk unless in-place rollback is proven.
- Never add unrestricted string-based Python execution. The existing user
  script path is opt-in, file-scoped, hash-confirmed, and logged.
- Preserve coordinate systems, transforms, units, and round-trip manifests.
- Do not commit photos, projects, tokens, personal paths, generated logs, or
  unsanitized acceptance data.

Keep version strings synchronized in `pyproject.toml`,
`start_metashape_mcp.py`, `metashape_mcp_server.py`, and
`workflow_catalog.py`. Live acceptance must use a copied project and record
enough sanitized evidence to reproduce the result.
