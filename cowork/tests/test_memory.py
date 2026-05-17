"""Tests for retrieval memory."""
from __future__ import annotations

import pytest

from cowork.memory import BM25Index, HashedTFEmbedder, MemoryStore, chunk_text, tokenize


# ---------------------------------------------------------------------------
# Tokenizer / chunker
# ---------------------------------------------------------------------------

def test_tokenize_lowercases_and_splits():
    assert tokenize("Hello, WORLD! foo_bar 42") == ["hello", "world", "foo_bar", "42"]


def test_chunk_text_respects_max_chars():
    text = ("Alpha is the first letter. " * 20).strip()
    chunks = chunk_text(text, max_chars=120, overlap=20)
    assert all(len(c) <= 120 + 1 for c in chunks)
    assert len(chunks) >= 2


def test_chunk_text_empty_returns_empty():
    assert chunk_text("   ") == []


def test_chunk_text_invalid_params():
    with pytest.raises(ValueError):
        chunk_text("x", max_chars=0)
    with pytest.raises(ValueError):
        chunk_text("x", max_chars=10, overlap=10)


def test_chunk_text_hard_split_for_long_sentence():
    s = "a" * 1000
    chunks = chunk_text(s, max_chars=200, overlap=20)
    assert len(chunks) >= 5


# ---------------------------------------------------------------------------
# Embedder
# ---------------------------------------------------------------------------

def test_embedder_is_unit_length():
    e = HashedTFEmbedder(dim=64)
    v = e.embed("hello world")
    norm = sum(x * x for x in v) ** 0.5
    assert 0.99 <= norm <= 1.01


def test_embedder_dim_must_be_positive():
    with pytest.raises(ValueError):
        HashedTFEmbedder(dim=0)


# ---------------------------------------------------------------------------
# BM25
# ---------------------------------------------------------------------------

def test_bm25_ranks_known_doc_higher():
    docs = [
        tokenize("Cats are small carnivorous mammals"),
        tokenize("The Python programming language is dynamic"),
        tokenize("Quantum mechanics describes the very small"),
    ]
    idx = BM25Index(docs)
    q = tokenize("python language")
    scores = [idx.score(q, i) for i in range(len(docs))]
    assert scores[1] > scores[0]
    assert scores[1] > scores[2]


# ---------------------------------------------------------------------------
# MemoryStore
# ---------------------------------------------------------------------------

def test_memorystore_alpha_validation():
    with pytest.raises(ValueError):
        MemoryStore(alpha=2.0)


def test_memorystore_returns_relevant_hit_first():
    store = MemoryStore(dim=128, alpha=0.7)
    store.add("Cats are small mammals", item_id="cat")
    store.add("Python is a programming language", item_id="py")
    store.add("Quantum theory describes particles", item_id="qm")
    hits = store.search("python language", k=3)
    assert hits[0].item.id == "py"


def test_memorystore_add_document_chunks_and_indexes():
    store = MemoryStore(dim=64)
    text = ("Sentence A. " * 40)
    items = store.add_document(text, max_chars=80, overlap=10, metadata={"src": "doc"})
    assert len(items) >= 3
    assert all(it.metadata.get("src") == "doc" for it in items)
    hits = store.search("sentence a", k=1)
    assert hits and hits[0].item.metadata.get("src") == "doc"


def test_memorystore_empty_search_returns_empty():
    store = MemoryStore()
    assert store.search("anything") == []
