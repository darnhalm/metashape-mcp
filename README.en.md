# Metashape MCP

[Русская версия](README.md)

A local MCP server for Agisoft Metashape Professional built around reversible
processing, persistent logs, coordinate-aware exchange, and explicit review.
Version 0.46.0 exposes 122 focused tools, live read-only resources, and
conservative workflow prompts without granting unrestricted Python execution.

Its defining feature is controlled, auditable photogrammetry: allowed working
roots, loopback-only transport, bearer authentication, persistent Metashape
logs, restore points, recoverable export quarantine, copied-chunk processing,
explicit result acceptance, and coordinate-checked model round trips.

The core is general-purpose. Cultural heritage, archaeology, museum objects,
historic buildings, and change monitoring are a priority profile because they
benefit especially from non-destructive processing, provenance, reliable scale
and coordinate systems, and reproducible reports.

## Requirements and start

- Agisoft Metashape Professional 2.2.x
- `uv`
- macOS is the currently live-tested platform; the pure Python tests also run
  on Linux in CI

Run `start_metashape_mcp.py` from **Tools -> Run Script** in Metashape. Connect
an MCP client using Streamable HTTP at `http://127.0.0.1:8766/mcp`, supply the
local bearer token stored in the per-port service directory, and add only the
required working directories to the allowlist. Full setup, operation, and
recovery instructions are maintained in the [Russian README](README.md).

## Development

```console
uv sync --frozen
uv run python -m unittest discover -s tests -v
```

Metashape acceptance tests must use copied projects and must never modify
irreplaceable source data.

## Security and license

The server is intended for local use. Do not expose its port or bearer token.
User scripts are disabled by default and execute with the current user's full
permissions when explicitly enabled. See [SECURITY.md](SECURITY.md).

Released under the [MIT License](LICENSE).

This is an independent community project. It is not affiliated with or
endorsed by Agisoft LLC. Agisoft and Metashape are trademarks of their
respective owners.
