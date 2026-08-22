"""The constraint that makes the backend swappable, asserted rather than trusted.

CLAUDE.md's decision is that the SDK import belongs behind one adapter and
nowhere else. That is a property of the whole source tree, so no unit test of
any single module can hold it — the way it breaks is someone reaching for the
SDK's types from the runner or a template helper, each import reasonable on its
own, and the seam being gone before anyone notices.

Reading the source rather than the import graph is deliberate: an import that
only fires on some path would not show up in `sys.modules` during a test run,
and this is exactly the kind of leak that would.
"""

import ast
import subprocess
import sys
from pathlib import Path

from workbench.config import repo_root

#: Every SDK a backend might reach for. A new backend adds its distribution
#: here at the same time it adds its module below.
VENDOR_PACKAGES = frozenset({"claude_agent_sdk", "anthropic", "openai"})

#: The one file allowed to import them, relative to the package root.
ADAPTER = Path("agents/claude.py")


def package_root() -> Path:
    return repo_root() / "src" / "workbench"


def imported_top_level_modules(source: Path) -> set[str]:
    """Top-level module names imported by a file, however they are imported.

    Walks the AST rather than matching text, so a name inside a docstring or a
    comment — this file's own prose, for instance — cannot fail the test, and a
    lazily imported SDK inside a function still counts.
    """
    tree = ast.parse(source.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        match node:
            case ast.Import():
                modules.update(alias.name.split(".")[0] for alias in node.names)
            case ast.ImportFrom() if node.module and node.level == 0:
                modules.add(node.module.split(".")[0])
    return modules


def test_only_the_adapter_imports_an_agent_sdk():
    root = package_root()
    offenders = {
        path.relative_to(root).as_posix(): sorted(
            imported_top_level_modules(path) & VENDOR_PACKAGES
        )
        for path in sorted(root.rglob("*.py"))
        if path.relative_to(root) != ADAPTER
        and "tests" not in path.relative_to(root).parts
        and imported_top_level_modules(path) & VENDOR_PACKAGES
    }

    assert offenders == {}, (
        f"An agent SDK is imported outside {ADAPTER.as_posix()}: {offenders}. "
        "Translate at the adapter and hand the rest of the app RunEventKind events."
    )


def test_the_adapter_exists_and_does_import_one():
    """Otherwise the test above passes vacuously after a rename."""
    assert VENDOR_PACKAGES & imported_top_level_modules(package_root() / ADAPTER)


def test_importing_the_package_does_not_import_an_sdk():
    """The web process resolves backend names constantly and must stay light."""
    assert not VENDOR_PACKAGES & imported_top_level_modules(package_root() / "agents/__init__.py")
    assert not VENDOR_PACKAGES & imported_top_level_modules(package_root() / "agents/registry.py")


def test_the_web_app_does_not_drag_an_sdk_into_its_process():
    """The static checks above cannot see this one.

    A module importing `workbench.agents.claude` — rather than the SDK itself —
    passes every import check above while still loading a hundred megabytes of
    vendor code into the process that serves the task tree. The only honest
    test is to import the app in a clean interpreter and look.
    """
    probe = (
        "import sys; import workbench.app; "
        f"leaked = {sorted(VENDOR_PACKAGES)!r}; "
        "print(sorted(set(leaked) & sys.modules.keys()))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, f"The probe failed to run.\n{result.stderr}"
    assert result.stdout.strip() == "[]", (
        f"Importing workbench.app pulled in {result.stdout.strip()}. "
        "Reach a backend through the registry, which imports it lazily."
    )
