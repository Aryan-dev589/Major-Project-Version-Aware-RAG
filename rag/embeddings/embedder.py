"""
rag/embeddings/embedder.py
Generates embeddings for policy chunks.

Primary:   BAAI/bge-small-en-v1.5 via sentence-transformers (best quality/speed)
Fallback:  all-MiniLM-L6-v2 (lighter)
Emergency: TF-IDF (if sentence-transformers not installed — no GPU needed)

Usage:
    embedder = get_embedder()
    vectors = embedder.embed(["text1", "text2"])
    query_vec = embedder.embed_query("how many leave days?")
"""
import math
import re
from abc import ABC, abstractmethod
from collections import Counter
from typing import Optional


class BaseEmbedder(ABC):
    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts. Returns list of float vectors."""

    @abstractmethod
    def embed_query(self, query: str) -> list[float]:
        """Embed a single query (some models use different instructions for queries)."""

    @property
    @abstractmethod
    def dimension(self) -> int:
        """Vector dimensionality."""

    @property
    @abstractmethod
    def model_name(self) -> str:
        pass


class SentenceTransformerEmbedder(BaseEmbedder):
    """
    Wraps sentence-transformers. Supports BGE models with query instruction prefix.
    """
    QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5"):
        from sentence_transformers import SentenceTransformer
        self._model_name = model_name
        self._model = SentenceTransformer(model_name)
        self._dim = self._model.get_sentence_embedding_dimension()
        self._is_bge = "bge" in model_name.lower()

    def embed(self, texts: list[str]) -> list[list[float]]:
        vecs = self._model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return vecs.tolist()

    def embed_query(self, query: str) -> list[float]:
        # BGE models expect an instruction prefix for queries
        text = self.QUERY_INSTRUCTION + query if self._is_bge else query
        vec = self._model.encode([text], normalize_embeddings=True, show_progress_bar=False)
        return vec[0].tolist()

    @property
    def dimension(self) -> int:
        return self._dim

    @property
    def model_name(self) -> str:
        return self._model_name


class TFIDFEmbedder(BaseEmbedder):
    """
    Pure-Python TF-IDF embedder using the "hashing trick".
    No GPU, no heavy deps — works with just Python stdlib.
    Lower quality than neural embeddings but fully offline.

    Why hashing instead of a learned vocabulary:
    A vocabulary built from "the top N most common terms seen so far" changes
    shape every time new text is indexed (more unique words -> longer vocab),
    which produces a different vector length each time and breaks ChromaDB
    ("Collection expecting embedding with dimension of 56, got 93"). It would
    also change *which* term each vector position represents between runs,
    silently corrupting the similarity of anything indexed earlier — and
    since Python's built-in hash() is randomized per process, even a
    hash-based vocabulary would shift every time the app restarts unless it
    uses a stable hash. This version hashes each token into one of a FIXED
    number of buckets with a stable hash (md5), so:
      - dimension is always exactly VOCAB_SIZE, regardless of corpus size
      - the same term always lands in the same bucket, in this run or the next
    Document-frequency weighting is still tracked (best-effort, in-memory,
    resets on restart) purely to down-weight very common words.
    """
    VOCAB_SIZE = 512
    _STOPWORDS = set(
        "a an the is are was were be been of to in on for and or with as at by "
        "from this that it its they he she we you your our can may will would "
        "should could what how many do does did all any some have has had".split()
    )

    def __init__(self):
        self._doc_freq: Counter = Counter()
        self._n_docs = 0

    def _tokenize(self, text: str) -> list[str]:
        tokens = re.findall(r"[a-z0-9]+", text.lower())
        return [t for t in tokens if len(t) > 2 and t not in self._STOPWORDS]

    @classmethod
    def _bucket(cls, term: str) -> int:
        import hashlib
        h = hashlib.md5(term.encode("utf-8")).hexdigest()
        return int(h, 16) % cls.VOCAB_SIZE

    def _observe(self, tokenized_docs: list[list[str]]):
        """Update running document-frequency stats (best-effort idf weighting)."""
        for tokens in tokenized_docs:
            self._n_docs += 1
            for t in set(tokens):
                self._doc_freq[t] += 1

    def _idf(self, term: str) -> float:
        df = self._doc_freq.get(term, 0)
        return math.log((self._n_docs + 1) / (df + 1)) + 1

    def _vectorize(self, text: str) -> list[float]:
        tokens = self._tokenize(text)
        tf = Counter(tokens)
        total = len(tokens) or 1
        vec = [0.0] * self.VOCAB_SIZE
        for term, count in tf.items():
            bucket = self._bucket(term)
            vec[bucket] += (count / total) * self._idf(term)
        mag = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / mag for v in vec]

    def embed(self, texts: list[str]) -> list[list[float]]:
        tokenized = [self._tokenize(t) for t in texts]
        self._observe(tokenized)
        return [self._vectorize(t) for t in texts]

    def embed_query(self, query: str) -> list[float]:
        return self._vectorize(query)

    @property
    def dimension(self) -> int:
        return self.VOCAB_SIZE

    @property
    def model_name(self) -> str:
        return "tfidf-hashing-fallback-512"


# Singleton
_embedder_instance: Optional[BaseEmbedder] = None


def get_embedder(model_name: str = None) -> BaseEmbedder:
    """
    Returns the best available embedder.
    Tries sentence-transformers first, falls back to TF-IDF.
    """
    global _embedder_instance
    if _embedder_instance is not None:
        return _embedder_instance
    # Allow overriding the embedder model via env var to avoid large downloads.
    import os
    if model_name is None:
        model_name = os.environ.get("EMBEDDER_MODEL", "sentence-transformers/all-MiniLM-L6-v2")

    try:
        _embedder_instance = SentenceTransformerEmbedder(model_name)
        print(f"[Embedder] Using sentence-transformers: {model_name}")
    except ImportError:
        _embedder_instance = TFIDFEmbedder()
        print("[Embedder] sentence-transformers not installed — using TF-IDF fallback")
    except Exception as e:
        print(f"[Embedder] Failed to load {model_name}: {e} — using TF-IDF fallback")
        _embedder_instance = TFIDFEmbedder()

    return _embedder_instance


def reset_embedder():
    """Force reload embedder (useful when switching models)."""
    global _embedder_instance
    _embedder_instance = None
