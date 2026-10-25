"""Every text people read must have a Portuguese translation in translation_cache.json.

Keys are collected from the templates (t("...")) and from the Python code: strings passed to t() or
flash(), values of the UPPERCASE label dictionaries, error messages, and AccountError messages.
"""

import ast
import re
from pathlib import Path

from portaria import faces, falls, fire, i18n, objects, pipeline

SRC = Path(__file__).parents[1] / "src" / "portaria"
TEMPLATE_KEY = re.compile(r"""\bt\(\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')""")
PYTHON_FILES = [*sorted((SRC / "web").glob("*.py")), SRC / "users.py", SRC / "importer.py"]


def template_keys() -> set[str]:
    keys = set()
    for path in (SRC / "web" / "templates").glob("*.html"):
        for double, single in TEMPLATE_KEY.findall(path.read_text(encoding="utf-8")):
            keys.add(double or single)
    return keys


def python_keys() -> set[str]:
    keys = set()
    for path in PYTHON_FILES:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            keys |= _keys_in(node)
    return keys


def _keys_in(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Call):
        func = node.func
        name = func.id if isinstance(func, ast.Name) else ""
        if name in ("t", "AccountError") or (isinstance(func, ast.Call) and _name(func.func) == "Translator"):
            return _literal(node.args[:1])
        if name == "flash":
            return _literal(node.args[1:2])
        errors = [k.value for k in node.keywords if k.arg == "error"]  # page(..., error="...")
        if errors:
            return _strings(errors)
    if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
        if any(isinstance(t, ast.Name) and t.id.isupper() for t in node.targets):
            return _strings(node.value.values)
    if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "error" for t in node.targets):
        return _strings([node.value])
    if isinstance(node, ast.FunctionDef) and node.name.endswith("_error"):
        return {s for r in ast.walk(node) if isinstance(r, ast.Return) for s in _strings([r.value])}
    return set()


def _name(node: ast.AST) -> str:
    return node.id if isinstance(node, ast.Name) else ""


def _literal(nodes: list) -> set[str]:
    """Only a string written right there, not strings somewhere inside an expression."""
    return {n.value for n in nodes if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def _strings(nodes: list) -> set[str]:
    found = set()
    for node in nodes:
        for child in ast.walk(node) if node is not None else []:
            if isinstance(child, ast.Constant) and isinstance(child.value, str) and child.value.strip():
                found.add(child.value)
    return found


def dynamic_keys() -> set[str]:
    """Texts chosen at run time: dates, why a face can't be used (shown on the live video), and the names of the
    things cameras record."""
    problems = {faces.NO_FACE, faces.SEVERAL_FACES, faces.TOO_SMALL, faces.TURNED_AWAY, faces.BLURRY, pipeline.FROZEN,
                pipeline.SPOOFED}
    things = {*objects.NAMES.values(), *fire.NAMES.values(), *falls.NAMES.values()}
    return {*i18n.WEEKDAYS, *i18n.MONTHS, "{weekday}, {month} {day}", *problems, *things}


def all_keys() -> set[str]:
    return template_keys() | python_keys() | dynamic_keys()


def test_every_text_has_a_portuguese_translation():
    missing = sorted(all_keys() - set(i18n.catalog("pt")))
    assert not missing, "missing in translation_cache.json:\n" + "\n".join(missing)


def test_translations_keep_their_placeholders():
    placeholder = re.compile(r"\{\w+\}")
    wrong = [
        key for key, value in i18n.catalog("pt").items()
        if sorted(placeholder.findall(key)) != sorted(placeholder.findall(value))
    ]
    assert not wrong, f"placeholders differ: {wrong}"


def test_english_is_the_source_and_portuguese_falls_back_to_it():
    assert i18n.translate("Today", "en") == "Today"
    assert i18n.translate("Today", "pt") == "Hoje"
    assert i18n.translate("Not in the catalog", "pt") == "Not in the catalog"
    assert i18n.Translator("pt")("Seen today: {count}", count=3) == "Vistas hoje: 3"


def test_dates_and_numbers_follow_the_language():
    from datetime import date

    day = date(2026, 10, 23)
    assert i18n.Translator("pt").long_date(day) == "Sexta-feira, 23 de outubro"
    assert i18n.Translator("en").long_date(day) == "Friday, October 23"
    assert i18n.Translator("pt").short_date(day) == "23/10/2026"
    assert i18n.Translator("en").short_date(day) == "Oct 23, 2026"
    assert i18n.Translator("pt").number(0.765) == "0,77"
    assert i18n.Translator("en").number(0.765) == "0.77"
