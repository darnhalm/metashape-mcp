# Security policy

## Supported version

Security fixes are applied to the latest released version. Older snapshots are
not maintained.

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability. Use GitHub's
private vulnerability reporting feature for this repository. If that feature
is unavailable, contact the maintainer privately through the GitHub profile.

Include the affected version, operating system, Metashape version, a minimal
reproduction, expected impact, and whether the issue can expose the local
bearer token, escape an allowed path, modify source data, or bypass a safety
confirmation. Remove project files, photographs, tokens, personal paths, and
other sensitive material from reports and logs.

## Security boundaries

- The service binds to loopback and requires a per-port bearer token for MCP
  calls. Do not expose the endpoint through a proxy or copy the token into a
  repository.
- Allowed working roots constrain project input and output paths. Do not grant
  an entire home directory merely to avoid configuring a project directory.
- User scripts are disabled by default. Enabling them grants arbitrary Python
  code the permissions of the current user; review the SHA-256 confirmation
  and save the project before execution.
- Restore points and quarantine reduce accidental data loss, but they are not
  a backup. Keep independent backups of irreplaceable source images and
  projects.
