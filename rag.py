import os
from io import BytesIO
from typing import Optional, List
from langchain_core.documents import Document
from langchain_openai import OpenAIEmbeddings, ChatOpenAI
from langchain_text_splitters import RecursiveCharacterTextSplitter
import docx
from pypdf import PdfReader
from langchain_chroma import Chroma


# --------- Global RAG objects (in-memory) ---------

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY in environment variables.")

model = ChatOpenAI(model="gpt-4.1", temperature=0)
embeddings = OpenAIEmbeddings(model="text-embedding-3-large")
vector_store = Chroma(
    collection_name="slack_docs",
    embedding_function=embeddings,
    persist_directory="./chroma_db",
)

text_splitter = RecursiveCharacterTextSplitter(
    chunk_size=1000,
    chunk_overlap=200,
    add_start_index=True,
)


# --------- Helpers ---------

def _extract_pages_from_pdf(file_bytes: bytes) -> List[str]:
    reader = PdfReader(BytesIO(file_bytes))
    pages = []
    for page in reader.pages:
        pages.append((page.extract_text() or "").strip())
    return pages

def _extract_text_from_docx(file_bytes: bytes) -> str:
    """Extract paragraph text from a DOCX file."""
    doc = docx.Document(BytesIO(file_bytes))
    parts = []
    for para in doc.paragraphs:
        text = para.text.strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def _bytes_to_text(file_bytes: bytes, mimetype: str) -> str:
    mt = (mimetype or "").lower()

    if "pdf" in mt:
        return _extract_pages_from_pdf(file_bytes)

    if "officedocument.wordprocessingml.document" in mt or mt.endswith("/docx"):
        return _extract_text_from_docx(file_bytes)

    # default: treat as text-ish
    return file_bytes.decode("utf-8", errors="ignore").strip()


# --------- Public API used by Slack bot ---------
def index_slack_file_bytes(file_bytes: bytes, file_obj: dict, user_id: str, channel_id: str | None = None):
    """
    Convert file bytes -> Documents (per page) -> split -> embed -> add to vector_store.
    Returns list of document IDs inserted into the vector store.
    """
    mimetype = (file_obj.get("mimetype") or "").lower()
    file_id = file_obj.get("id")
    name = file_obj.get("name") or file_obj.get("title") or (file_id or "unknown")

    docs: List[Document] = []

    if "pdf" in mimetype:
        pages_text = _extract_pages_from_pdf(file_bytes)

        for page_num, page_text in enumerate(pages_text, start=1):
            if not page_text:
                continue

            docs.append(
                Document(
                    page_content=page_text,
                    metadata={
                        "source": "slack",
                        "user_id": user_id,
                        "page_number": page_num,
                        "slack_file_id": file_id,
                        "slack_filename": name,
                        "slack_channel": channel_id,
                        "mimetype": mimetype,
                    },
                )
            )

            #print (user_id, page_num)
    else:
        # non-PDF: treat as text-ish single doc (page_number omitted or set to 1)
        text = _bytes_to_text(file_bytes, mimetype)
        if text:
            docs.append(
                Document(
                    page_content=text,
                    metadata={
                        "source": "slack",
                        "user_id": user_id,
                        "page_number": 1,
                        "slack_file_id": file_id,
                        "slack_filename": name,
                        "slack_channel": channel_id,
                        "mimetype": mimetype,
                    },
                )
            )

    if not docs:
        return []

    splits = text_splitter.split_documents(docs)  
    ids = vector_store.add_documents(splits)
    return ids



def answer_query(query: str, slack_channel: Optional[str] = None, k: int = 4) -> str:
    """
    Similarity search -> prompt model with retrieved chunks -> return answer.
    """
    retrieved = vector_store.similarity_search(query, k=k)

    if not retrieved:
        return "I don’t have any indexed documents yet. Upload a file first."

    context_parts = []
    for d in retrieved:
        fname = d.metadata.get("slack_filename", "unknown")
        fid = d.metadata.get("slack_file_id", "")
        context_parts.append(
            f"FILE: {fname} ({fid})\n{d.page_content}"
     )

    context = "\n\n---\n\n".join(context_parts)

    resp = model.invoke(
        [
            {
                "role": "system",
                "content": (
                    "You answer questions using ONLY the provided context. "
                    "If the answer is not in the context, say you don't know."
                ),
            },
            {
                "role": "user",
                "content": f"QUESTION:\n{query}\n\nCONTEXT:\n{context}",
            },
        ]
    )

    # Log token usage for visibility when answering questions
    usage = resp.usage_metadata or resp.response_metadata.get("token_usage", {})
    if usage:
        print(
            "total:",
            usage.get("total_tokens"),
        )

    return resp.content

def delete_all_embeddings() -> int:

    # Print the current count of embeddings at the beginning
    current_count = vector_store._collection.count()
    print(f"Current embeddings count: {current_count}")

    existing = vector_store._collection.get(include=[])
    ids = existing.get("ids", []) or []

    if ids:
        vector_store._collection.delete(ids=ids)
        print(f"Deleting {len(ids)} embeddings.")
    else:
        print("No embeddings found to delete.")

    # Return the count of remaining embeddings in the collection
    remaining_count = vector_store._collection.count()
    print(f"Remaining embeddings: {remaining_count}")

    return vector_store._collection.count()
