"""Lightweight retrieval memory.

Stdlib-only stand-in for the planned pgvector + Sentence-Transformers stack.
Offers:

* Sentence-aware chunker with character budget + overlap.
* Hashed token-frequency embedder (deterministic, no external model).
* BM25 sparse scorer.
* Hybrid ranking: BM25 score + cosine over hashed TF, weighted.

Designed so a swap to real embeddings later only needs to replace
``HashedTFEmbedder.embed`` and the storage backend.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Optional


_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text)]


# ---------------------------------------------------------------------------
# Chunker
# ---------------------------------------------------------------------------

def chunk_text(
    text: str,
    *,
    max_chars: int = 512,
    overlap: int = 64,
) -> list[str]:
    """Split ``text`` into sentence-aware chunks not exceeding ``max_chars``.

    ``overlap`` characters from the tail of the previous chunk are prepended
    to the next chunk to preserve cross-boundary context.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if overlap < 0 or overlap >= max_chars:
        raise ValueError("overlap must be in [0, max_chars)")
    text = text.strip()
    if not text:
        return []
    sentences = _SENT_SPLIT_RE.split(text)
    chunks: list[str] = []
    buf = ""
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        if len(buf) + len(s) + 1 <= max_chars:
            buf = f"{buf} {s}".strip()
        else:
            if buf:
                chunks.append(buf)
                tail = buf[-overlap:] if overlap else ""
                buf = (tail + " " + s).strip() if tail else s
            else:
                # Single sentence longer than budget -> hard split.
                for i in range(0, len(s), max_chars - overlap):
                    piece = s[i:i + max_chars]
                    chunks.append(piece)
                buf = ""
    if buf:
        chunks.append(buf)
    return chunks


# ---------------------------------------------------------------------------
# Hashed TF embedder
# ---------------------------------------------------------------------------

class HashedTFEmbedder:
    """Maps tokens to a fixed-dim vector via Python's builtin hash."""

    def __init__(self, dim: int = 256):
        if dim <= 0:
            raise ValueError("dim must be positive")
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for tok in tokenize(text):
            # Stable hash via Python's builtin (within a run); fine for our
            # in-process retrieval. Replace with sha1 if cross-run stability
            # is needed.
            h = (hash(tok) & 0x7FFFFFFF) % self.dim
            v[h] += 1.0
        # L2-normalize
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise ValueError("dim mismatch")
    return sum(x * y for x, y in zip(a, b))


# ---------------------------------------------------------------------------
# BM25
# ---------------------------------------------------------------------------

class BM25Index:
    """Standard BM25 implementation over an in-memory corpus."""

    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.docs = docs
        self.N = len(docs)
        self.avgdl = (sum(len(d) for d in docs) / self.N) if self.N else 0.0
        self.df: dict[str, int] = {}
        for d in docs:
            for term in set(d):
                self.df[term] = self.df.get(term, 0) + 1
        self.idf: dict[str, float] = {
            term: math.log(1 + (self.N - df + 0.5) / (df + 0.5))
            for term, df in self.df.items()
        }

    def score(self, query: list[str], doc_idx: int) -> float:
        d = self.docs[doc_idx]
        if not d:
            return 0.0
        tf = Counter(d)
        score = 0.0
        dl = len(d)
        for term in query:
            if term not in self.idf:
                continue
            f = tf.get(term, 0)
            if f == 0:
                continue
            denom = f + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1.0))
            score += self.idf[term] * (f * (self.k1 + 1)) / denom
        return score


# ---------------------------------------------------------------------------
# Memory store
# ---------------------------------------------------------------------------

@dataclass
class MemoryItem:
    id: str
    text: str
    tokens: list[str] = field(default_factory=list)
    embedding: list[float] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass
class RetrievalHit:
    item: MemoryItem
    bm25_score: float
    vector_score: float
    score: float


class MemoryStore:
    """Hybrid retrieval store: BM25 + cosine over hashed TF, weighted."""

    def __init__(self, *, dim: int = 256, alpha: float = 0.5):
        if not 0.0 <= alpha <= 1.0:
            raise ValueError("alpha must be in [0,1]")
        self.dim = dim
        self.alpha = alpha
        self.embedder = HashedTFEmbedder(dim=dim)
        self.items: list[MemoryItem] = []
        self._dirty = True
        self._bm25: Optional[BM25Index] = None

    # ---- write --------------------------------------------------------
    def add(self, text: str, *, item_id: Optional[str] = None, metadata: Optional[dict] = None) -> MemoryItem:
        item_id = item_id or f"m{len(self.items)}"
        toks = tokenize(text)
        emb = self.embedder.embed(text)
        item = MemoryItem(id=item_id, text=text, tokens=toks, embedding=emb, metadata=metadata or {})
        self.items.append(item)
        self._dirty = True
        return item

    def add_document(self, text: str, *, metadata: Optional[dict] = None, max_chars: int = 512, overlap: int = 64) -> list[MemoryItem]:
        out = []
        for i, chunk in enumerate(chunk_text(text, max_chars=max_chars, overlap=overlap)):
            md = dict(metadata or {})
            md.setdefault("chunk_index", str(i))
            out.append(self.add(chunk, metadata=md))
        return out

    # ---- read --------------------------------------------------------
    def _ensure_bm25(self) -> BM25Index:
        if self._dirty or self._bm25 is None:
            self._bm25 = BM25Index([it.tokens for it in self.items])
            self._dirty = False
        return self._bm25

    def search(self, query: str, *, k: int = 5) -> list[RetrievalHit]:
        if not self.items:
            return []
        bm25 = self._ensure_bm25()
        q_tokens = tokenize(query)
        q_emb = self.embedder.embed(query)
        scored: list[RetrievalHit] = []
        # Normalize BM25 to roughly [0,1] for blending.
        raw = [bm25.score(q_tokens, i) for i in range(len(self.items))]
        max_bm25 = max(raw) or 1.0
        for i, item in enumerate(self.items):
            b = raw[i] / max_bm25
            v = cosine(q_emb, item.embedding)
            blended = self.alpha * b + (1 - self.alpha) * v
            scored.append(RetrievalHit(item=item, bm25_score=raw[i], vector_score=v, score=blended))
        scored.sort(key=lambda h: h.score, reverse=True)
        return scored[:k]
