# Security Policy

## Supported Versions

Security updates are provided for the latest stable release of ContextForge.

| Version | Supported |
| ------- | --------- |
| Latest stable release | ✅ |
| Older releases | ❌ |
| Development branches | ❌ |

Users are encouraged to upgrade to the latest available version before
reporting an issue.

## Reporting a Vulnerability

Please do not report suspected security vulnerabilities through public GitHub
issues or discussions.

Instead, use GitHub's private vulnerability reporting feature:

1. Open the **Security** tab of this repository.
2. Select **Advisories**.
3. Select **Report a vulnerability**.

Please include, where possible:

- A description of the vulnerability and its potential impact
- The affected ContextForge version
- Steps or a minimal example that reproduces the issue
- Relevant operating-system and Python-version information
- Any suggested mitigation or fix
- Whether the vulnerability has been disclosed elsewhere

Please avoid including real credentials, private source code, access tokens, or
other sensitive information in the report.

Reports will be reviewed as availability permits. Confirmed vulnerabilities
will be addressed according to their severity and the maintainer's capacity.
You may be contacted for additional information or to help verify a fix.

Please allow reasonable time for investigation and remediation before making a
vulnerability public.

## Security Scope

Examples of security issues that should be reported privately include:

- Reading files outside the intended repository or configured scope
- Path traversal or unsafe symbolic-link handling
- Exposure of credentials, secrets, or private source code
- Inclusion of unintended files in generated context packages or handoffs
- Execution of commands or modification of source files without explicit
  authorization
- Unsafe handling of configuration, cache, index, or output files
- Dependency or supply-chain issues with a demonstrated impact on ContextForge

General bugs, feature requests, documentation problems, and questions that do
not have a security impact may be reported through GitHub Issues or
Discussions.

## Security Model

ContextForge is designed to inspect repositories and generate bounded,
reviewable context. It should not grant coding agents permission to edit source
code or execute arbitrary commands.

Users should still review generated context packages before sharing them with
external services. ContextForge cannot guarantee that a repository contains no
credentials, secrets, personal information, or other sensitive material.
