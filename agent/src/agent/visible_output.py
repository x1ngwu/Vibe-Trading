"""Keep trusted session-recovery context out of user-visible assistant text."""

from __future__ import annotations

import re


_INTERNAL_CONTEXT_TAGS = (
    "persisted-strategy-version",
    "persisted-similarity-results",
)
_OPEN_MARKERS = tuple(f"<{tag}" for tag in _INTERNAL_CONTEXT_TAGS)
_CLOSE_MARKERS = tuple(f"</{tag}" for tag in _INTERNAL_CONTEXT_TAGS)
_BLOCK_RE = re.compile(
    r"<(?P<tag>persisted-strategy-version|persisted-similarity-results)"
    r"(?:\s[^>]*)?>.*?</(?P=tag)\s*>",
    flags=re.IGNORECASE | re.DOTALL,
)
_OPEN_PREFIX_RE = re.compile(
    r"<(?:persisted-strategy-version|persisted-similarity-results)",
    flags=re.IGNORECASE,
)
_CLOSE_RE = re.compile(
    r"</(?:persisted-strategy-version|persisted-similarity-results)\s*>",
    flags=re.IGNORECASE,
)
_PARTIAL_INTERNAL_TAG_RE = re.compile(
    r"</?persisted-[^\n>]*$",
    flags=re.IGNORECASE,
)

SAFE_FILTERED_RESPONSE = "操作已完成，请以页面中的结构化卡片状态为准。"


def strip_internal_context_blocks(content: str) -> str:
    """Remove server-only recovery blocks from one complete assistant response."""

    if not content:
        return ""
    sanitized = _BLOCK_RE.sub("", content)
    # Fail closed when a model emits an unterminated opening block.
    opening = _OPEN_PREFIX_RE.search(sanitized)
    if opening is not None:
        sanitized = sanitized[: opening.start()]
    sanitized = _CLOSE_RE.sub("", sanitized)
    sanitized = _PARTIAL_INTERNAL_TAG_RE.sub("", sanitized)
    return sanitized.strip()


class VisibleAssistantStreamFilter:
    """Incrementally suppress internal context even when tags split across chunks."""

    def __init__(self) -> None:
        self._pending = ""
        self._blocked_tag: str | None = None

    @staticmethod
    def _partial_marker_suffix_length(value: str) -> int:
        lowered = value.lower()
        markers = _OPEN_MARKERS + _CLOSE_MARKERS
        max_length = min(len(lowered), max(len(marker) for marker in markers) - 1)
        for length in range(max_length, 0, -1):
            suffix = lowered[-length:]
            if any(marker.startswith(suffix) for marker in markers):
                return length
        return 0

    @staticmethod
    def _next_marker(value: str) -> tuple[int, str, bool] | None:
        lowered = value.lower()
        matches: list[tuple[int, str, bool]] = []
        for tag, marker in zip(_INTERNAL_CONTEXT_TAGS, _OPEN_MARKERS, strict=True):
            index = lowered.find(marker)
            if index >= 0:
                matches.append((index, tag, True))
        for tag, marker in zip(_INTERNAL_CONTEXT_TAGS, _CLOSE_MARKERS, strict=True):
            index = lowered.find(marker)
            if index >= 0:
                matches.append((index, tag, False))
        return min(matches, key=lambda item: item[0]) if matches else None

    def feed(self, delta: str) -> str:
        """Return the safe portion of one provider text delta."""

        if not delta:
            return ""
        self._pending += delta
        visible: list[str] = []

        while self._pending:
            if self._blocked_tag is not None:
                close_marker = f"</{self._blocked_tag}"
                close_index = self._pending.lower().find(close_marker)
                if close_index < 0:
                    return "".join(visible)
                close_end = self._pending.find(">", close_index)
                if close_end < 0:
                    return "".join(visible)
                self._pending = self._pending[close_end + 1 :]
                self._blocked_tag = None
                continue

            match = self._next_marker(self._pending)
            if match is not None:
                marker_index, tag, is_open = match
                marker_end = self._pending.find(">", marker_index)
                if marker_end < 0:
                    visible.append(self._pending[:marker_index])
                    self._pending = self._pending[marker_index:]
                    return "".join(visible)
                visible.append(self._pending[:marker_index])
                self._pending = self._pending[marker_end + 1 :]
                if is_open:
                    self._blocked_tag = tag
                continue

            suffix_length = self._partial_marker_suffix_length(self._pending)
            if suffix_length:
                visible.append(self._pending[:-suffix_length])
                self._pending = self._pending[-suffix_length:]
            else:
                visible.append(self._pending)
                self._pending = ""
            break

        return "".join(visible)

    def finish(self) -> str:
        """Flush safe trailing text and discard an unterminated internal block."""

        if self._blocked_tag is not None:
            self._pending = ""
            self._blocked_tag = None
            return ""
        trailing = strip_internal_context_blocks(self._pending)
        self._pending = ""
        return trailing
