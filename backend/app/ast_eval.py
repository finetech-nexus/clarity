"""Subset of the Marble AST used to execute configured rules.

Supported function names: comparisons, AND, OR, NOT, Payload, IsInList, Screen.
Anything else is rejected so an unknown formula cannot silently pass.
"""


class AstError(Exception):
    pass


def eval_ast(node, payload: dict, lists: dict[str, list]):
    if not isinstance(node, dict):
        raise AstError("AST node must be an object")
    if "constant" in node and not node.get("name"):
        return node.get("constant")
    name = node.get("name")
    if not isinstance(name, str) or not name:
        raise AstError("AST node needs a function name or a constant")
    children = [eval_ast(child, payload, lists) for child in node.get("children") or []]
    named = {key: eval_ast(child, payload, lists) for key, child in (node.get("named_children") or {}).items()}
    if name in {">", ">=", "<", "<=", "=", "==", "!="}:
        if len(children) != 2:
            raise AstError(f"{name} expects two arguments")
        left, right = children
        if left is None or right is None:
            return False
        if name == ">":
            return left > right
        if name == ">=":
            return left >= right
        if name == "<":
            return left < right
        if name == "<=":
            return left <= right
        if name in {"=", "=="}:
            return left == right
        return left != right
    if name in {"AND", "And"}:
        return all(bool(item) for item in children)
    if name in {"OR", "Or"}:
        return any(bool(item) for item in children)
    if name == "NOT":
        if len(children) != 1:
            raise AstError("NOT expects one argument")
        return not bool(children[0])
    if name == "Payload":
        path = named.get("path")
        if path is None and children:
            path = children[0]
        if not isinstance(path, str):
            raise AstError("Payload path must be a string")
        return payload.get(path)
    if name == "IsInList":
        if not children:
            raise AstError("IsInList expects a value")
        list_name = named.get("list")
        if list_name is None and len(children) > 1:
            list_name = children[1]
        values = lists.get(str(list_name), [])
        return children[0] in values
    if name == "IsNotEmpty":
        if len(children) != 1:
            raise AstError("IsNotEmpty expects one argument")
        return children[0] is not None and children[0] != ""
    if name == "Screen":
        return True
    raise AstError(f"unsupported AST function {name}")


def node_evaluation(value, errors: list | None = None) -> dict:
    return {
        "return_value": {"value": value, "is_omitted": value is None},
        "errors": errors or [],
        "children": [],
        "named_children": {},
        "skipped": False,
    }
