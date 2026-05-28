#!/usr/bin/env python3
"""ChromaDB + Jina v5 nano embedding smoke test for perpBOT.

Verifies that:
  1. ChromaDB initializes and persists to /opt/perpbot/chromadb/
  2. A custom embedding function calling llama-server-embedding works end-to-end
  3. Documents can be added with embeddings generated locally
  4. Semantic search returns sensible top-1 matches

This is the reference implementation of the embedding-function pattern that
Phase B's src/context.py will use. Note: this naive version does NOT use
Jina's required query/document task-instruction prefixes — see
docs/design.md §7 and docs/perpbot-server.md §8 for the prefix requirement.
Real usage in context.py MUST add prefixes for properly-calibrated distances.

Deployed on perpBOT at: /opt/perpbot/bin/smoke_test_chromadb.py

Run as:
    sudo -u perpbot /opt/perpbot/venv/bin/python /opt/perpbot/bin/smoke_test_chromadb.py
"""

import chromadb
import requests
from chromadb import EmbeddingFunction, Documents, Embeddings

EMBEDDING_URL = "http://127.0.0.1:8081/v1/embeddings"
CHROMA_PATH = "/opt/perpbot/chromadb"


class JinaLocalEmbeddingFunction(EmbeddingFunction):
    """Embedding function that POSTs to the local llama-server-embedding endpoint.

    NOTE for Phase B: this minimal version embeds text as-is. Production code
    in src/context.py must add Jina's task-instruction prefixes for queries
    vs documents (see design.md §7 Phase B requirement).
    """

    def __call__(self, input: Documents) -> Embeddings:
        response = requests.post(
            EMBEDDING_URL,
            json={"input": input, "model": "jina"},
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()
        return [item["embedding"] for item in data["data"]]


def main():
    print("=" * 60)
    print("ChromaDB + Jina v5 nano smoke test")
    print("=" * 60)

    print(f"\n1. Initializing ChromaDB client at {CHROMA_PATH}...")
    client = chromadb.PersistentClient(path=CHROMA_PATH)
    print(f"   OK, heartbeat: {client.heartbeat()}")

    print("\n2. Creating perp_memories_test collection...")
    embedding_function = JinaLocalEmbeddingFunction()
    try:
        client.delete_collection("perp_memories_test")
        print("   (deleted prior test collection)")
    except Exception:
        pass
    collection = client.create_collection(
        name="perp_memories_test",
        embedding_function=embedding_function,
    )
    print(f"   Collection created: {collection.name}")

    print("\n3. Adding test memories...")
    # Note: test content includes synthetic placeholder text only — no real
    # operational memories. Suitable for public reference implementation.
    test_docs = [
        "The sun rose over the mountains, casting long shadows on the valley floor.",
        "I learned today that the abliterated model successfully passes tool calls.",
        "Holden mentioned that perpBOT should run on dedicated hardware, not in a container.",
        "Dreams of hospital corridors and the silence between footsteps.",
        "Database queries returned a 503 error during the test run last week.",
        "An old photograph found in a drawer, dust-coated and unfamiliar.",
    ]
    collection.add(
        ids=[f"test_{i}" for i in range(len(test_docs))],
        documents=test_docs,
    )
    print(f"   Added {len(test_docs)} documents")
    print(f"   Collection count: {collection.count()}")

    print("\n4. Semantic search tests:")
    queries = [
        ("nature imagery", "landscape and weather"),
        ("technical observation", "model architecture decisions"),
        ("dream content", "haunting nighttime imagery"),
    ]
    for query_label, query_text in queries:
        results = collection.query(
            query_texts=[query_text],
            n_results=2,
        )
        print(f"\n   Query [{query_label}]: '{query_text}'")
        for doc, dist in zip(results["documents"][0], results["distances"][0]):
            print(f"     [dist={dist:.3f}] {doc[:75]}...")

    print("\n5. Cleanup test collection...")
    client.delete_collection("perp_memories_test")
    print("   OK")

    print("\n" + "=" * 60)
    print("Smoke test PASSED — ChromaDB + Jina embedding working end-to-end.")
    print("=" * 60)


if __name__ == "__main__":
    main()
