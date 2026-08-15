"""Runnable examples for ``cordispy``.

Five scripts sit next to this file. Each is a standalone CLI built with
``argparse`` and named parameters only; run any of them with ``--help``::

    uv run python examples/run_effects.py --help

``run_effects.py``
    The temporal dimension on its own: the five effect forms and LIFO recovery.

``run_services.py``
    The spatial dimension: a provider, a consumer, and activation driven by the
    coeffect declaration rather than by call order.

``run_hotswap.py``
    A provider replaced under a running consumer.

``run_benefit.py``
    The measured comparison. The same application is built twice -- once on this
    runtime (``examples.harness``) and once on a conventional plugin registry
    (``examples.naive``) -- and four scenarios are run against both, with the
    residue counted in the live process.

``run_loader.py``
    Declarative configuration reconciled by the loader.

Two packages support them:

``examples.harness``
    The demonstration application, in which every feature is a component. It
    also owns the leaf services (the request dispatcher, the stores, the sqlite
    journal) and the measurement probe, both of which the conventional
    implementation imports as well -- the two implementations differ only in how
    the features are *composed*, never in what they do.

``examples.naive``
    The same application on a conventional registry.
"""

from __future__ import annotations

__all__: list[str] = []
