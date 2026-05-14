"""
Ingest the Environ Property Services knowledge base into ChromaDB.
Run this once before starting the server: python ingest.py

Embeddings are handled locally by ChromaDB's built-in model (all-MiniLM-L6-v2
via ONNX). No OpenAI API key or any external embedding service is required.
"""
import re
import time
from pathlib import Path
import chromadb

CHROMA_PATH      = "./chroma_db"
COLLECTION_NAME  = "environ_knowledge"
MAX_CHUNK        = 1200

# Look for the data file in common locations
DATA_PATHS = [
    "data/knowledge_base.txt",
    r"C:\Users\Crown Tech\Downloads\Cleaned_Data.txt",
]


def find_data_file() -> Path:
    for p in DATA_PATHS:
        path = Path(p)
        if path.exists():
            return path
    raise FileNotFoundError(
        f"Knowledge base file not found. Tried: {DATA_PATHS}\n"
        "Copy your Cleaned_Data.txt to data/knowledge_base.txt"
    )


def chunk_text(text: str) -> list[str]:
    """Split text by --- section dividers, then by paragraphs if too large."""
    chunks = []
    sections = re.split(r"\n---+\n", text)

    for section in sections:
        section = section.strip()
        if not section or len(section) < 80:
            continue

        if len(section) <= MAX_CHUNK:
            chunks.append(section)
            continue

        # Split large sections by double newline
        paras = [p.strip() for p in section.split("\n\n") if p.strip()]
        current = ""
        for para in paras:
            candidate = (current + "\n\n" + para).strip()
            if len(candidate) <= MAX_CHUNK:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                if len(para) > MAX_CHUNK:
                    # Last resort: split by single newline
                    lines = [ln.strip() for ln in para.split("\n") if ln.strip()]
                    cur = ""
                    for line in lines:
                        test = (cur + "\n" + line).strip()
                        if len(test) <= MAX_CHUNK:
                            cur = test
                        else:
                            if cur:
                                chunks.append(cur)
                            cur = line
                    current = cur
                else:
                    current = para

        if current:
            chunks.append(current)

    return chunks


def main():
    data_file = find_data_file()
    print(f"Reading: {data_file}")
    text = data_file.read_text(encoding="utf-8")

    chunks = chunk_text(text)
    print(f"Created {len(chunks)} chunks")

    # Reset collection
    chroma = chromadb.PersistentClient(path=CHROMA_PATH)
    try:
        chroma.delete_collection(COLLECTION_NAME)
        print("Cleared existing collection")
    except Exception:
        pass

    # No embedding_function argument → ChromaDB uses its built-in local model
    # (all-MiniLM-L6-v2 via ONNX). Embeddings are generated on-device; no API key needed.
    collection = chroma.create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )

    # Store in batches — ChromaDB embeds automatically when only documents are provided
    batch_size = 50
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i : i + batch_size]
        collection.add(
            documents=batch,
            ids=[f"c{i + j}" for j in range(len(batch))],
        )
        print(f"  Indexed {min(i + batch_size, len(chunks))}/{len(chunks)} chunks")
        if i + batch_size < len(chunks):
            time.sleep(0.05)

    print(f"\nDone! {collection.count()} chunks stored in ChromaDB.")


if __name__ == "__main__":
    main()
