"""
rag_source_loader.py — Load RAG document sources from JSON config with disk caching.
"""

import hashlib
import json
import pickle
import re
from pathlib import Path
from typing import Optional

import trafilatura
from langchain_community.document_loaders import PyPDFLoader, TextLoader, WebBaseLoader
from langchain_core.documents import Document

_MODULE_DIR = Path(__file__).resolve().parent if "__file__" in dir() else Path.cwd()
DEFAULT_CONFIG_PATH = _MODULE_DIR / "rag_sources_default.json"
DEFAULT_CACHE_DIR = _MODULE_DIR / ".rag_cache"


def load_config(
    config_path: Optional[str] = None,
    user_config_path: Optional[str] = None,
) -> dict:
    """Load source config JSON. Optionally merge a user override (adds/replaces source groups)."""
    base_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if not base_path.exists():
        raise FileNotFoundError(f"Config not found: {base_path}")

    with open(base_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    if user_config_path:
        user_path = Path(user_config_path)
        if not user_path.exists():
            raise FileNotFoundError(f"User config not found: {user_path}")
        with open(user_path, "r", encoding="utf-8") as f:
            user_cfg = json.load(f)
        if "sources" in user_cfg:
            config.setdefault("sources", {}).update(user_cfg["sources"])

    return config


def collect_urls(config: dict, groups: Optional[list[str]] = None) -> list[str]:
    """Flatten source groups into a deduplicated URL list. Pass groups= to select a subset."""
    sources = config.get("sources", {})
    if groups is not None:
        missing = set(groups) - set(sources.keys())
        if missing:
            raise ValueError(f"Unknown source groups: {missing}. Available: {list(sources.keys())}")
        sources = {k: v for k, v in sources.items() if k in groups}

    seen = set()
    urls = []
    for group_info in sources.values():
        for url in group_info.get("urls", []):
            normalized = url.rstrip("#").rstrip("/")
            if normalized not in seen:
                seen.add(normalized)
                urls.append(url)
    return urls


def collect_urls_with_metadata(config: dict, groups: Optional[list[str]] = None) -> list[dict]:
    """Flatten source groups into a deduplicated list of {url, group, content_type} dicts.

    This is the metadata-aware version of collect_urls(). Each returned dict
    carries the group name and content_type so that downstream loaders can
    stamp every Document with the right metadata for filtered retrieval.
    """
    sources = config.get("sources", {})
    if groups is not None:
        missing = set(groups) - set(sources.keys())
        if missing:
            raise ValueError(f"Unknown source groups: {missing}. Available: {list(sources.keys())}")
        sources = {k: v for k, v in sources.items() if k in groups}

    seen = set()
    url_records = []
    for group_name, group_info in sources.items():
        content_type = group_info.get("content_type", "general")
        for url in group_info.get("urls", []):
            normalized = url.rstrip("#").rstrip("/")
            if normalized not in seen:
                seen.add(normalized)
                url_records.append({
                    "url": url,
                    "group": group_name,
                    "content_type": content_type,
                })
    return url_records


def load_documents(
    urls: list[str],
    *,
    use_cache: bool = True,
    cache_dir: Optional[str] = None,
    verbose: bool = True,
) -> list[Document]:
    """Fetch documents from URLs with disk caching. Cache turns multi-minute fetches into <1s."""
    root = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
    key = hashlib.sha256("|".join(sorted(urls)).encode()).hexdigest()[:16]
    cache_path = root / f"docs_{key}.pkl"

    if use_cache and cache_path.exists():
        if verbose:
            print(f"✅ Document cache hit — loading from {cache_path.name}")
        with open(cache_path, "rb") as f:
            return pickle.load(f)

    if verbose:
        print(f"🌐 Fetching {len(urls)} URLs (this may take a minute)...")

    all_docs: list[Document] = []
    for i, url in enumerate(urls, 1):
        docs = WebBaseLoader(url).load()
        all_docs.extend(docs)
        if verbose:
            print(f"  [{i}/{len(urls)}] ✓ {url[:80]}")

    if use_cache and all_docs:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "wb") as f:
            pickle.dump(all_docs, f)
        if verbose:
            print(f"💾 Document cache saved → {cache_path.name}")

    return all_docs


def load_documents_with_metadata(
    url_records: list[dict],
    *,
    use_cache: bool = True,
    cache_dir: Optional[str] = None,
    verbose: bool = True,
) -> list[Document]:
    """Fetch documents from URL records and inject group/content_type into metadata.

    Parameters
    ----------
    url_records : list[dict]
        Output of collect_urls_with_metadata(). Each dict has keys:
        url, group, content_type.

    Returns
    -------
    list[Document]
        Documents with metadata['group'] and metadata['content_type'] set.
    """
    sorted_urls = sorted(r["url"] for r in url_records)
    root = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
    key = hashlib.sha256("|".join(sorted_urls).encode()).hexdigest()[:16]
    cache_path = root / f"docs_meta_{key}.pkl"

    if use_cache and cache_path.exists():
        if verbose:
            print(f"✅ Document cache hit (with metadata) — loading from {cache_path.name}")
        with open(cache_path, "rb") as f:
            return pickle.load(f)

    if verbose:
        print(f"🌐 Fetching {len(url_records)} URLs with metadata (this may take a minute)...")

    all_docs: list[Document] = []
    for i, rec in enumerate(url_records, 1):
        url, group, content_type = rec["url"], rec["group"], rec["content_type"]
        # Use trafilatura for main-content extraction (strips nav/sidebar/footer).
        # Falls back to WebBaseLoader if trafilatura returns empty.
        downloaded = trafilatura.fetch_url(url)
        text = trafilatura.extract(downloaded, include_comments=False) if downloaded else None
        if text and text.strip():
            docs = [Document(page_content=text, metadata={"source": url})]
        else:
            docs = WebBaseLoader(url).load()  # fallback: full page including nav
        for doc in docs:
            doc.metadata["group"] = group
            doc.metadata["content_type"] = content_type
            doc.page_content = re.sub(r'\n{3,}', '\n\n', doc.page_content)
        all_docs.extend(docs)
        if verbose:
            print(f"  [{i}/{len(url_records)}] ✓ [{content_type:8s}] {url[:70]}")

    if use_cache and all_docs:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "wb") as f:
            pickle.dump(all_docs, f)
        if verbose:
            print(f"💾 Document cache saved (with metadata) → {cache_path.name}")

    return all_docs


_FILE_LOADERS = {
    ".pdf": PyPDFLoader,
    ".txt": TextLoader,
    ".md": TextLoader,
    ".csv": TextLoader,
}


def collect_files(config: dict, groups: Optional[list[str]] = None) -> list[str]:
    """Flatten source groups into a deduplicated file path list. Only includes type=file groups."""
    sources = config.get("sources", {})
    if groups is not None:
        missing = set(groups) - set(sources.keys())
        if missing:
            raise ValueError(f"Unknown source groups: {missing}. Available: {list(sources.keys())}")
        sources = {k: v for k, v in sources.items() if k in groups}

    seen = set()
    paths = []
    for group_info in sources.values():
        for p in group_info.get("paths", []):
            resolved = str(Path(p).resolve())
            if resolved not in seen:
                seen.add(resolved)
                paths.append(p)
    return paths


def collect_files_with_metadata(config: dict, groups: Optional[list[str]] = None) -> list[dict]:
    """Flatten source groups into a deduplicated list of {path, group, content_type} dicts.

    This is the metadata-aware version of collect_files(). Each returned dict
    carries the group name and content_type for downstream metadata injection.
    """
    sources = config.get("sources", {})
    if groups is not None:
        missing = set(groups) - set(sources.keys())
        if missing:
            raise ValueError(f"Unknown source groups: {missing}. Available: {list(sources.keys())}")
        sources = {k: v for k, v in sources.items() if k in groups}

    seen = set()
    file_records = []
    for group_name, group_info in sources.items():
        content_type = group_info.get("content_type", "general")
        for p in group_info.get("paths", []):
            resolved = str(Path(p).resolve())
            if resolved not in seen:
                seen.add(resolved)
                file_records.append({
                    "path": p,
                    "group": group_name,
                    "content_type": content_type,
                })
    return file_records


def load_files(
    paths: list[str],
    *,
    verbose: bool = True,
) -> list[Document]:
    """Load documents from local files (PDF, TXT, MD, CSV). Fail-fast on missing files."""
    if not paths:
        return []

    if verbose:
        print(f"📂 Loading {len(paths)} local files...")

    all_docs: list[Document] = []
    for i, p in enumerate(paths, 1):
        filepath = Path(p)
        if not filepath.exists():
            raise FileNotFoundError(f"Source file not found: {p}")

        ext = filepath.suffix.lower()
        loader_cls = _FILE_LOADERS.get(ext)
        if loader_cls is None:
            raise ValueError(f"Unsupported file type '{ext}' for: {p}. Supported: {list(_FILE_LOADERS.keys())}")

        docs = loader_cls(str(filepath)).load()
        all_docs.extend(docs)
        if verbose:
            print(f"  [{i}/{len(paths)}] ✓ {p} ({len(docs)} docs)")

    return all_docs


def load_files_with_metadata(
    file_records: list[dict],
    *,
    verbose: bool = True,
) -> list[Document]:
    """Load local files and inject group/content_type into metadata.

    Parameters
    ----------
    file_records : list[dict]
        Output of collect_files_with_metadata(). Each dict has keys:
        path, group, content_type.

    Returns
    -------
    list[Document]
        Documents with metadata['group'] and metadata['content_type'] set.
    """
    if not file_records:
        return []

    if verbose:
        print(f"📂 Loading {len(file_records)} local files with metadata...")

    all_docs: list[Document] = []
    for i, rec in enumerate(file_records, 1):
        p, group, content_type = rec["path"], rec["group"], rec["content_type"]
        filepath = Path(p)
        if not filepath.exists():
            raise FileNotFoundError(f"Source file not found: {p}")

        ext = filepath.suffix.lower()
        loader_cls = _FILE_LOADERS.get(ext)
        if loader_cls is None:
            raise ValueError(f"Unsupported file type '{ext}' for: {p}. Supported: {list(_FILE_LOADERS.keys())}")

        docs = loader_cls(str(filepath)).load()
        for doc in docs:
            doc.metadata["group"] = group
            doc.metadata["content_type"] = content_type
            doc.page_content = re.sub(r'\n{3,}', '\n\n', doc.page_content)  # compress excessive blank lines
        all_docs.extend(docs)
        if verbose:
            print(f"  [{i}/{len(file_records)}] ✓ [{content_type:8s}] {p} ({len(docs)} docs)")

    return all_docs
