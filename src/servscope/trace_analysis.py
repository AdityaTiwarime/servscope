"""Turns the flat list of a trace's spans into what a developer needs to read it:
depth for the waterfall, each span's own time, the critical path and clock-skew warnings.

Kept free of the database so it can be tested with hand-built spans.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SpanNode:
    span_id: str
    parent_span_id: str | None
    start_ns: int
    duration_ns: int
    children: list[SpanNode] = field(default_factory=list)
    depth: int = 0
    self_ns: int = 0
    on_critical_path: bool = False
    starts_before_parent: bool = False

    @property
    def end_ns(self) -> int:
        return self.start_ns + self.duration_ns


@dataclass
class Analysis:
    nodes: dict[str, SpanNode]
    roots: list[SpanNode]
    critical_path: list[str]
    slowest_span_id: str | None  # the span with the most time of its own


def _covered_ns(intervals: list[tuple[int, int]]) -> int:
    """Total length covered by possibly overlapping intervals (children run in parallel)."""
    covered, cur_start, cur_end = 0, None, None
    for start, end in sorted(intervals):
        if cur_end is None or start > cur_end:
            if cur_end is not None and cur_start is not None:
                covered += cur_end - cur_start
            cur_start, cur_end = start, end
        else:
            cur_end = max(cur_end, end)
    if cur_end is not None and cur_start is not None:
        covered += cur_end - cur_start
    return covered


def analyse(spans: list[tuple[str, str | None, int, int]]) -> Analysis:
    """spans: (span_id, parent_span_id, start_ns, duration_ns)."""
    nodes = {sid: SpanNode(sid, parent, start, dur) for sid, parent, start, dur in spans}
    roots: list[SpanNode] = []
    for node in nodes.values():
        parent = nodes.get(node.parent_span_id or "")
        if parent is None:
            roots.append(node)  # true root, or the parent never arrived: shown at the top
        else:
            parent.children.append(node)
    roots.sort(key=lambda n: n.start_ns)

    # Depth and own time, iteratively (a deep trace must not hit Python's recursion limit).
    stack = [(root, 0) for root in roots]
    while stack:
        node, depth = stack.pop()
        node.depth = depth
        node.children.sort(key=lambda n: n.start_ns)
        # A child's time only counts inside the parent's own window.
        clipped = [
            (max(c.start_ns, node.start_ns), min(c.end_ns, node.end_ns)) for c in node.children
        ]
        clipped = [(s, e) for s, e in clipped if e > s]
        node.self_ns = max(0, node.duration_ns - _covered_ns(clipped))
        for child in node.children:
            child.starts_before_parent = child.start_ns < node.start_ns
            stack.append((child, depth + 1))

    # Critical path: what the request was actually waiting on, found by walking backwards.
    # Start at the end of the main root. The child that finished last (up to that moment) is
    # what the span was waiting for; it is on the path. Jump to when that child started and
    # repeat with the remaining children, so calls made one after another (reserve stock,
    # then charge, then release) are all on the path, not just the last one. A child that
    # outlives its parent (still running after the caller gave up) only counts up to the
    # moment the parent stopped waiting.
    path: list[str] = []
    if roots:
        main = max(roots, key=lambda n: n.duration_ns)
        todo: list[tuple[SpanNode, int]] = [(main, main.end_ns)]
        while todo:
            span, until = todo.pop()
            span.on_critical_path = True
            path.append(span.span_id)
            t = min(span.end_ns, until)
            remaining = list(span.children)
            while True:
                waiting_on = [c for c in remaining if c.start_ns < t]
                if not waiting_on:
                    break
                last = max(waiting_on, key=lambda c: min(c.end_ns, t))
                remaining.remove(last)
                todo.append((last, t))
                t = last.start_ns

    slowest = max(nodes.values(), key=lambda n: n.self_ns, default=None)
    return Analysis(nodes, roots, path, slowest.span_id if slowest else None)
