"""ServScope's own counters, served on /metrics in Prometheus text format.

Kept deliberately small: what an operator needs to tell whether ServScope itself is keeping
up (received vs stored vs duplicate vs rejected vs refused, and late data).
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

HELP = {
    "servscope_spans_received_total": "Spans decoded from accepted export requests",
    "servscope_spans_stored_total": "Spans newly stored (not duplicates)",
    "servscope_spans_duplicate_total": "Spans already stored, ignored on a retried delivery",
    "servscope_spans_rejected_total": "Invalid spans skipped inside otherwise valid batches",
    "servscope_spans_late_total": "Spans stored but too late to be counted in metrics",
    "servscope_logs_received_total": "Log records decoded from accepted export requests",
    "servscope_logs_stored_total": "Log records newly stored (not duplicates)",
    "servscope_logs_duplicate_total": "Log records already stored, ignored on a retry",
    "servscope_logs_rejected_total": "Invalid log records skipped",
    "servscope_ingest_refused_total": "Export requests answered 503 (busy or storage down)",
    "servscope_retention_deleted_total": "Rows removed by the retention job",
    "servscope_alert_evaluations_total": "Completed alert evaluation passes",
    "servscope_alert_evaluation_failures_total": "Alert passes that failed (e.g. database down)",
    "servscope_incidents_opened_total": "Incidents opened by the alert evaluator",
    "servscope_incidents_resolved_total": "Incidents resolved",
}


class Stats:
    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self.started = datetime.now(UTC)
        # When each background job last succeeded / failed, so a job that silently stopped
        # is visible (a dead alert evaluator would otherwise just mean "no alerts").
        self.last: dict[str, datetime] = {}

    def mark(self, event: str) -> None:
        self.last[event] = datetime.now(UTC)

    def add(self, name: str, n: int = 1) -> None:
        if name not in HELP:
            raise KeyError(name)  # a typo here would otherwise be a silently missing metric
        self.counts[name] += n

    def render(self, gauges: dict[str, int]) -> str:
        lines: list[str] = []
        for name, help_text in HELP.items():
            lines += [
                f"# HELP {name} {help_text}",
                f"# TYPE {name} counter",
                f"{name} {self.counts[name]}",
            ]
        for name, value in gauges.items():
            lines += [f"# TYPE {name} gauge", f"{name} {value}"]
        return "\n".join(lines) + "\n"
