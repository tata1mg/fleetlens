"""Human-authored engineering guidance, kept separate from everything derived.

Every other object fleetlens stores is re-extracted on each index and therefore cannot go
stale. These cannot. A rule like "inter-service calls go through the shared HTTP client" is
not written anywhere a parser can reach, and an agent that infers conventions by reading the
fleet infers the *existing* convention rather than the intended one. Where a team is
migrating, the majority pattern is precisely the one they are moving away from.

So guidance is stored with `source="authored"` and every response that carries it says so,
along with when a human last reviewed it. A claim is never handed over looking like evidence.
"""
from .embed import embed_guidance
from .link import governing, link, violations
from .load import (
    GuidanceError,
    guidance_dirs,
    ingest,
    ingest_roots,
    load_guidance,
)

__all__ = ["GuidanceError", "load_guidance", "ingest", "link", "governing",
           "violations", "embed_guidance", "ingest_roots", "guidance_dirs"]
