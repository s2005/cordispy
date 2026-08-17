"""The declarative loader and hot module replacement -- paper section 5.2.

Two layers sit on top of the core runtime here.

:class:`Loader` turns a persistent configuration record into fibers and keeps
the two in step. An orchestrator states *what* should be composed; the loader
works out which imperative fiber operations that implies, dispatching on which
of an entry's fields actually changed so that a revision costs the least
disruption it can (section 5.2.1).

:class:`Hmr` applies the same revertible-effect pattern at the module level. A
fiber already bounds all of its component's effects, so replacing a module needs
no acceptance boundaries: retire the fiber, re-import, instantiate a fresh one.
The reload is transactional -- if any module fails to import, every cache is
restored and every touched entry is rebuilt from the backup (section 5.2.2).

A minimal use::

    from cordispy import Context
    from cordispy.loader import Loader, read_config

    root = Context()
    loader = Loader.trusted(root, base=".")
    await loader.reconcile(read_config("app.yaml"))
    print("\\n".join(loader.describe()))
"""

from __future__ import annotations

from .entry import ENTRY_FIELDS, Entry, EntryOptions, LoaderError, RealmManager
from .group import EntryGroup, group_component
from .hmr import (
    Hmr,
    HmrResult,
    ImportGraph,
    ModuleNode,
    classify,
    dependencies,
    detect,
    module_of,
)
from .loader import (
    BUILTIN_GROUP,
    BUILTIN_INCLUDE,
    Loader,
    LoaderPolicy,
    Resolver,
    Tree,
    import_target,
    include_component,
    normalize_document,
    read_config,
)

__all__ = [
    "BUILTIN_GROUP",
    "BUILTIN_INCLUDE",
    "ENTRY_FIELDS",
    "Entry",
    "EntryGroup",
    "EntryOptions",
    "Hmr",
    "HmrResult",
    "ImportGraph",
    "Loader",
    "LoaderError",
    "LoaderPolicy",
    "ModuleNode",
    "RealmManager",
    "Resolver",
    "Tree",
    "classify",
    "dependencies",
    "detect",
    "group_component",
    "import_target",
    "include_component",
    "module_of",
    "normalize_document",
    "read_config",
]
