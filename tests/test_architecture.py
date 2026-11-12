"""Orders never imports a sibling module — the purchasable is opaque."""
import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent
FORBIDDEN = ("stapel_listings", "stapel_billing", "stapel_shop", "stapel_catalog",
             "stapel_classified", "stapel_currencies", "stapel_notifications")


def _imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            yield node.module


def _sources():
    for path in PACKAGE.rglob("*.py"):
        rel = path.relative_to(PACKAGE).parts
        if (rel[0] in ("tests", "build", "dist") or rel[0].startswith(".")
                or ".egg-info" in str(path)):
            continue
        yield path


def test_no_sibling_import_anywhere():
    offenders = [
        (str(path.relative_to(PACKAGE)), name)
        for path in _sources()
        for name in _imports(path)
        if name.split(".")[0] in FORBIDDEN
    ]
    assert offenders == []


def test_the_scan_sees_the_package():
    """Inversion: the walk reaches the modules it claims to check."""
    names = {p.name for p in _sources()}
    assert {"models.py", "registry.py", "views.py", "checkout.py"} <= names


def test_the_rule_fires_on_a_sibling_import(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("from stapel_listings.models import Listing\n")
    assert list(_imports(bad)) == ["stapel_listings.models"]
