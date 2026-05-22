#!/usr/bin/env python3
"""Guardrail: PocketBase-Filter-f-Strings müssen `pb_quote(...)` nutzen.

Scannt `backend/**/*.py` per AST und flagged Stellen, an denen ein Filter-
String per f-String-Interpolation gebaut wird, ohne dass der Wert durch
`pb_quote(...)` läuft.

Erkennt:
- `params={"filter": f'email="{x}"'}` — Dict-Literal mit key "filter"
- `params["filter"] = f"..."` — Subscript-Zuweisung

Implizite Whitelist (kein Treffer):
- Konstante Filter ohne Platzhalter: `params={"filter": "is_new=true"}`
- f-Strings nur mit Konstanten: `f"is_done!=true"`
- Filter aus bereits gequoteten Bestandteilen: `" && ".join(parts)`
- Variablen, die woanders sicher gebaut wurden: `params={"filter": history_filter}`
- Jede `{...}`-Stelle ist ein direkter `pb_quote(...)`/`pb_client.pb_quote(...)`-Call

Explizite Whitelist:
- Inline-Kommentar `# pb-filter-safe` in oder direkt nach der Value-Zeile.

Exit-Code 0 = keine Treffer; 1 = mindestens ein verdächtiger Filter.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

SAFE_MARKER = "# pb-filter-safe"


def _is_pb_quote_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name) and func.id == "pb_quote":
        return True
    if isinstance(func, ast.Attribute) and func.attr == "pb_quote":
        return True
    return False


def _is_safe_filter_value(value: ast.AST) -> bool:
    """Filter-Value gilt als safe, wenn er kein f-String mit Platzhaltern ist
    ODER jeder Platzhalter direkt ein `pb_quote(...)`-Call ist."""
    if not isinstance(value, ast.JoinedStr):
        return True
    for part in value.values:
        if isinstance(part, ast.FormattedValue):
            if not _is_pb_quote_call(part.value):
                return False
    return True


def _has_safe_marker(value: ast.AST, source_lines: list[str]) -> bool:
    start = getattr(value, "lineno", 0)
    end = getattr(value, "end_lineno", start) or start
    # Erlaubt Marker eine Zeile vor dem Value (Code-Review-Kommentar im Dict-Block),
    # innerhalb der Value-Range oder direkt darunter.
    for ln in range(start - 1, end + 2):
        if 0 < ln <= len(source_lines) and SAFE_MARKER in source_lines[ln - 1]:
            return True
    return False


def _record(value: ast.AST, source_lines: list[str], file: Path, suspects: list) -> None:
    if _is_safe_filter_value(value):
        return
    if _has_safe_marker(value, source_lines):
        return
    line = getattr(value, "lineno", 0)
    snippet = source_lines[line - 1].strip() if 0 < line <= len(source_lines) else ""
    suspects.append((file, line, snippet))


def _check_dict(node: ast.Dict, source_lines: list[str], file: Path, suspects: list) -> None:
    for key, val in zip(node.keys, node.values):
        if isinstance(key, ast.Constant) and key.value == "filter":
            _record(val, source_lines, file, suspects)


def _check_subscript_assign(target: ast.AST, value: ast.AST,
                            source_lines: list[str], file: Path, suspects: list) -> None:
    if not isinstance(target, ast.Subscript):
        return
    slice_ = target.slice
    if isinstance(slice_, ast.Constant) and slice_.value == "filter":
        _record(value, source_lines, file, suspects)


def check_file(file: Path) -> list:
    source = file.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        print(f"WARN: {file}: parse error {e}", file=sys.stderr)
        return []
    source_lines = source.splitlines()
    suspects: list = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            _check_dict(node, source_lines, file, suspects)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                _check_subscript_assign(tgt, node.value, source_lines, file, suspects)
        elif isinstance(node, ast.AugAssign):
            _check_subscript_assign(node.target, node.value, source_lines, file, suspects)
    return suspects


def main() -> int:
    root = Path(__file__).resolve().parent.parent / "backend"
    if not root.is_dir():
        print(f"ERROR: {root} not found", file=sys.stderr)
        return 2
    all_suspects: list = []
    files_scanned = 0
    for py in sorted(root.rglob("*.py")):
        if "__pycache__" in py.parts:
            continue
        files_scanned += 1
        all_suspects.extend(check_file(py))
    if all_suspects:
        print("PocketBase-Filter-Guardrail: verdächtige f-String-Filter ohne pb_quote()")
        print()
        for file, line, snippet in all_suspects:
            rel = file.relative_to(root.parent)
            print(f"  {rel}:{line}")
            print(f"    {snippet}")
        print()
        print(f"{len(all_suspects)} Treffer in {files_scanned} Dateien gescannt.")
        print("Fix: jeden interpolierten Wert durch pb_client.pb_quote(...) ersetzen,")
        print(f"     oder die Zeile mit `{SAFE_MARKER}` markieren (nur bei nachweislich")
        print("     sicheren Fällen wie statischen Konstanten oder vorgequoteten Variablen).")
        return 1
    print(f"PocketBase-Filter-Guardrail: keine verdächtigen Filter in {files_scanned} Dateien.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
