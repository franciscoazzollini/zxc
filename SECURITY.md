# Security policy

## Supported versions

Security-sensitive fixes are applied to the **latest released minor version**
on the default branch (`main`). Older tags may not receive backports unless
someone volunteers to maintain them.

## Reporting a vulnerability

**Please do not** open a public GitHub issue for undisclosed security bugs.

Preferred options:

1. Use **GitHub Private vulnerability reporting** for this repository (if
   enabled by the maintainer).
2. Otherwise, open a **draft / confidential** discussion with maintainers as
   agreed in the project README or organization profile.

Include steps to reproduce, affected versions or commits, and impact (e.g.
memory safety, integrity of decompressed output).

## Scope

OmniComp embeds or links **Zstandard (zstd)** and uses native code; reports
about vulnerabilities **inside upstream zstd** should follow [facebook/zstd](https://github.com/facebook/zstd)
disclosure practices after confirming the issue is not specific to OmniComp’s
glue code.
