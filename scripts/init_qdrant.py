#!/usr/bin/env python3
"""Provision the `documents_kb` Qdrant collection (RAG-01).

Idempotent — safe to run on every deploy/startup, and safe to run twice in a
row: it never drops or recreates a collection that already exists. See
`src.services.qdrant` for what it actually sets up.

Usage:
    python scripts/init_qdrant.py
"""

from src.services.qdrant import COLLECTION_NAME, ensure_collection, get_qdrant_client


def main() -> None:
    client = get_qdrant_client()
    ensure_collection(client)
    print(f"Qdrant collection '{COLLECTION_NAME}' ready.")


if __name__ == "__main__":
    main()
