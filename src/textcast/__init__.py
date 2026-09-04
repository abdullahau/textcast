"""textcast — a private, self-hosted audio reader for newsletters and articles."""

from __future__ import annotations

__version__ = "0.7.1"

from .document import Article, Block, BlockKind, Section

__all__ = ["Article", "Block", "BlockKind", "Section", "__version__"]
