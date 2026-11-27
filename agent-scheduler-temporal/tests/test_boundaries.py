"""The session manager side must not depend on Temporal or on the executor's workflow code.

Walks the imports (not the text) of the manager-side modules and everything they import inside
this package. If the manager ever imports the workflow, the activities, the worker, the executor's
data types or `temporalio`, this fails and says which import chain did it.
"""
import ast
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "agent_scheduler_temporal"
NAME = PKG.name

# The manager side: admission, the HTTP API, the engine interface, and the agent definition.
MANAGER_SIDE = ["session_manager", "api", "engine", "agent", "agent_store"]
# Executor-side and Temporal-specific modules the manager side must never reach.
FORBIDDEN = {
    "workflow", "activities", "worker", "temporal_engine", "models", "tool_service", "sandbox", "notifier",
    "scripted_model", "chaos", "harness", "fault", "invariants", "server",
}


def imports_of(module: str) -> tuple[set[str], set[str]]:
    """(modules of this package it imports, top-level external packages it imports)."""
    tree = ast.parse((PKG / f"{module}.py").read_text())
    internal, external = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
        else:
            continue
        for name in names:
            parts = name.split(".")
            if parts[0] == NAME and len(parts) > 1 and (PKG / f"{parts[1]}.py").exists():
                internal.add(parts[1])
            elif parts[0] != NAME:
                external.add(parts[0])
    return internal, external


def closure(roots: list[str]) -> dict[str, list[str]]:
    """Every module reachable from the roots, with the chain of imports that reaches it."""
    chains = {r: [r] for r in roots}
    todo = list(roots)
    while todo:
        module = todo.pop()
        for dep in imports_of(module)[0]:
            if dep not in chains:
                chains[dep] = chains[module] + [dep]
                todo.append(dep)
    return chains


def test_the_manager_side_reaches_no_executor_module():
    reached = closure(MANAGER_SIDE)
    leaks = {m: " -> ".join(chain) for m, chain in reached.items() if m in FORBIDDEN}
    assert leaks == {}


def test_the_manager_side_does_not_import_temporal():
    reached = closure(MANAGER_SIDE)
    leaks = {m: sorted(e for e in imports_of(m)[1] if e == "temporalio") for m in reached}
    assert {m: v for m, v in leaks.items() if v} == {}


def test_the_checker_can_see_a_leak():
    """The boundary test would be worthless if it could not fail: the Temporal adapter does import
    temporalio and the workflow, and the checker must say so."""
    internal, external = imports_of("temporal_engine")
    assert "workflow" in internal and "models" in internal
    assert "temporalio" in external
    assert "temporal_engine" in closure(["server"])  # and it follows chains
