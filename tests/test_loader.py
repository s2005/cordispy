"""The declarative loader -- paper section 5.2.1.

The claim under test is that reconciliation is *incremental*: a revised
configuration is realized with the least disruptive operation each changed field
allows, never by tearing the tree down and rebuilding it. Almost every test here
therefore asserts on fiber uids as well as on behaviour -- a uid is drawn fresh
and never reused, so an unchanged uid is proof that a fiber was left alone.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cordispy import Context, FiberState, plugin
from cordispy.loader import (
    BUILTIN_GROUP,
    BUILTIN_INCLUDE,
    EntryOptions,
    Loader,
    LoaderError,
    LoaderPolicy,
    read_config,
)

# ---------------------------------------------------------------------------
# the components these tests compose
# ---------------------------------------------------------------------------


@plugin(name="store", provide=["store"])
def store(ctx: Context, config: Any) -> Any:
    data: dict[str, Any] = {"routes": {}, "log": []}
    ctx.set("store", data)

    def undo() -> None:
        data["routes"].clear()

    return undo


def make_tool(label: str) -> Any:
    """A component that registers one route and removes it on unload."""

    @plugin(name=label, inject=["store"])
    def tool(ctx: Context, config: Any) -> Any:
        settings = config or {}
        routes = ctx.store["routes"]
        route = "/" + settings.get("path", label)
        routes[route] = settings.get("reply", label)
        return lambda: routes.pop(route, None)

    return tool


@plugin(name="probe", inject=["store"])
def probe(ctx: Context, config: Any) -> Any:
    """Reports its own read-time interception metadata when asked."""
    route = "/" + (config or {}).get("path", "meta")
    routes = ctx.store["routes"]
    routes[route] = lambda: ctx.interception("store")
    return lambda: routes.pop(route, None)


COMPONENTS: dict[str, Any] = {
    "demo:store": store,
    "demo:probe": probe,
    "demo:alpha": make_tool("alpha"),
    "demo:beta": make_tool("beta"),
    "demo:gamma": make_tool("gamma"),
}


def build(root: Context, **kwargs: Any) -> Loader:
    return Loader(root, resolve=COMPONENTS.get, **kwargs)


def include_policy(root: Path, **limits: Any) -> LoaderPolicy:
    return LoaderPolicy(include_roots=(root,), **limits)


def make_directory_link(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError as error:
        if os.name != "nt":
            pytest.fail(f"directory links are unavailable: {error}")
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/j", str(link), str(target)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr or result.stdout


def uids(loader: Loader) -> dict[str, int | None]:
    return {entry.id: entry.uid for entry in loader.entries()}


def routes(root: Context) -> dict[str, Any]:
    data = root.get("store")
    return {} if data is None else dict(data["routes"])


def flat(*children: Any) -> list[Any]:
    return [{"id": "store", "name": "demo:store"}, *children]


def grouped(*children: Any) -> list[Any]:
    return flat({"id": "tools", "name": BUILTIN_GROUP, "config": list(children)})


# ---------------------------------------------------------------------------
# the entry record (Definition 74)
# ---------------------------------------------------------------------------


def test_an_entry_needs_an_id_because_the_id_is_the_reconciliation_key() -> None:
    with pytest.raises(LoaderError, match="reconciliation key"):
        EntryOptions.parse({"name": "demo:alpha"})


def test_an_entry_rejects_fields_it_does_not_declare() -> None:
    with pytest.raises(LoaderError, match="unknown entry fields: colour"):
        EntryOptions.parse({"id": "a", "name": "demo:alpha", "colour": "red"})


def test_an_entry_id_may_not_contain_the_tree_separator() -> None:
    with pytest.raises(LoaderError, match="namespaces an id"):
        EntryOptions.parse({"id": "outer:inner", "name": "demo:alpha"})


def test_the_diff_reports_exactly_the_fields_that_moved() -> None:
    first = EntryOptions.parse({"id": "a", "name": "demo:alpha", "config": {"reply": "one"}})
    second = EntryOptions.parse({"id": "a", "name": "demo:alpha", "config": {"reply": "two"}})
    assert first.diff(second) == frozenset({"config"})
    assert first.diff(first) == frozenset()


# ---------------------------------------------------------------------------
# reading documents
# ---------------------------------------------------------------------------


def test_yaml_and_json_documents_parse_to_the_same_thing(tmp_path: Path) -> None:
    document = {"plugins": [{"id": "store", "name": "demo:store"}]}
    yaml_file = tmp_path / "app.yaml"
    yaml_file.write_text(
        "plugins:\n  - id: store\n    name: demo:store\n",
        encoding="utf-8",
    )
    json_file = tmp_path / "app.json"
    json_file.write_text(json.dumps(document, indent=2), encoding="utf-8")

    assert read_config(yaml_file) == read_config(json_file) == document


def test_an_unknown_file_extension_is_refused(tmp_path: Path) -> None:
    other = tmp_path / "app.ini"
    other.write_text("[plugins]\n", encoding="utf-8")
    with pytest.raises(LoaderError, match=r"expected a \.yaml"):
        read_config(other)


async def test_a_document_file_is_realized_as_a_fiber_tree(tmp_path: Path) -> None:
    config = tmp_path / "app.json"
    config.write_text(json.dumps({"plugins": grouped({"id": "alpha", "name": "demo:alpha"})}), "utf-8")

    root = Context()
    loader = build(root)
    await loader.load(config)

    assert loader.describe() == [
        "- store [demo:store] ACTIVE #1",
        "- tools [cordispy:group] ACTIVE #2",
        "  - alpha [demo:alpha] ACTIVE #3",
    ]
    assert routes(root) == {"/alpha": "alpha"}


async def test_imports_are_denied_without_calling_the_import_system(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[str] = []

    def record_import(name: str) -> Any:
        called.append(name)
        return SimpleNamespace(default=make_tool("imported"))

    monkeypatch.setattr("cordispy.loader.loader.importlib.import_module", record_import)
    root = Context()
    loader = build(root)
    await loader.reconcile(flat({"id": "imported", "name": "untrusted.module"}))

    error = loader.entry("imported").error
    assert isinstance(error, LoaderError)
    assert "imports are disabled" in str(error)
    assert called == []


async def test_a_resolver_remains_the_secure_component_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_import(name: str) -> Any:
        pytest.fail(f"resolver-served component unexpectedly imported {name}")

    monkeypatch.setattr("cordispy.loader.loader.importlib.import_module", fail_import)
    root = Context()
    loader = build(root)
    await loader.reconcile(flat({"id": "alpha", "name": "demo:alpha"}))

    assert routes(root) == {"/alpha": "alpha"}


async def test_the_named_trusted_loader_enables_imports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[str] = []

    def record_import(name: str) -> Any:
        called.append(name)
        return SimpleNamespace(default=make_tool("imported"))

    monkeypatch.setattr("cordispy.loader.loader.importlib.import_module", record_import)
    root = Context()
    loader = Loader.trusted(root, base=tmp_path, resolve=COMPONENTS.get)
    await loader.reconcile(flat({"id": "imported", "name": "trusted.module"}))

    assert called == ["trusted.module"]
    assert routes(root) == {"/imported": "imported"}


# ---------------------------------------------------------------------------
# the reconciliation ladder
# ---------------------------------------------------------------------------


async def test_disabled_unloads_only_that_entry_and_clearing_it_reloads() -> None:
    root = Context()
    loader = build(root)
    await loader.reconcile(
        grouped({"id": "alpha", "name": "demo:alpha"}, {"id": "beta", "name": "demo:beta"})
    )
    before = uids(loader)
    assert set(routes(root)) == {"/alpha", "/beta"}

    await loader.reconcile(
        grouped(
            {"id": "alpha", "name": "demo:alpha", "disabled": True},
            {"id": "beta", "name": "demo:beta"},
        )
    )
    after = uids(loader)

    assert set(routes(root)) == {"/beta"}, "the disabled entry withdrew its route"
    assert after["alpha"] is None
    assert loader.entry("alpha").status == "DISABLED"
    assert {key: value for key, value in after.items() if key != "alpha"} == {
        key: value for key, value in before.items() if key != "alpha"
    }, "no sibling and no ancestor may be touched"

    await loader.reconcile(
        grouped({"id": "alpha", "name": "demo:alpha"}, {"id": "beta", "name": "demo:beta"})
    )
    revived = uids(loader)
    assert set(routes(root)) == {"/alpha", "/beta"}
    assert revived["alpha"] != before["alpha"], "a reloaded entry gets a fresh fiber"
    assert revived["beta"] == before["beta"]
    assert revived["tools"] == before["tools"]


async def test_a_config_change_reconciles_that_entry_without_restarting_its_group() -> None:
    root = Context()
    loader = build(root)
    await loader.reconcile(
        grouped(
            {"id": "alpha", "name": "demo:alpha", "config": {"reply": "one"}},
            {"id": "beta", "name": "demo:beta"},
        )
    )
    before = uids(loader)

    await loader.reconcile(
        grouped(
            {"id": "alpha", "name": "demo:alpha", "config": {"reply": "two"}},
            {"id": "beta", "name": "demo:beta"},
        )
    )
    after = uids(loader)

    assert routes(root)["/alpha"] == "two"
    assert after["alpha"] != before["alpha"], "the entry itself is reconciled"
    assert after["beta"] == before["beta"], "its sibling is untouched"
    assert after["tools"] == before["tools"], "its group is not restarted"
    assert after["store"] == before["store"], "and neither is its provider"


async def test_a_group_config_change_is_a_keyed_diff_over_child_ids() -> None:
    root = Context()
    loader = build(root)
    await loader.reconcile(
        grouped({"id": "alpha", "name": "demo:alpha"}, {"id": "beta", "name": "demo:beta"})
    )
    before = uids(loader)
    fibers = len(root.registry)

    await loader.reconcile(
        grouped(
            {"id": "alpha", "name": "demo:alpha"},
            {"id": "beta", "name": "demo:beta"},
            {"id": "gamma", "name": "demo:gamma"},
        )
    )

    assert len(root.registry) == fibers + 1, "the diff creates exactly one fiber"
    after = uids(loader)
    assert {key: after[key] for key in before} == before, "every surviving child keeps its fiber"
    assert set(routes(root)) == {"/alpha", "/beta", "/gamma"}

    await loader.reconcile(grouped({"id": "alpha", "name": "demo:alpha"}))

    assert set(routes(root)) == {"/alpha"}, "the removed children withdrew their routes"
    assert uids(loader)["alpha"] == before["alpha"]
    assert len(root.registry) == fibers - 1


async def test_changing_the_component_rebuilds_the_entry() -> None:
    root = Context()
    loader = build(root)
    await loader.reconcile(flat({"id": "tool", "name": "demo:alpha"}))
    before = uids(loader)

    await loader.reconcile(flat({"id": "tool", "name": "demo:beta"}))

    assert set(routes(root)) == {"/beta"}
    assert uids(loader)["tool"] != before["tool"]
    assert uids(loader)["store"] == before["store"]


async def test_intercept_is_updated_in_place_with_no_reload() -> None:
    """Interception metadata is read-time, so revising it costs no transition."""
    root = Context()
    loader = build(root)
    await loader.reconcile(flat({"id": "probe", "name": "demo:probe"}))
    before = uids(loader)
    assert routes(root)["/meta"]() == {}

    await loader.reconcile(
        flat({"id": "probe", "name": "demo:probe", "intercept": {"store": {"scope": "admin"}}})
    )

    assert uids(loader)["probe"] == before["probe"], "no reload for a read-time annotation"
    assert routes(root)["/meta"]() == {"scope": "admin"}

    await loader.reconcile(flat({"id": "probe", "name": "demo:probe"}))
    assert uids(loader)["probe"] == before["probe"]
    assert routes(root)["/meta"]() == {}


async def test_an_intercept_annotation_does_not_leak_to_a_sibling_entry() -> None:
    """An annotation is the entry's own, even when the entry derives no context."""
    root = Context()
    loader = build(root)
    await loader.reconcile(
        flat(
            {
                "id": "left",
                "name": "demo:probe",
                "config": {"path": "left"},
                "intercept": {"store": {"scope": "admin"}},
            },
            {"id": "right", "name": "demo:probe", "config": {"path": "right"}},
        )
    )

    assert routes(root)["/left"]() == {"scope": "admin"}
    assert routes(root)["/right"]() == {}

    await loader.reconcile(
        flat(
            {"id": "left", "name": "demo:probe", "config": {"path": "left"}},
            {
                "id": "right",
                "name": "demo:probe",
                "config": {"path": "right"},
                "intercept": {"store": {"scope": "guest"}},
            },
        )
    )

    assert routes(root)["/left"]() == {}
    assert routes(root)["/right"]() == {"scope": "guest"}


async def test_isolating_a_key_moves_the_entry_into_a_realm_of_its_own() -> None:
    root = Context()
    loader = build(root)
    await loader.reconcile(flat({"id": "tool", "name": "demo:alpha"}))
    before = uids(loader)
    assert set(routes(root)) == {"/alpha"}

    await loader.reconcile(flat({"id": "tool", "name": "demo:alpha", "isolate": {"store": True}}))

    tool = loader.entry("tool")
    assert tool.fiber is not None
    assert tool.fiber.state is FiberState.PENDING, "nothing provides store in the private realm"
    assert uids(loader)["tool"] != before["tool"]
    assert routes(root) == {}, "the isolated entry withdrew its route"

    await loader.reconcile(flat({"id": "tool", "name": "demo:alpha"}))
    assert loader.entry("tool").status == "ACTIVE"
    assert set(routes(root)) == {"/alpha"}


async def test_a_shared_isolate_label_puts_a_provider_and_a_consumer_in_one_realm() -> None:
    root = Context()
    loader = build(root)
    await loader.reconcile(
        [
            {"id": "store", "name": "demo:store", "isolate": {"store": "wing"}},
            {"id": "inside", "name": "demo:alpha", "isolate": {"store": "wing"}},
            {"id": "outside", "name": "demo:beta"},
        ]
    )

    inside = loader.entry("inside")
    outside = loader.entry("outside")
    assert inside.fiber is not None
    assert outside.fiber is not None
    assert inside.fiber.state is FiberState.ACTIVE, "it shares the realm with the provider"
    assert outside.fiber.state is FiberState.PENDING, "it resolves store in the default realm"
    assert root.get("store") is None, "the default realm holds nothing at all"


async def test_reassigning_the_providers_realm_takes_the_binding_with_it() -> None:
    """Algorithm 7: the dependents that move are the ones the provider moved with.

    The provider's own binding follows it into the new realm, so a consumer left
    behind in the default realm loses it and a consumer that follows regains it.
    """
    root = Context()
    loader = build(root)
    await loader.reconcile([{"id": "store", "name": "demo:store"}, {"id": "tool", "name": "demo:alpha"}])
    assert loader.entry("tool").status == "ACTIVE"
    settled = loader.entry("tool").uid

    await loader.reconcile(
        [
            {"id": "store", "name": "demo:store", "isolate": {"store": "wing"}},
            {"id": "tool", "name": "demo:alpha"},
        ]
    )
    tool = loader.entry("tool")
    assert tool.fiber is not None
    # INACTIVE, not PENDING: the consumer loaded once and has now fully
    # recovered. Reassigning the provider's realm rebuilt the provider only --
    # the consumer was deactivated by the withdrawal, keeping its own fiber.
    assert tool.fiber.state is FiberState.INACTIVE, "it was left behind in the default realm"
    assert tool.uid == settled, "the consumer entry itself was never reconciled"
    assert routes(root) == {}
    assert root.get("store") is None, "the binding moved out of the default realm"

    await loader.reconcile(
        [
            {"id": "store", "name": "demo:store", "isolate": {"store": "wing"}},
            {"id": "tool", "name": "demo:alpha", "isolate": {"store": "wing"}},
        ]
    )
    assert loader.entry("tool").status == "ACTIVE", "following the provider restores the binding"


async def test_an_entry_moved_between_groups_keeps_its_identity() -> None:
    root = Context()
    loader = build(root)
    await loader.reconcile(
        flat(
            {
                "id": "left",
                "name": BUILTIN_GROUP,
                "config": [{"id": "tool", "name": "demo:alpha"}],
            },
            {"id": "right", "name": BUILTIN_GROUP, "config": []},
        )
    )
    tool = loader.entry("tool")
    assert tool.parent.owner is not None
    assert tool.parent.owner.options.id == "left"

    await loader.reconcile(
        flat(
            {"id": "left", "name": BUILTIN_GROUP, "config": []},
            {
                "id": "right",
                "name": BUILTIN_GROUP,
                "config": [{"id": "tool", "name": "demo:alpha"}],
            },
        )
    )

    assert loader.entry("tool") is tool, "the same entry, not a replacement"
    assert tool.parent.owner is not None
    assert tool.parent.owner.options.id == "right"
    assert set(routes(root)) == {"/alpha"}, "and it never stopped serving on the far side"


async def test_include_grafts_a_file_in_and_namespaces_its_ids(tmp_path: Path) -> None:
    nested = tmp_path / "tools.json"
    nested.write_text(json.dumps([{"id": "alpha", "name": "demo:alpha"}]), encoding="utf-8")

    root = Context()
    loader = build(root, base=tmp_path, policy=include_policy(tmp_path))
    await loader.reconcile(flat({"id": "extra", "name": BUILTIN_INCLUDE, "config": {"path": "tools.json"}}))

    assert [entry.id for entry in loader.entries()] == ["store", "extra", "extra:alpha"]
    assert set(routes(root)) == {"/alpha"}

    await loader.reconcile(flat())
    assert [entry.id for entry in loader.entries()] == ["store"]
    assert routes(root) == {}


async def test_includes_are_disabled_by_default_even_with_a_base(tmp_path: Path) -> None:
    nested = tmp_path / "tools.json"
    nested.write_text("[]", encoding="utf-8")
    loader = build(Context(), base=tmp_path)

    with pytest.raises(LoaderError, match="includes are disabled"):
        await loader.reconcile([{"id": "extra", "name": BUILTIN_INCLUDE, "config": {"path": "tools.json"}}])


@pytest.mark.parametrize("path_style", ["absolute", "relative"])
async def test_an_include_cannot_escape_its_allowed_root(tmp_path: Path, path_style: str) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("[]", encoding="utf-8")
    requested = str(outside) if path_style == "absolute" else "../outside.json"
    loader = build(Context(), base=allowed, policy=include_policy(allowed))

    with pytest.raises(LoaderError, match="escapes the configured include roots"):
        await loader.reconcile([{"id": "escape", "name": BUILTIN_INCLUDE, "config": {"path": requested}}])


async def test_an_include_cannot_escape_through_a_link(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "tools.json").write_text("[]", encoding="utf-8")
    link = allowed / "linked"
    make_directory_link(link, outside)
    loader = build(Context(), base=allowed, policy=include_policy(allowed))

    with pytest.raises(LoaderError, match="escapes the configured include roots"):
        await loader.reconcile(
            [{"id": "escape", "name": BUILTIN_INCLUDE, "config": {"path": "linked/tools.json"}}]
        )


@pytest.mark.parametrize("kind", ["suffix", "directory"])
async def test_an_include_must_be_a_supported_regular_file(tmp_path: Path, kind: str) -> None:
    if kind == "suffix":
        target = tmp_path / "tools.txt"
        target.write_text("[]", encoding="utf-8")
        message = "must name a .json"
    else:
        target = tmp_path / "tools.json"
        target.mkdir()
        message = "not a regular file"
    loader = build(Context(), base=tmp_path, policy=include_policy(tmp_path))

    with pytest.raises(LoaderError, match=message):
        await loader.reconcile([{"id": "extra", "name": BUILTIN_INCLUDE, "config": {"path": target.name}}])


async def test_a_root_file_cannot_include_itself(tmp_path: Path) -> None:
    root_file = tmp_path / "root.json"
    root_file.write_text(
        json.dumps([{"id": "again", "name": BUILTIN_INCLUDE, "config": {"path": "root.json"}}]),
        encoding="utf-8",
    )
    loader = build(Context(), policy=include_policy(tmp_path))

    with pytest.raises(LoaderError, match="include cycle"):
        await loader.load(root_file)
    assert len(loader.ctx.registry) == 1


async def test_an_indirect_include_cycle_is_rejected(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(
        json.dumps([{"id": "second", "name": BUILTIN_INCLUDE, "config": {"path": "second.json"}}]),
        encoding="utf-8",
    )
    second.write_text(
        json.dumps([{"id": "first", "name": BUILTIN_INCLUDE, "config": {"path": "first.json"}}]),
        encoding="utf-8",
    )
    loader = build(Context(), policy=include_policy(tmp_path))

    with pytest.raises(LoaderError, match="include cycle"):
        await loader.load(first)
    assert len(loader.ctx.registry) == 1


async def test_a_noncyclic_file_may_be_included_more_than_once(tmp_path: Path) -> None:
    nested = tmp_path / "tools.json"
    nested.write_text(json.dumps([{"id": "alpha", "name": "demo:alpha"}]), encoding="utf-8")
    loader = build(Context(), base=tmp_path, policy=include_policy(tmp_path))

    await loader.reconcile(
        [
            {"id": "left", "name": BUILTIN_INCLUDE, "config": {"path": "tools.json"}},
            {"id": "right", "name": BUILTIN_INCLUDE, "config": {"path": "tools.json"}},
        ]
    )

    assert [entry.id for entry in loader.entries()] == [
        "left",
        "left:alpha",
        "right",
        "right:alpha",
    ]


async def test_root_and_included_files_obey_the_byte_limit(tmp_path: Path) -> None:
    oversized = tmp_path / "oversized.json"
    oversized.write_text("[" + " " * 128 + "]", encoding="utf-8")
    policy = include_policy(tmp_path, max_file_bytes=32)
    loader = build(Context(), base=tmp_path, policy=policy)

    with pytest.raises(LoaderError, match="max_file_bytes=32"):
        await loader.load(oversized)
    with pytest.raises(LoaderError, match="max_file_bytes=32"):
        await loader.reconcile(
            [{"id": "large", "name": BUILTIN_INCLUDE, "config": {"path": "oversized.json"}}]
        )


async def test_document_limits_are_enforced_before_reconciliation(tmp_path: Path) -> None:
    nested = tmp_path / "nested.json"
    nested.write_text(
        json.dumps([{"id": "deeper", "name": BUILTIN_INCLUDE, "config": {"path": "leaf.json"}}]),
        encoding="utf-8",
    )
    (tmp_path / "leaf.json").write_text("[]", encoding="utf-8")

    with pytest.raises(LoaderError, match="max_entries=1"):
        await build(Context(), policy=LoaderPolicy(max_entries=1)).reconcile(
            [
                {"id": "one", "name": "demo:alpha"},
                {"id": "two", "name": "demo:beta"},
            ]
        )

    include_limited = build(
        Context(),
        base=tmp_path,
        policy=include_policy(tmp_path, max_include_depth=1),
    )
    with pytest.raises(LoaderError, match="max_include_depth=1"):
        await include_limited.reconcile(
            [{"id": "nested", "name": BUILTIN_INCLUDE, "config": {"path": "nested.json"}}]
        )

    file_limited = build(
        Context(),
        base=tmp_path,
        policy=include_policy(tmp_path, max_included_files=1),
    )
    with pytest.raises(LoaderError, match="max_included_files=1"):
        await file_limited.reconcile(
            [
                {"id": "first", "name": BUILTIN_INCLUDE, "config": {"path": "leaf.json"}},
                {"id": "second", "name": BUILTIN_INCLUDE, "config": {"path": "leaf.json"}},
            ]
        )

    deep: Any = "value"
    for _ in range(8):
        deep = {"nested": deep}
    depth_limited = build(Context(), policy=LoaderPolicy(max_nesting_depth=6))
    with pytest.raises(LoaderError, match="max_nesting_depth=6"):
        await depth_limited.reconcile([{"id": "deep", "name": "demo:alpha", "config": deep}])


@pytest.mark.parametrize("container_kind", ["mapping", "sequence"])
async def test_self_referential_python_documents_are_rejected(container_kind: str) -> None:
    if container_kind == "mapping":
        record: dict[str, Any] = {"id": "loop", "name": "demo:alpha"}
        record["config"] = record
        document: Any = [record]
    else:
        sequence: list[Any] = []
        sequence.append(sequence)
        document = sequence
    loader = build(Context())

    with pytest.raises(LoaderError, match="self-referential"):
        await loader.reconcile(document)
    assert len(loader.ctx.registry) == 1


async def test_preflight_failure_does_not_change_the_live_tree(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("[]", encoding="utf-8")
    root = Context()
    loader = build(root, base=allowed, policy=include_policy(allowed))
    await loader.reconcile(flat({"id": "alpha", "name": "demo:alpha"}))
    before_description = loader.describe()
    before_uids = uids(loader)
    before_fibers = len(root.registry)
    before_routes = routes(root)

    with pytest.raises(LoaderError, match="escapes the configured include roots"):
        await loader.reconcile(
            flat(
                {"id": "beta", "name": "demo:beta"},
                {"id": "escape", "name": BUILTIN_INCLUDE, "config": {"path": "../outside.json"}},
            )
        )

    assert loader.describe() == before_description
    assert uids(loader) == before_uids
    assert len(root.registry) == before_fibers
    assert routes(root) == before_routes


# ---------------------------------------------------------------------------
# failure containment and teardown
# ---------------------------------------------------------------------------


async def test_a_denied_import_does_not_derail_its_siblings() -> None:
    root = Context()
    loader = build(root)
    await loader.reconcile(
        flat({"id": "missing", "name": "no_such_module:thing"}, {"id": "beta", "name": "demo:beta"})
    )

    broken = loader.entry("missing")
    assert isinstance(broken.error, LoaderError)
    assert "imports are disabled" in str(broken.error)
    assert broken.status == "FAILED"
    assert loader.entry("beta").status == "ACTIVE"
    assert set(routes(root)) == {"/beta"}


async def test_stopping_the_loader_returns_the_system_to_its_prior_state() -> None:
    root = Context()
    loader = build(root)
    await loader.reconcile(
        grouped({"id": "alpha", "name": "demo:alpha"}, {"id": "beta", "name": "demo:beta"})
    )
    assert len(root.registry) == 5

    await loader.stop()

    assert loader.describe() == []
    assert root.get("store") is None
    assert len(root.registry) == 1, "only the root fiber is left"
    assert len(loader.realms) == 0


async def test_a_reconcile_that_changes_nothing_touches_nothing() -> None:
    root = Context()
    loader = build(root)
    document = grouped({"id": "alpha", "name": "demo:alpha", "config": {"reply": "one"}})
    await loader.reconcile(document)
    before = uids(loader)

    await loader.reconcile(document)

    assert uids(loader) == before
    for entry in loader.entries():
        assert entry.fiber is not None
        assert entry.fiber.inertia is None
