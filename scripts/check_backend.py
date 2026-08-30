#!/usr/bin/env python3
"""
SmAttaker — Static Sanity Checker
==================================
Runs the exact checks that caught every deploy-breaking bug during
manual debugging sessions (NameError from missing imports, syntax
errors, undefined names) — now automated so they're caught in CI
*before* a push ever reaches Render, instead of discovered live in
production logs.

Usage:
    python3 scripts/check_backend.py

Exit code 0 = clean. Exit code 1 = at least one issue found (CI fails).

This is intentionally a lightweight, dependency-free static checker
(stdlib `ast` only) rather than requiring pyflakes/mypy/etc. to be
installed — it needs to run the same way every time, in CI and
locally, with zero setup friction.
"""
import ast
import glob
import builtins
import sys

BUILTINS = set(dir(builtins)) | {"self", "cls", "__name__", "__file__", "True", "False", "None"}


def collect_assigned_names(node: ast.AST) -> set[str]:
    """Collect every name bound anywhere within a node (imports, assignments,
    function/class defs, exception handlers, for-loop targets, etc.)."""
    names = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            names.add(n.id)
        elif isinstance(n, ast.arg):
            names.add(n.arg)
        elif isinstance(n, ast.Import):
            for alias in n.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            for alias in n.names:
                names.add(alias.asname or alias.name)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            names.add(n.name)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            if hasattr(n, "name"):
                names.add(n.name)
        elif isinstance(n, ast.Global):
            names.update(n.names)
    return names


def check_file(fpath: str) -> list[str]:
    """Returns a list of human-readable problem descriptions for one file."""
    problems = []
    try:
        src = open(fpath, encoding="utf-8").read()
    except Exception as e:
        return [f"{fpath}: could not read file ({e})"]

    try:
        tree = ast.parse(src, filename=fpath)
    except SyntaxError as e:
        return [f"{fpath}: SYNTAX ERROR — {e}"]

    module_names = collect_assigned_names(tree)

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            local_names = collect_assigned_names(node)
            scope = module_names | local_names | BUILTINS
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                    if sub.id not in scope:
                        problems.append(
                            f"{fpath}:{sub.lineno}: possibly undefined name '{sub.id}' "
                            f"in function '{node.name}' (likely a missing import)"
                        )
    return problems


def main() -> int:
    files = sorted(set(
        glob.glob("backend/**/*.py", recursive=True)
        + glob.glob("alembic/**/*.py", recursive=True)
        + glob.glob("scripts/**/*.py", recursive=True)
    ))
    if not files:
        print("No Python files found — check you're running this from the repo root.")
        return 1

    all_problems = []
    for fpath in files:
        all_problems.extend(check_file(fpath))

    if all_problems:
        print(f"❌ {len(all_problems)} issue(s) found across {len(files)} files:\n")
        for p in all_problems:
            print(f"  {p}")
        print(
            "\nThese are the exact class of bugs that took SmAttaker down in "
            "production multiple times (e.g. a route using `get_current_user_dep` "
            "without importing it). Fix them before merging."
        )
        return 1

    print(f"✅ All {len(files)} backend files clean — no syntax errors, no undefined names.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
