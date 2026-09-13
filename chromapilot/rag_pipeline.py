"""
rag_pipeline.py — RAG pipeline: splitting, embedding, retriever, and LangGraph.

Depends on rag_source_loader.py for data loading.
This module handles everything after documents are loaded:
    documents → split → embed → FAISS vectorstore → retriever → RAG graph
"""

import pickle
import re
import time
from pathlib import Path
from typing import Literal, Optional

from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document
from langchain_core.tools.retriever import create_retriever_tool
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_core.messages import HumanMessage
from langgraph.graph import MessagesState, StateGraph, START, END
from langgraph.prebuilt import ToolNode, tools_condition
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
#  Prompts (same as original RAG.ipynb)
# ---------------------------------------------------------------------------
GRADE_PROMPT = (
    "You are a grader assessing whether a retrieved document is USEFUL for answering a user question.\n\n"
    "Here is the retrieved document:\n\n{context}\n\n"
    "Here is the user question: {question}\n\n"
    "Grading criteria:\n"
    "- The document must contain SUBSTANTIVE information that helps answer the question "
    "(e.g., code examples, API usage, parameter explanations, step-by-step instructions, "
    "or conceptual descriptions).\n"
    "- Documents that only contain navigation menus, file path listings, table of contents, "
    "page metadata, or boilerplate text should be graded as NOT relevant, "
    "even if they share keywords with the question.\n"
    "- Keyword overlap alone is NOT sufficient. The document must provide actionable knowledge.\n\n"
    "Give a binary score 'yes' or 'no' to indicate whether the document is useful for answering the question."
)

REWRITE_PROMPT = (
    "Look at the input and try to reason about the underlying semantic intent / meaning.\n"
    "Here is the initial question:"
    "\n ------- \n"
    "{question}"
    "\n ------- \n"
    "Formulate an improved question:"
)

GENERATE_PROMPT = (
    "You are an assistant for question-answering tasks. "
    "Use the following pieces of retrieved context to answer the question. "
    "If you don't know the answer, just say that you don't know. "
    "Use three sentences maximum and keep the answer concise.\n"
    "Question: {question} \n"
    "Context: {context}"
)

CONTEXTUAL_PROMPT = (
    "Here is the full document:\n<document>\n{doc_text}\n</document>\n\n"
    "Here is a chunk from this document:\n<chunk>\n{chunk_text}\n</chunk>\n\n"
    "Write a short (2-3 sentence) context to situate this chunk within the overall document "
    "for improving search retrieval. Focus on what specific function, tool, or analysis step "
    "this chunk describes. If the chunk is a navigation menu or boilerplate, say so explicitly. "
    "Respond with ONLY the context, nothing else."
)


class GradeDocuments(BaseModel):
    """Grade documents using a binary score for relevance check."""
    binary_score: str = Field(
        description="Relevance score: 'yes' if relevant, or 'no' if not relevant"
    )


# ---------------------------------------------------------------------------
#  Contextual Retrieval: enrich chunks with LLM-generated context prefixes
# ---------------------------------------------------------------------------
def _enrich_chunks_with_context(doc_splits, original_docs, model_name="gpt-4o-mini", verbose=True):
    """Prepend LLM-generated context to each chunk for improved retrieval.

    For each chunk, uses the full source document text to generate a 2-3
    sentence context that describes what the chunk is about. This context
    is prepended to the chunk's page_content before embedding.

    One-time cost: ~$0.20 for ~700 chunks with GPT-4o-mini.
    """
    # Build source → full text mapping from original (unsplit) documents
    source_texts = {}
    for doc in original_docs:
        src = doc.metadata.get("source", str(id(doc)))
        if src in source_texts:
            source_texts[src] += "\n\n" + doc.page_content
        else:
            source_texts[src] = doc.page_content

    model = ChatOpenAI(model=model_name, temperature=0)
    if verbose:
        print(f"🧠 Generating contextual prefixes for {len(doc_splits)} chunks (one-time cost)...")

    for i, chunk in enumerate(doc_splits):
        src = chunk.metadata.get("source", "")
        full_text = source_texts.get(src, "")[:4000]  # truncate to control cost
        prompt = CONTEXTUAL_PROMPT.format(
            doc_text=full_text, chunk_text=chunk.page_content[:2000]
        )
        try:
            response = model.invoke([{"role": "user", "content": prompt}])
            context = response.content.strip()
            chunk.page_content = f"{context}\n\n---\n\n{chunk.page_content}"
        except Exception as e:
            if verbose:
                print(f"  ⚠️ Failed to generate context for chunk {i}: {e}")

        if verbose and (i + 1) % 50 == 0:
            print(f"  [Contextual] {i + 1}/{len(doc_splits)} chunks processed")

    if verbose:
        print(f"  [Contextual] Done — all {len(doc_splits)} chunks enriched")

    return doc_splits


# ---------------------------------------------------------------------------
#  Doc splits persistence (for BM25 retriever alongside FAISS)
# ---------------------------------------------------------------------------
def _doc_splits_path(vectorstore_dir: str) -> Path:
    """Return the standard path for cached doc_splits alongside a vectorstore."""
    return Path(vectorstore_dir).parent / ".rag_doc_splits.pkl"


def load_doc_splits(vectorstore_dir: str, verbose: bool = True) -> list[Document]:
    """Load cached doc_splits from disk. Used to build BM25 retriever.

    Parameters
    ----------
    vectorstore_dir : str
        Same directory passed to build_retriever for FAISS cache.

    Returns
    -------
    list[Document]
        The chunked documents (with contextual prefixes if enabled).
        Empty list if no cache found.
    """
    path = _doc_splits_path(vectorstore_dir)
    if path.exists():
        with open(path, "rb") as f:
            splits = pickle.load(f)
        if verbose:
            print(f"✅ Loaded {len(splits)} cached doc splits for BM25")
        return splits
    if verbose:
        print("⚠️ No doc_splits cache found — BM25 retriever will be empty")
    return []


# ---------------------------------------------------------------------------
#  Lightweight retriever combinators (no extra package dependencies)
# ---------------------------------------------------------------------------
class SimpleEnsembleRetriever:
    """Combine multiple retrievers via Reciprocal Rank Fusion (RRF).

    Replaces langchain.retrievers.EnsembleRetriever which is unavailable
    in LangChain 1.x. Only depends on retrievers that implement invoke().
    """

    def __init__(self, retrievers: list, weights: list[float], rrf_k: int = 60):
        self.retrievers = retrievers
        self.weights = weights
        self.rrf_k = rrf_k

    def invoke(self, query: str) -> list[Document]:
        # Collect ranked results from each retriever
        doc_scores: dict[str, tuple[float, Document]] = {}
        for retriever, weight in zip(self.retrievers, self.weights):
            docs = retriever.invoke(query)
            for rank, doc in enumerate(docs):
                key = doc.page_content[:200]  # dedup key
                rrf_score = weight / (self.rrf_k + rank + 1)
                if key in doc_scores:
                    doc_scores[key] = (doc_scores[key][0] + rrf_score, doc_scores[key][1])
                else:
                    doc_scores[key] = (rrf_score, doc)
        # Return docs sorted by fused score
        ranked = sorted(doc_scores.values(), key=lambda x: x[0], reverse=True)
        return [doc for _, doc in ranked]


class SimpleMultiQueryRetriever:
    """Generate query variations with an LLM, retrieve for each, deduplicate.

    Replaces langchain.retrievers.MultiQueryRetriever which is unavailable
    in LangChain 1.x. Only depends on a retriever with invoke() and a
    LangChain chat model.
    """

    _MULTI_QUERY_PROMPT = (
        "You are an AI assistant helping to generate alternative search queries.\n"
        "Given the original query below, generate 3 alternative versions that "
        "capture different aspects or phrasings of the same information need.\n"
        "Return ONLY the 3 queries, one per line, no numbering or explanation.\n\n"
        "Original query: {question}"
    )

    def __init__(self, retriever, llm):
        self.retriever = retriever
        self.llm = llm

    def invoke(self, query: str, rrf_k: int = 60) -> list[Document]:
        # Generate query variations
        response = self.llm.invoke(
            [{"role": "user", "content": self._MULTI_QUERY_PROMPT.format(question=query)}]
        )
        variations = [q.strip() for q in response.content.strip().split("\n") if q.strip()]
        all_queries = [query] + variations[:3]  # original + up to 3 variations

        # Retrieve for each query and fuse with RRF across all queries
        doc_scores: dict[str, tuple[float, Document]] = {}
        for q in all_queries:
            for rank, doc in enumerate(self.retriever.invoke(q)):
                key = doc.page_content[:200]  # dedup key
                rrf_score = 1.0 / (rrf_k + rank + 1)
                if key in doc_scores:
                    doc_scores[key] = (doc_scores[key][0] + rrf_score, doc_scores[key][1])
                else:
                    doc_scores[key] = (rrf_score, doc)
        # Docs found by multiple query variations get higher fused scores
        ranked = sorted(doc_scores.values(), key=lambda x: x[0], reverse=True)
        return [doc for _, doc in ranked]


# ---------------------------------------------------------------------------
#  build_retriever: documents → split → embed → FAISS vectorstore
# ---------------------------------------------------------------------------
def build_retriever(
    docs: list[Document],
    *,
    chunk_size: int = 250,
    chunk_overlap: int = 50,
    vectorstore_dir: Optional[str] = None,
    use_contextual: bool = False,
    verbose: bool = True,
):
    """
    Build a FAISS vectorstore from loaded documents.

    If vectorstore_dir is provided and exists on disk, skip embedding and
    load directly from cache. Otherwise, split + embed + save.

    When use_contextual=True and building from scratch, each chunk is
    enriched with an LLM-generated context prefix before embedding.
    Cached vectorstores already contain enriched chunks, so this flag
    only takes effect on fresh builds. Clear the cache to re-enrich.

    Parameters
    ----------
    docs : list[Document]
        Raw documents from rag_source_loader (load_documents + load_files).
    chunk_size : int
        Chunk size in tokens for splitting.
    chunk_overlap : int
        Overlap between consecutive chunks in tokens.
    vectorstore_dir : str, optional
        Directory for FAISS vectorstore cache. None = no caching.
    use_contextual : bool
        If True, enrich each chunk with an LLM-generated context prefix
        before embedding (Contextual Retrieval). Only runs on first build;
        cached vectorstores already contain enriched chunks.
    verbose : bool
        Print progress info.

    Returns
    -------
    FAISS
        A FAISS vectorstore. Call .as_retriever() to get a retriever,
        optionally with search_kwargs={"filter": ..., "fetch_k": ...}
        for metadata filtering.
    """
    # --- try loading cached vectorstore ---
    if vectorstore_dir and Path(vectorstore_dir).exists():
        if verbose:
            print("✅ Loading cached vectorstore...")
            if use_contextual:
                print("   (use_contextual=True ignored — loading from cache. Clear cache to re-enrich.)")
        vectorstore = FAISS.load_local(
            vectorstore_dir,
            OpenAIEmbeddings(chunk_size=200),
            allow_dangerous_deserialization=True,
        )
        return vectorstore

    # --- split documents ---
    if verbose:
        print(f"📄 Splitting {len(docs)} documents (chunk_size={chunk_size}, overlap={chunk_overlap})...")
    splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
        chunk_size=chunk_size, chunk_overlap=chunk_overlap,
    )
    doc_splits = splitter.split_documents(docs)
    if verbose:
        print(f"   → {len(doc_splits)} chunks")

    # --- contextual retrieval: enrich chunks with LLM-generated prefixes ---
    if use_contextual:
        doc_splits = _enrich_chunks_with_context(doc_splits, docs, verbose=verbose)

    # --- embed and build vectorstore ---
    if verbose:
        print("🔢 Embedding documents...")
    vectorstore = FAISS.from_documents(
        documents=doc_splits, embedding=OpenAIEmbeddings(chunk_size=200)
    )

    # --- save cache ---
    if vectorstore_dir:
        vectorstore.save_local(vectorstore_dir)
        # Also save doc_splits for BM25 retriever (loaded via load_doc_splits)
        with open(_doc_splits_path(vectorstore_dir), "wb") as f:
            pickle.dump(doc_splits, f)
        if verbose:
            print(f"💾 Vectorstore saved → {vectorstore_dir}/")
            print(f"💾 Doc splits saved → {_doc_splits_path(vectorstore_dir)}")

    return vectorstore


# ---------------------------------------------------------------------------
#  Retry helper for model invocations
# ---------------------------------------------------------------------------
def _is_rate_limit_error(e: Exception) -> bool:
    msg = str(e).lower()
    return any(k in msg for k in ("rate limit", "ratelimit", "429", "too many requests", "resource exhausted"))


def _invoke_with_retry(llm, messages, retries: int = 3):
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            return llm.invoke(messages)
        except Exception as e:
            last_error = e
            if _is_rate_limit_error(e):
                print(f"    [rag_pipeline] Rate limit hit, sleeping 60s (attempt {attempt}/{retries})")
                time.sleep(60)
            else:
                print(f"    [rag_pipeline] invoke failed (attempt {attempt}/{retries}): {e}")
    raise last_error


# ---------------------------------------------------------------------------
#  agentic_retrieve: LLM-driven retrieval with grading + query rewrite
# ---------------------------------------------------------------------------
def agentic_retrieve(
    query: str,
    retriever,
    model,
    *,
    reranker=None,
    rerank_top_n: int = 8,
    max_retries: int = 0,
    verbose: bool = True,
) -> list[Document]:
    """
    Corrective retrieval: always retrieve, grade each document for
    relevance, and rewrite the query if nothing is relevant.

    Unlike raw retriever.invoke(), this function:
      1. Optionally reranks retrieved documents (cross-encoder)
      2. Grades each document (filters out noise)
      3. Rewrites the query and retries if no relevant docs found

    Parameters
    ----------
    query : str
        The user's question or task description.
    retriever
        A LangChain retriever (output of vectorstore.as_retriever()).
    model
        A LangChain chat model.
    reranker : flashrank.Ranker, optional
        A FlashRank Ranker instance. If provided, retrieved docs are
        reranked and truncated to rerank_top_n before grading.
    rerank_top_n : int
        Number of top docs to keep after reranking. Default 8.
    max_retries : int
        Max query-rewrite-and-retry cycles. Default 2.
    verbose : bool
        Print progress info.

    Returns
    -------
    list[Document]
        Documents that passed grading. Empty list if nothing relevant
        was found after all retries.
    """
    # --- Retrieve → grade → rewrite loop ---
    current_query = query
    for attempt in range(max_retries + 1):
        docs = retriever.invoke(current_query)
        if verbose:
            print(f"  [Corrective RAG] Attempt {attempt + 1}: retrieved {len(docs)} docs")

        if not docs:
            if verbose:
                print("  [Corrective RAG] No documents retrieved, stopping")
            return []

        # Rerank with cross-encoder if provided (before grading to save LLM calls)
        if reranker is not None:
            from flashrank import RerankRequest
            passages = [{"id": i, "text": doc.page_content[:2000]} for i, doc in enumerate(docs)]
            results = reranker.rerank(RerankRequest(query=current_query, passages=passages))
            reranked_indices = [r["id"] for r in results[:rerank_top_n]]
            docs = [docs[i] for i in reranked_indices]
            if verbose:
                print(f"  [Corrective RAG] Reranked → keeping top {len(docs)} docs")

        # Grade each document
        relevant = []
        for i, doc in enumerate(docs):
            prompt = GRADE_PROMPT.format(
                context=doc.page_content, question=current_query
            )
            grade = _invoke_with_retry(
                model.with_structured_output(GradeDocuments),
                [{"role": "user", "content": prompt}],
            )
            is_rel = grade.binary_score == "yes"
            if verbose:
                tag = "✅" if is_rel else "❌"
                content_type = doc.metadata.get("content_type", "?")
                # Split contextual prefix from original content at the --- separator
                parts = doc.page_content.split("\n\n---\n\n", 1)
                if len(parts) == 2:
                    ctx = re.sub(r'\n{3,}', '\n\n', parts[0][:200])
                    body = re.sub(r'\n{3,}', '\n\n', parts[1][:400])
                    print(f"    [doc {i}] {tag} [{content_type}] {ctx}...")
                    print(f"             content: {body}...")
                else:
                    preview = re.sub(r'\n{3,}', '\n\n', doc.page_content[:400])
                    print(f"    [doc {i}] {tag} [{content_type}] {preview}...")
                source = doc.metadata.get("source", "")
                if source:
                    print(f"             source: {source[:120]}")
            if is_rel:
                relevant.append(doc)

        if relevant:
            if verbose:
                print(f"  [Corrective RAG] Returning {len(relevant)} relevant docs")
            return relevant

        # All docs irrelevant — rewrite and retry
        if attempt < max_retries:
            rewritten = _invoke_with_retry(
                model,
                [{"role": "user", "content": REWRITE_PROMPT.format(question=current_query)}],
            )
            current_query = rewritten.content
            if verbose:
                print(f"  [Corrective RAG] No relevant docs → rewriting query")
                print(f"  [Corrective RAG] New query: '{current_query[:80]}...'")
        else:
            if verbose:
                print("  [Corrective RAG] Max retries reached, returning empty")

    return []


# ---------------------------------------------------------------------------
#  build_rag_graph: retriever + model → compiled LangGraph
# ---------------------------------------------------------------------------
def build_rag_graph(retriever, model):
    """
    Build the RAG LangGraph: query/respond → retrieve → grade → answer/rewrite.

    Parameters
    ----------
    retriever
        A LangChain retriever (e.g. from vectorstore.as_retriever()).
    model
        A LangChain chat model (e.g. from init_chat_model or CONFIG.build_model()).
        Used for all LLM calls: query generation, grading, rewriting, answering.

    Returns
    -------
    CompiledGraph
        A compiled LangGraph that can be invoked or streamed.
    """
    # --- tools ---
    retriever_tool = create_retriever_tool(
        retriever,
        "retrieve_blog_posts",
        "Search and return information from the RAG knowledge base.",
    )

    # --- node functions (close over model and retriever_tool) ---
    def generate_query_or_respond(state: MessagesState):
        """Decide whether to retrieve or respond directly."""
        response = model.bind_tools([retriever_tool]).invoke(state["messages"])
        return {"messages": [response]}

    def grade_documents(
        state: MessagesState,
    ) -> Literal["generate_answer", "rewrite_question"]:
        """Grade retrieved documents for relevance."""
        question = state["messages"][0].content
        context = state["messages"][-1].content
        prompt = GRADE_PROMPT.format(question=question, context=context)
        response = (
            model
            .with_structured_output(GradeDocuments)
            .invoke([{"role": "user", "content": prompt}])
        )
        return "generate_answer" if response.binary_score == "yes" else "rewrite_question"

    def rewrite_question(state: MessagesState):
        """Rewrite the original user question for better retrieval."""
        question = state["messages"][0].content
        prompt = REWRITE_PROMPT.format(question=question)
        response = model.invoke([{"role": "user", "content": prompt}])
        return {"messages": [HumanMessage(content=response.content)]}

    def generate_answer(state: MessagesState):
        """Generate an answer based on retrieved context."""
        question = state["messages"][0].content
        context = state["messages"][-1].content
        prompt = GENERATE_PROMPT.format(question=question, context=context)
        response = model.invoke([{"role": "user", "content": prompt}])
        return {"messages": [response]}

    # --- assemble graph ---
    workflow = StateGraph(MessagesState)

    workflow.add_node(generate_query_or_respond)
    workflow.add_node("retrieve", ToolNode([retriever_tool]))
    workflow.add_node(rewrite_question)
    workflow.add_node(generate_answer)

    workflow.add_edge(START, "generate_query_or_respond")
    workflow.add_conditional_edges(
        "generate_query_or_respond",
        tools_condition,
        {"tools": "retrieve", END: END},
    )
    workflow.add_conditional_edges("retrieve", grade_documents)
    workflow.add_edge("generate_answer", END)
    workflow.add_edge("rewrite_question", "generate_query_or_respond")

    return workflow.compile()
