"""The provider boundary, enforced by reading the import graph.

``ruff``'s ``TID251`` bans the vendor libraries and the concrete-provider import
at lint time, and that is the fast feedback. It cannot express the *reverse*
rule -- that nothing under ``providers/`` may import the app -- because a
banned-api table is global rather than directional, and the provider package has
to be exempted from the table wholesale in order to import its own vendor
libraries. This module is the mechanism for that half.

It is also a second opinion on the half ruff does cover, which matters because
the ban lives in ``pyproject.toml`` and a lint config is one deletion away from
being gone without anything failing. The last test here asserts the two agree.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "garmin_health"
TESTS = ROOT / "tests"
PROVIDERS = SRC / "providers"
GARMINDB = PROVIDERS / "garmindb"

# Everything that knows what GarminDB is on disk. A module importing any of these
# is, by definition, provider code wherever it happens to live.
VENDOR = ("garmindb", "garminconnect", "idbutils", "fitfile", "sqlalchemy", "garth")

PROVIDER_PACKAGE = "garmin_health.providers.garmindb"

# A provider is a leaf. Importing any of these would make the boundary circular
# and, worse, would let a provider reach the serving layer's decisions rather
# than answering to them.
APP_LAYER = ("garmin_health.app", "garmin_health.service", "garmin_health.routes")


def imports_in(path: Path) -> list[tuple[str, int]]:
    """Every module named by an import in ``path``, with its line number.

    ``ast.walk`` rather than a scan of the top level, precisely so that a lazy
    import inside a function -- which is how the selector and the app defer their
    heavy imports -- is caught like any other.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            found.append((node.module, node.lineno))
    return found


def python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def is_under(path: Path, directory: Path) -> bool:
    return directory in path.parents


def names(module: str, prefixes: tuple[str, ...]) -> bool:
    """True if ``module`` is one of ``prefixes`` or lives inside one."""
    return any(module == p or module.startswith(f"{p}.") for p in prefixes)


def offences(files: list[Path], predicate: object) -> list[str]:
    hits = []
    for path in files:
        for module, line in imports_in(path):
            if predicate(module):  # type: ignore[operator]
                hits.append(f"{path.relative_to(ROOT)}:{line} imports {module}")
    return hits


class TestVendorLibraries:
    def test_only_the_provider_package_imports_them(self) -> None:
        """The rule AGENTS.md states: everything crossing the boundary is a spec
        type, a ports type, or a stdlib type -- never a session or a DB handle."""
        outside = [p for p in python_files(SRC) if not is_under(p, GARMINDB)]
        assert offences(outside, lambda m: m.split(".")[0] in VENDOR) == []

    def test_the_provider_package_really_does_import_them(self) -> None:
        """Guards the test above against passing for the wrong reason. If this
        ever fails, the vendor code moved and the ban is now checking nothing."""
        inside = python_files(GARMINDB)
        assert offences(inside, lambda m: m.split(".")[0] in VENDOR) != []

    def test_generic_tests_do_not_import_them_either(self) -> None:
        """A corpus fixture in a generic test would quietly make the generic
        suite depend on GarminDB being installed."""
        outside = [p for p in python_files(TESTS) if not is_under(p, TESTS / "providers")]
        assert offences(outside, lambda m: m.split(".")[0] in VENDOR) == []


class TestConcreteProviderNaming:
    def test_only_the_selector_names_a_provider(self) -> None:
        """``build_provider`` is the one place a provider is named. Anywhere else
        and ``HEALTH_PROVIDER`` stops being able to choose."""
        allowed = {PROVIDERS / "__init__.py"}
        candidates = [
            p for p in python_files(SRC) if not is_under(p, GARMINDB) and p not in allowed
        ]
        assert offences(candidates, lambda m: names(m, (PROVIDER_PACKAGE,))) == []

    def test_the_selector_is_still_the_one_that_does(self) -> None:
        """The companion check: if the selector stops importing a provider, the
        test above is vacuous and the app builds nothing."""
        selector = PROVIDERS / "__init__.py"
        assert offences([selector], lambda m: names(m, (PROVIDER_PACKAGE,))) != []

    def test_generic_tests_do_not_name_a_provider(self) -> None:
        outside = [p for p in python_files(TESTS) if not is_under(p, TESTS / "providers")]
        assert offences(outside, lambda m: names(m, (PROVIDER_PACKAGE,))) == []


class TestProvidersAreLeaves:
    def test_no_provider_imports_the_app_layer(self) -> None:
        """The rule ruff cannot state. A provider answers to the serving layer's
        decisions; it must not reach into them.

        This is why the owner page's shell lives in ``setup_page.py`` rather than
        under ``routes/``: the provider re-renders the whole page when one of its
        own forms fails validation, so it has to import the shell.
        """
        assert offences(python_files(PROVIDERS), lambda m: names(m, APP_LAYER)) == []


class TestTheLintRuleAndThisTestAgree:
    """Two mechanisms, one rule. Either alone can be removed by accident."""

    def banned(self) -> set[str]:
        manifest = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        lint = manifest["tool"]["ruff"]["lint"]
        return set(lint["flake8-tidy-imports"]["banned-api"])

    def test_the_rule_is_switched_on(self) -> None:
        manifest = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        assert "TID251" in manifest["tool"]["ruff"]["lint"]["select"]

    def test_every_vendor_library_is_banned_at_lint_time(self) -> None:
        assert set(VENDOR) <= self.banned()

    def test_the_concrete_provider_is_banned_at_lint_time(self) -> None:
        assert PROVIDER_PACKAGE in self.banned()

    def test_the_provider_package_is_exempt(self) -> None:
        """It must be, or it could not import its own vendor libraries -- which is
        also exactly why the reverse rule needs the AST test above."""
        manifest = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        ignores = manifest["tool"]["ruff"]["lint"]["per-file-ignores"]
        assert any("providers/garmindb" in path for path in ignores)
