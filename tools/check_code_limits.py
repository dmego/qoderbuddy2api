#!/usr/bin/env python3
"""Enforce the repository's production source-size limits."""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = ROOT / "src" / "qb2api"
FRONTEND_ROOT = ROOT / "frontend" / "src"
TEST_ROOT = ROOT / "tests"
MAX_FILE_LINES = 300
MAX_FUNCTION_LINES = 50
MAX_COMPLEXITY = 10
MAX_NESTING = 3
MAX_POSITIONAL_ARGS = 3


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    message: str

    def render(self) -> str:
        return f"{self.path.relative_to(ROOT)}:{self.line}: {self.message}"


def source_files() -> list[Path]:
    python_files = list(PYTHON_ROOT.rglob("*.py"))
    frontend_files = [
        path
        for path in FRONTEND_ROOT.rglob("*")
        if path.suffix in {".vue", ".ts", ".css", ".js"}
    ]
    legacy_web = [
        path
        for path in (PYTHON_ROOT / "web").glob("admin.*")
        if path.is_file()
    ]
    return sorted(python_files + frontend_files + legacy_web)


def test_files() -> list[Path]:
    return sorted(TEST_ROOT.rglob("*.py"))


def file_violations(path: Path, source: str) -> list[Violation]:
    lines = len(source.splitlines())
    if lines <= MAX_FILE_LINES:
        return []
    return [Violation(path, 1, f"file has {lines} lines (max {MAX_FILE_LINES})")]


def python_violations(path: Path, source: str) -> list[Violation]:
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as error:
        return [Violation(path, error.lineno or 1, f"syntax error: {error.msg}")]
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        violations.extend(function_violations(path, node))
    return violations


def function_violations(
    path: Path,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[Violation]:
    lines = (node.end_lineno or node.lineno) - node.lineno + 1
    complexity = cyclomatic_complexity(node)
    nesting = max_nesting(node)
    positional = positional_arg_count(node)
    checks = (
        (lines > MAX_FUNCTION_LINES, f"{node.name} has {lines} lines (max {MAX_FUNCTION_LINES})"),
        (complexity > MAX_COMPLEXITY, f"{node.name} complexity is {complexity} (max {MAX_COMPLEXITY})"),
        (nesting > MAX_NESTING, f"{node.name} nesting is {nesting} (max {MAX_NESTING})"),
        (positional > MAX_POSITIONAL_ARGS, f"{node.name} has {positional} positional args (max {MAX_POSITIONAL_ARGS})"),
    )
    return [Violation(path, node.lineno, message) for failed, message in checks if failed]


def positional_arg_count(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    names = [argument.arg for argument in (*node.args.posonlyargs, *node.args.args)]
    if names and names[0] in {"self", "cls"}:
        names = names[1:]
    return len(names)


def cyclomatic_complexity(node: ast.AST) -> int:
    complexity = 1
    for child in ast.walk(node):
        if isinstance(child, ast.BoolOp):
            complexity += max(1, len(child.values) - 1)
        elif isinstance(child, ast.Try):
            complexity += len(child.handlers) + bool(child.orelse) + bool(child.finalbody)
        elif isinstance(child, ast.Match):
            complexity += len(child.cases)
        elif isinstance(child, ast.comprehension):
            complexity += 1 + len(child.ifs)
        elif isinstance(child, ast.If | ast.For | ast.AsyncFor | ast.While | ast.IfExp):
            complexity += 1
    return complexity


def max_nesting(node: ast.AST) -> int:
    nested_types = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.Match)

    def visit(current: ast.AST, depth: int) -> int:
        next_depth = depth + 1 if isinstance(current, nested_types) else depth
        children = [
            child
            for child in ast.iter_child_nodes(current)
            if not isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda)
        ]
        return max([next_depth, *(visit(child, next_depth) for child in children)])

    return max((visit(statement, 0) for statement in getattr(node, "body", [])), default=0)


def main() -> int:
    violations: list[Violation] = []
    for path in source_files():
        source = path.read_text(encoding="utf-8")
        violations.extend(file_violations(path, source))
        if path.suffix == ".py":
            violations.extend(python_violations(path, source))
    for path in test_files():
        source = path.read_text(encoding="utf-8")
        violations.extend(file_violations(path, source))
    for violation in sorted(violations, key=lambda item: (str(item.path), item.line, item.message)):
        print(violation.render())
    if violations:
        print(f"code limits failed: {len(violations)} violation(s)")
        return 1
    print("code limits passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
