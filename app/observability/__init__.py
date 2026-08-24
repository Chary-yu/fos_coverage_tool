"""Low-cost, release-bound runtime performance evidence."""

from app.observability.performance import (
    PerformanceEvidenceCollector, bind_collector, current_collector,
    instrument_connection,
)

__all__ = [
    "PerformanceEvidenceCollector", "bind_collector", "current_collector",
    "instrument_connection",
]
