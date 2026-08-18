"""Progress-event Data and the pure calcs that select and render it.

Events convey lifecycle transitions and heartbeats only — never a fabricated progress percentage or
latency figure, since the engine streams no progress (``stream:false``) and no latency is sourced (E-16).
The retained log lives in ``JobStore``; these functions are pure Calculations over it.
"""

import json
from collections.abc import Iterable
from dataclasses import dataclass, field

#: Event types that end a stream. Mirror the terminal ``JobStatus`` values.
TERMINAL_EVENT_TYPES = frozenset({"succeeded", "failed", "cancelled"})


@dataclass(frozen=True)
class JobEvent:
    """One progress event: a per-job monotonic ``id``, a ``type``, and an opaque ``data`` payload."""

    id: int
    type: str
    data: dict = field(default_factory=dict)


def events_since(log: Iterable[JobEvent], last_id: int | None) -> list[JobEvent]:
    """Return the events after ``last_id`` (all of them when ``last_id`` is ``None``). Pure.

    This is the whole replay mechanism: a late listener passes ``None`` and receives every retained event
    including the terminal one; a reconnecting listener passes its ``Last-Event-ID`` and receives only the
    delta.
    """
    if last_id is None:
        return list(log)
    return [event for event in log if event.id > last_id]


def render_sse(event: JobEvent) -> str:
    """Render one event as an SSE frame (``id:``/``event:``/``data:`` + blank-line terminator). Pure."""
    return f"id: {event.id}\nevent: {event.type}\ndata: {json.dumps(event.data)}\n\n"
