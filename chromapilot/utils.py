# helper function 
from typing import Optional, List, Tuple
import os

import os
from typing import List, Optional

from pathlib import Path
import json
from typing import Any, Iterable
import os
import json
from pathlib import Path
from collections.abc import Iterable


def generate_summary(output_dir, messages, full_plan, input_file_path):
    """
    Save summary artifacts to separate files under `output_dir`/summary.

    Parameters
    ----------
    output_dir : str or Path or None
        Directory where all summary files will be written. If None or empty,
        defaults to "./report_output".
    messages : Iterable[Any] or single message or None
        Typically a list of BaseMessage objects (e.g., LangChain messages).
    full_plan : Any or None
        The full pipeline plan to save. Will be converted to str.
    input_file_path : Iterable[str] or str or None
        List of input file paths to save, one per line. A single string is
        treated as one path, not as an iterable of characters.

    Returns
    -------
    dict
        Mapping from logical name to the actual file path written (as strings).
    """
    # ---- Normalize and prepare output directory ----
    if not output_dir:
        # Fallback if state.output_dir is None or ""
        base_dir = Path("report_output")
    else:
        base_dir = Path(output_dir)

    # Always write into a "test_summary" subdirectory
    out_dir = base_dir / "summary"
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---- Define file paths (always Path objects) ----
    full_plan_path = out_dir / "full_plan.txt"
    input_paths_path = out_dir / "input_file_path.txt"
    messages_path = out_dir / "messages.json"

    # ---- Save full_plan ----
    # Allow anything that can be stringified
    with full_plan_path.open("w", encoding="utf-8") as f:
        if full_plan is not None:
            f.write(str(full_plan))

    # ---- Normalize input_file_path to a list-like of paths ----
    input_paths_iter = []
    if input_file_path:
        # If it's a single string, treat as one path (not iterable of chars)
        if isinstance(input_file_path, (str, os.PathLike)):
            input_paths_iter = [input_file_path]
        elif isinstance(input_file_path, Iterable):
            input_paths_iter = list(input_file_path)
        else:
            # Last-resort: stringify and write as a single "path"
            input_paths_iter = [str(input_file_path)]

    # ---- Save input_file_path (one path per line) ----
    with input_paths_path.open("w", encoding="utf-8") as f:
        for p in input_paths_iter:
            f.write(f"{p}\n")

    # ---- Normalize messages to an iterable of messages ----
    msg_iterable = []
    if messages:
        if isinstance(messages, Iterable) and not isinstance(messages, (str, bytes, dict)):
            msg_iterable = list(messages)
        else:
            # Single message / dict / string → wrap
            msg_iterable = [messages]

    # ---- Save messages as JSON (role + content) ----
    msg_list = []
    for m in msg_iterable:
        # LangChain / OpenAI messages usually have .type or .role and .content
        role = getattr(m, "type", None) or getattr(m, "role", None)
        content = getattr(m, "content", None)

        # If it's a dict, try dict-style access
        if content is None and isinstance(m, dict):
            role = role or m.get("role")
            content = m.get("content")

        # Fallback: stringify the whole object
        if content is None:
            content = str(m)

        msg_list.append({
            "role": role,
            "content": content,
        })

    with messages_path.open("w", encoding="utf-8") as f:
        json.dump(msg_list, f, ensure_ascii=False, indent=2)

    # ---- Return the paths as strings ----
    return {
        "full_plan": str(full_plan_path),
        "input_file_path": str(input_paths_path),
        "messages": str(messages_path),
    }
    
def expand_input_file_paths(input_file_path: Optional[List[str]] = None):
    """
    Expand directories in the input list to include all file paths recursively.
    
    Args:
        input_file_path (Optional[List[str]]): Initial list of file or folder paths.
    
    Returns:
        Tuple[List[str], bool]: Updated list including all file paths (with directories expanded),
                                and True if new files were found during expansion.
    """
    if input_file_path is None:
        return [], False
    
    # Keep track of original file paths (normalize them)
    original_files = set()
    for path in input_file_path:
        if os.path.isfile(path):
            original_files.add(os.path.abspath(path))
    
    expanded_paths = []
    all_files = set()
    
    for path in input_file_path:
        if os.path.isdir(path):
            # Recursively walk through the folder
            for root, _, files in os.walk(path):
                for name in files:
                    file_path = os.path.abspath(os.path.join(root, name))
                    expanded_paths.append(file_path)
                    all_files.add(file_path)
        elif os.path.isfile(path):
            file_path = os.path.abspath(path)
            expanded_paths.append(file_path)
            all_files.add(file_path)
        else:
            print(f"⚠️ Path does not exist or is invalid: {path}")
    
    # Check if there are new files (files that weren't in the original input)
    new_files_detected = bool(all_files - original_files)
    
    return expanded_paths, new_files_detected

def check_files_exist(input_file_path: Optional[List[str]] = None) -> Tuple[bool, List[str]]:
    """
    Check if all files in the provided list exist.

    Args:
        input_file_path: Optional list of file paths to check

    Returns:
        Tuple:
            - bool: True if all files exist, False otherwise
            - List[str]: List of file paths that do not exist
    """
    if input_file_path is None or not input_file_path:
        return True, []

    missing_files = [path for path in input_file_path if not os.path.exists(path)]
    return len(missing_files) == 0, missing_files

def check_or_create_output_dir(output_dir: str) -> bool:
    """
    Check if the output directory exists. If not, try to create it.
    
    Returns True if the directory exists or is successfully created, False otherwise.
    """
    try:
        if not os.path.exists(output_dir):
            os.makedirs(output_dir, exist_ok=True)
            print(f"Output_dir: {output_dir} is created correctly.")
        return os.path.isdir(output_dir)
    except Exception as e:
        print(f"❌ Failed to create or access output directory: {e}")
        return False


# ============= CODE GENERATION UTILITIES (ADDED) =============

from pydantic import BaseModel, Field
from bs4 import BeautifulSoup as Soup
from langchain_community.document_loaders.recursive_url_loader import RecursiveUrlLoader


# ============= CODE GENERATION DATA MODELS =============

class GeneratedCode(BaseModel):
    """Schema for generated code output"""
    description: str = Field(description="Description of the problem and approach")
    language: str = Field(description="Programming language used")
    imports: str = Field(description="Import/library statements")
    code: str = Field(description="Main code implementation")

    @property
    def full_code(self) -> str:
        """Return complete code with imports"""
        if self.imports:
            return f"{self.imports}\n\n{self.code}"
        return self.code


class FunctionNameOutput(BaseModel):
    """Schema for function name extraction"""
    function_name: str


# ============= CODE GENERATION CONSTANTS =============

# Language extensions mapping
LANGUAGE_EXTENSIONS = {
    "python": ".py",
    "r": ".R",
    "bash": ".sh",
    "shell": ".sh",
    "sh": ".sh",
    "javascript": ".js",
    "java": ".java",
    "cpp": ".cpp",
    "c": ".c",
    "go": ".go",
    "rust": ".rs",
    "typescript": ".ts",
}


# ============= CODE GENERATION HELPER CLASSES =============

class CodeGeneratorHelper:
    """Helper class for code generation with RAG support"""

    def __init__(self, use_rag: bool = False, rag_url: Optional[str] = None, rag_context: Optional[str] = None):
        self.use_rag = use_rag
        self.rag_context = ""

        if use_rag:
            if rag_context:
                self.rag_context = rag_context
            elif rag_url:
                self.rag_context = self._load_rag_context(rag_url)
            else:
                # Default Python docs for RAG
                self.rag_context = self._load_rag_context(
                    "https://python.langchain.com/docs/concepts/"
                )

    def _load_rag_context(self, url: str) -> str:
        """Load RAG context from URL"""
        try:
            print(f"Loading RAG context from: {url}")
            loader = RecursiveUrlLoader(
                url=url,
                max_depth=10,
                extractor=lambda x: Soup(x, "html.parser").text
            )
            docs = loader.load()

            # Sort and concatenate
            sorted_docs = sorted(docs, key=lambda x: x.metadata.get("source", ""))
            content = "\n\n---\n\n".join([doc.page_content for doc in sorted_docs])

            print(f"Loaded {len(content)} characters of context")
            return content[:5000]  # Limit context size

        except Exception as e:
            print(f"Warning: Could not load RAG context: {e}")
            return ""

    def get_prompt_for_language(self, language: str, prompts_module) -> str:
        """
        Get the appropriate prompt template for the language.
        
        Args:
            language: Programming language
            prompts_module: The prompts module containing prompt templates
        """
        language_lower = language.lower()

        if language_lower == "python":
            return prompts_module.AUTO_CODE_GENERATOR_PROMPT
        elif language_lower == "r":
            return prompts_module.R_PROMPT_TEMPLATE
        elif language_lower in ["bash", "shell", "sh"]:
            return prompts_module.BASH_PROMPT_TEMPLATE
        else:
            return prompts_module.GENERAL_PROMPT_TEMPLATE


# Global instance for potential RAG support
code_gen_helper = CodeGeneratorHelper(use_rag=False)