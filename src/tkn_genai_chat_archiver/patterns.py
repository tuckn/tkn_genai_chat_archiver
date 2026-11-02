"""Glob patterns over source-relative paths: `*` within a segment, `**` across segments."""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Sequence
from dataclasses import dataclass

_DOUBLE = "**"


class PatternError(ValueError):
    pass


@dataclass(frozen=True)
class _Pattern:
    text: str
    segments: tuple[re.Pattern[str] | None, ...]  # None means `**`


def _compile(text: str, *, case_insensitive: bool) -> _Pattern:
    normalized = text.strip().replace("\\", "/")
    if not normalized or normalized.startswith("/") or ":" in normalized:
        raise PatternError(f"pattern must be a relative path: {text!r}")
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise PatternError(f"pattern must not contain empty, '.' or '..' segments: {text!r}")
    flags = re.IGNORECASE if case_insensitive else 0
    segments: list[re.Pattern[str] | None] = []
    for part in parts:
        if part == _DOUBLE:
            if segments and segments[-1] is None:
                continue
            segments.append(None)
        elif _DOUBLE in part:
            raise PatternError(f"'**' must be a whole path segment: {text!r}")
        else:
            segments.append(re.compile(fnmatch.translate(part), flags))
    return _Pattern(normalized, tuple(segments))


def _full(pattern: tuple[re.Pattern[str] | None, ...], parts: Sequence[str], pi: int = 0, si: int = 0) -> bool:
    while True:
        if pi == len(pattern):
            return si == len(parts)
        segment = pattern[pi]
        if segment is None:
            return any(_full(pattern, parts, pi + 1, index) for index in range(si, len(parts) + 1))
        if si == len(parts) or not segment.match(parts[si]):
            return False
        pi += 1
        si += 1


def _prefix(pattern: tuple[re.Pattern[str] | None, ...], parts: Sequence[str], pi: int = 0, si: int = 0) -> bool:
    """Whether some path below the directory `parts` could match."""

    while True:
        if si == len(parts):
            return pi < len(pattern)
        if pi == len(pattern):
            return False
        segment = pattern[pi]
        if segment is None:
            # `**` can absorb the rest of the directory and any depth below it.
            return True
        if not segment.match(parts[si]):
            return False
        pi += 1
        si += 1


class PatternSet:
    def __init__(self, include: Sequence[str], exclude: Sequence[str], *, case_insensitive: bool) -> None:
        if not include:
            raise PatternError("at least one include pattern is required")
        self.include_texts = tuple(include)
        self.exclude_texts = tuple(exclude)
        self._include = [_compile(text, case_insensitive=case_insensitive) for text in include]
        self._exclude = [_compile(text, case_insensitive=case_insensitive) for text in exclude]

    def excluded(self, relative: str) -> bool:
        parts = relative.split("/")
        return any(_full(item.segments, parts) for item in self._exclude)

    def included(self, relative: str) -> bool:
        parts = relative.split("/")
        return any(_full(item.segments, parts) for item in self._include)

    def selects(self, relative: str) -> bool:
        return self.included(relative) and not self.excluded(relative)

    def may_contain(self, directory: str) -> bool:
        """Whether walking into `directory` can find selected files."""

        parts = directory.split("/") if directory else []
        if not any(_prefix(item.segments, parts) for item in self._include):
            return False
        for item in self._exclude:
            # `a/**` excludes everything below a matching directory.
            if item.segments and item.segments[-1] is None and _full(item.segments[:-1], parts):
                return False
        return True
