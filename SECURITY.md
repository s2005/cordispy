# Security Policy

## Supported Versions

Security fixes are made against the latest published `cordispy` release and the current `main` branch. Older pre-1.0 releases may require upgrading to receive a fix.

## Reporting a Vulnerability

Use [GitHub private vulnerability reporting](https://github.com/s2005/cordispy/security/advisories/new). Do not open a public issue for a suspected vulnerability or include secrets, exploit details, or affected production data in a public discussion.

The maintainer will acknowledge the report, confirm its scope, coordinate a fix and disclosure with the reporter, and publish an advisory when users can upgrade safely. The policy does not promise a fixed response schedule because severity and coordination needs vary by report.

## Trust Boundary

Python components are trusted executable code. This includes components returned by a resolver, modules enabled through `Loader.trusted`, and source reloaded by `Hmr`. Importing or reloading one of these components runs its Python module code with the permissions of the host process.

The ordinary `Loader` constructor is the boundary for less-trusted configuration. It denies Python import fallback and file includes by default. Applications may provide an explicit resolver allowlist, and may enable includes only under resolved `LoaderPolicy.include_roots`. File size, nesting, entry, include-depth, and included-file limits are enforced before the running fiber tree is changed.

This library does not sandbox trusted Python components. Applications that accept configuration from another trust domain must keep import fallback disabled, constrain include roots, and avoid returning attacker-selected components from their resolver.

## Release Controls

Before publishing a release, the maintainer must verify these repository controls in GitHub:

- The `pypi` environment restricts deployment to intended version tags and requires appropriate approval.
- Version tags can be created only through the intended maintainer process and resolve to commits contained in `main`.
- Branch protection is enabled for `main`.
- Private vulnerability reporting is enabled.
- GitHub Actions policy requires full-length commit SHAs where the repository settings support it.
- The completed PyPI publication displays a valid provenance attestation.

The repository workflow enforces the source-controlled portion of this boundary: immutable actions, an unprivileged build job, hash-constrained build dependencies, locked release tools, verified artifacts, and a minimal OIDC publishing job.
