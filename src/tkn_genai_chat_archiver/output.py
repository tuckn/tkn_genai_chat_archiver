"""Final results on stdout: `key=value` lines by default, one JSON document with --json."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any


def _escape(text: str) -> str:
    out = []
    for char in text:
        if char == "\n":
            out.append("\\n")
        elif char == "\r":
            out.append("\\r")
        elif char == "\t":
            out.append("\\t")
        elif ord(char) < 32 or char in "  \x7f":
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    return "".join(out)


def _scalar(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    return _escape(str(value))


def flatten(value: Any, prefix: str = "") -> list[str]:
    if isinstance(value, dict):
        if not value:
            return [f"{prefix}={{}}"] if prefix else []
        lines: list[str] = []
        for key, item in value.items():
            lines.extend(flatten(item, f"{prefix}.{key}" if prefix else str(key)))
        return lines
    if isinstance(value, list):
        if not value:
            return [f"{prefix}=[]"]
        lines = []
        for index, item in enumerate(value):
            lines.extend(flatten(item, f"{prefix}[{index}]"))
        return lines
    return [f"{prefix}={_scalar(value)}"]


def summarize(result: dict[str, Any], *, full_output: bool) -> dict[str, Any]:
    """Collapse routine per-file items unless --full-output is given."""

    if full_output:
        return result
    projected = dict(result)
    targets = []
    for target in result.get("targets", []):
        item = dict(target)
        items = item.pop("items", None)
        if items:
            item["itemsCount"] = len(items)
            item["itemsCounts"] = dict(Counter(str(entry.get("action")) for entry in items))
            item["omittedDetailsCount"] = len(items)
            item["detailsHint"] = "add --full-output, or read reportPath"
        problems = item.get("problems")
        if isinstance(problems, list) and len(problems) > 50:
            item["problems"] = problems[:50]
            item["omittedProblemsCount"] = len(problems) - 50
        targets.append(item)
    if "targets" in result:
        projected["targets"] = targets
    return projected


def render(result: dict[str, Any], *, as_json: bool, full_output: bool) -> str:
    projected = summarize(result, full_output=full_output)
    if as_json:
        return json.dumps(projected, ensure_ascii=False, indent=2)
    return "\n".join(flatten(projected))
