"""Heuristic Protocol — every discovery heuristic conforms to this shape."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..context import DiscoveryContext
    from ..findings import Finding


@runtime_checkable
class Heuristic(Protocol):
    name: str
    data_sources: tuple[str, ...]  # DiscoveryContext attribute names this heuristic reads

    def run(self, ctx: "DiscoveryContext") -> list["Finding"]: ...
