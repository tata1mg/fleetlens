"""Interface-discovery adapters (the pluggable producer boundary)."""
from .base import Interface, InterfaceAdapter
from .registry import ADAPTERS, build_interfaces, discover_interfaces

__all__ = ["Interface", "InterfaceAdapter", "ADAPTERS", "build_interfaces", "discover_interfaces"]
