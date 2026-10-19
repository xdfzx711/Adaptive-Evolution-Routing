"""Bounded arithmetic for a local calculator, without Python eval."""

import ast
from math import isfinite
from time import monotonic


class CalculatorTimeout(TimeoutError):
    pass


def calculate(expression: str, *, deadline: float | None = None) -> int | float:
    def check_time():
        if deadline is not None and monotonic() >= deadline:
            raise CalculatorTimeout("Calculator deadline reached")

    check_time()
    if not isinstance(expression, str) or not expression.strip() or len(expression) > 1024:
        raise ValueError("expression must be a nonempty string of at most 1024 characters")
    tree = ast.parse(expression, mode="eval")
    if sum(1 for _ in ast.walk(tree)) > 128:
        raise ValueError("expression contains too many operations")

    def visit(node, depth=0):
        check_time()
        if depth > 32:
            raise ValueError("expression is too deeply nested")
        if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
            value = node.value
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            operand = visit(node.operand, depth + 1)
            value = operand if isinstance(node.op, ast.UAdd) else -operand
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            left, right = visit(node.left, depth + 1), visit(node.right, depth + 1)
            if isinstance(node.op, ast.Add):
                value = left + right
            elif isinstance(node.op, ast.Sub):
                value = left - right
            elif isinstance(node.op, ast.Mult):
                value = left * right
            else:
                value = left / right
        else:
            raise ValueError("only numeric constants, parentheses, +, -, * and / are supported")
        if type(value) is int and value.bit_length() > 4096:
            raise ValueError("integer result is too large")
        if type(value) is float and not isfinite(value):
            raise ValueError("result must be finite")
        return value

    value = visit(tree.body)
    check_time()
    return int(value) if type(value) is float and value.is_integer() else value
