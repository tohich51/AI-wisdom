"""Immutable configuration for the selected embedding model."""

import hashlib
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class EmbeddingProfile:
    name: str
    provider: str
    api_base: str
    model: str
    dimension: int
    normalization: str
    query_instruction: str
    chunking_policy: str
    processor_version: str

    def config(self) -> dict:
        """Return a fresh dictionary containing every profile value."""
        return {
            "name": self.name,
            "provider": self.provider,
            "api_base": self.api_base,
            "model": self.model,
            "dimension": self.dimension,
            "normalization": self.normalization,
            "query_instruction": self.query_instruction,
            "chunking_policy": self.chunking_policy,
            "processor_version": self.processor_version,
        }

    @property
    def profile_hash(self) -> str:
        serialized = json.dumps(
            self.config(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def query_text(self, text: str) -> str:
        if not isinstance(text, str):
            raise TypeError("query text must be a string")
        trimmed = text.strip()
        if not trimmed:
            raise ValueError("query text must not be blank")
        if len(trimmed) > 2000:
            raise ValueError("query text must be at most 2000 characters")
        return f"Instruct: {self.query_instruction}\nQuery: {trimmed}"


SELECTED_PROFILE = EmbeddingProfile(
    name="openrouter:qwen3-embedding-8b:4096:v1",
    provider="openai",
    api_base="https://openrouter.ai/api/v1",
    model="qwen/qwen3-embedding-8b",
    dimension=4096,
    normalization="provider_native",
    query_instruction=(
        "Given a web search query, retrieve relevant passages that answer the query"
    ),
    chunking_policy="published-knowledge-markdown:v1",
    processor_version="OpenViking0.5.0-vectors_only",
)
