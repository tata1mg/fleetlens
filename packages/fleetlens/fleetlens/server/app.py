"""Composition root for the MCP server: db path -> store -> services."""
from __future__ import annotations

from dataclasses import dataclass

from ..service.callgraph import CallGraphService
from ..service.discovery import DiscoveryService
from ..service.relationships import RelationshipService
from ..store.sqlite import SqliteStore


@dataclass
class ServiceContext:
    store: SqliteStore
    callgraph: CallGraphService
    relationships: RelationshipService
    discovery: DiscoveryService

    def close(self) -> None:
        self.store.close()


def build_context(db_path: str, embedder=None, read_only: bool = False) -> ServiceContext:
    """Wire db -> store -> services. `embedder` (optional) enables semantic discovery; when
    None, the discover_* tools report themselves unavailable. `read_only` opens the index
    through a read-only connection, which is what a shared deployment wants."""
    store = SqliteStore(db_path, read_only=read_only)
    return ServiceContext(
        store=store,
        callgraph=CallGraphService(store, store),
        relationships=RelationshipService(store, store),
        discovery=DiscoveryService(store, store, embedder),
    )
