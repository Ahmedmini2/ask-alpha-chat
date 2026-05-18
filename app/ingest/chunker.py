def chunk_text(text: str, chunk_size: int = 1500, overlap: int = 200) -> list[str]:
    """Split text into overlapping chunks, preferring paragraph/sentence boundaries."""
    # Strip NUL bytes: pypdf occasionally returns them, and Postgres TEXT rejects them.
    text = text.replace("\x00", "").strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + chunk_size, n)
        if end < n:
            window_start = start + chunk_size // 2
            for sep in ("\n\n", ". ", "\n", " "):
                idx = text.rfind(sep, window_start, end)
                if idx > 0:
                    end = idx + len(sep)
                    break
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return chunks
