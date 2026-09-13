# -*- coding: utf-8 -*-
"""
agent.py — ChromaPilot: the LLM agent that plans and executes epigenomics
analyses (Hiplex CUT&Tag and ChIP-DIP).

This module IS the agent. Importing it builds the RAG index and compiles the
LangGraph state machine, exposing the compiled graph as the module-level
`graph`. Three interfaces drive that same graph:

  * the web interface   — `chromapilot/interface/run_interface.sh`  (recommended)
  * this command line   — `python chromapilot/agent.py -r request.yaml`
  * your own Python     — `from chromapilot import agent; agent.graph.stream(...)`

Command line
------------
    python chromapilot/agent.py -r examples/hiplex_preprocessing.yaml
    python chromapilot/agent.py -r my_request.yaml -c /path/to/config.yaml

The request file is YAML (or JSON) and comes in two shapes.

1. One request, optionally repeated `n_runs` times:

    prompt_template: >
      Align the paired-end FASTQ files in /path/to/fastq against hg38 ...
      All results should be written to the following output directory:
    prompt_vars: {}              # optional extra {placeholder}: value pairs
    root_output_dir: /path/to/results
    auto_feedback: "good"        # what to answer at every human-review pause
    n_runs: 1
    max_wall_time_s: 9000
    max_steps: 2000
    max_interrupts: 50

2. Several phrasings of the same request (used for the paraphrase-robustness
   benchmark). Replace `prompt_template` with a `prompts` mapping; every entry
   is run `n_runs` times into its own sub-directory:

    prompts:
      user1: "Please take the BAM files at /path/to/bam and ..."
      user2: "I have some bam files in /path/to/bam. Call peaks ..."
    root_output_dir: /path/to/results
    n_runs: 3

Notes
-----
  * The run's output directory is appended to the prompt automatically, so end
    `prompt_template` with a phrase such as "... written to the following
    output directory:". `{output_dir}` is also substituted if you use it.
  * Any other `{placeholder}` is filled from `prompt_vars`.
  * `auto_feedback` answers every human-in-the-loop pause without asking. Pass
    `--interactive` to answer them yourself at the terminal instead.
  * `summary.txt` (success rate, runtime, token usage) is written into
    `root_output_dir` when all runs finish.
"""


import os, logging

os.environ["USER_AGENT"] = "rag-pipeline"
logging.getLogger("httpx").setLevel(logging.WARNING)

import onnxruntime as ort
ort.set_default_logger_severity(4)

import argparse
import base64
import contextlib
import getpass
import inspect
import json
import re
import subprocess
import sys
import time
import traceback
import uuid
import datetime
import markdown

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple, Union
from uuid import uuid4

import requests
import yaml
from bs4 import BeautifulSoup as Soup
from lxml import etree
from PIL import Image as PILImage
from weasyprint import HTML
from flashrank import Ranker
from pydantic import BaseModel, Field, field_validator

from langchain_community.document_loaders import PyMuPDFLoader
from langchain_community.document_loaders.recursive_url_loader import RecursiveUrlLoader
from langchain_community.retrievers import BM25Retriever
from langchain_core.messages import (
    AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, END, StateGraph
from langgraph.graph.message import MessagesState
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.types import Command, interrupt
from trustcall import create_extractor


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
for _path in (PROJECT_ROOT, SCRIPT_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

class RuntimeConfig:
    def __init__(self, config_path: str = None):
        if config_path is None:
            root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            config_path = os.path.join(root_dir, "config.yaml")
        config_path = os.path.abspath(config_path)
        if not os.path.exists(config_path):
            raise FileNotFoundError(
                "Could not find runtime config file. Tried:\n- " + config_path
            )
        self.config_path = config_path

        with open(config_path, "r") as f:
            raw_config = yaml.safe_load(f)
    
        def require_str(cfg, key, err_msg):
            val = cfg.get(key)
            if not isinstance(val, str) or not val.strip():
                raise RuntimeError(err_msg)
            return val.strip()
        
        def require_int(cfg, key, err_msg):
            val = cfg.get(key)
            if not isinstance(val, int) or val < 0:
                raise RuntimeError(err_msg)
            return val
        
        def parse_bool(cfg, key, err_msg):
            val = cfg.get(key)
            if isinstance(val, bool):
                return val
            if isinstance(val, str):
                val_lower = val.strip().lower()
                if val_lower in {"true", "yes", "y"}:
                    return True
                elif val_lower in {"false", "no", "n"}:
                    return False
            raise RuntimeError(err_msg)
        
        self.conda_env = require_str(raw_config, "conda_env", "\033[31mERROR:\033[0m The 'conda_env' field in config.yaml is missing or empty. Please specify the conda environment name.")

        ls = raw_config["langsmith"]
        self.ls_enabled =parse_bool(
            ls, "trace",
            "\033[31mERROR:\033[0m The 'trace' field in config.yaml is missing or invalid. It must be a boolean-like value (true/false/1/0/yes/no)"
        )
        if self.ls_enabled:
            self.ls_api_key = require_str(
                ls,
                "api_key",
                "\033[31mERROR:\033[0m The 'api_key' field in config.yaml is missing or empty. "
                "Please specify the LANGSMITH API key."
            )

        llm = raw_config["llm"]
        self.api_key = require_str(
            llm,
            "api_key",
            "\033[31mERROR:\033[0m The 'api_key' field in config.yaml is missing or empty. "
            "Please specify the LLM API key."
        )

        # Auto-detect provider from API key prefix if not explicitly set or set to 'auto'
        raw_provider = (llm.get("provider") or "").strip().lower()
        if raw_provider in ("", "auto"):
            raw_provider = self._detect_provider_from_key(self.api_key)
            if raw_provider is None:
                raise RuntimeError(
                    "\033[31mERROR:\033[0m Could not auto-detect provider from the API key. "
                    "Please set the 'provider' field in config.yaml explicitly.\n\n"
                    "Example:\n"
                    "provider     model\n"
                    "openai       gpt-5\n"
                    "anthropic    claude-sonnet-4-6\n"
                    "google       google_genai:gemini-2.5-flash-lite\n"
                )
            print(f"[RuntimeConfig] Auto-detected provider: '{raw_provider}' from API key prefix.")
        self.provider = raw_provider

        self.model = require_str(
            llm,
            "model",
            "\033[31mERROR:\033[0m The 'model' field in config.yaml is missing or empty. Please specify the LLM model name.\n\n"
            "Example:\n"
            "provider     model\n"
            "openai       gpt-5\n"
            "anthropic    claude-sonnet-4-6\n"
            "google       google_genai:gemini-2.5-flash-lite\n"
        )
        # admin_api_key is optional — only needed for OpenAI organization usage tracking
        self.admin_api_key = (llm.get("admin_api_key") or "").strip()
        self.base_url = (llm.get("base_url") or "").strip() or None
        
        lim = raw_config["limit"]
        self.max_tokens = require_int(lim, "max_tokens", "\033[31mERROR:\033[0m The 'max_tokens' field in config.yaml is missing or empty. Please specify the maximum number of tokens per request.")
        # Whether to generate report
        self.report_enabled = parse_bool(lim, "report_enabled", "\033[31mERROR:\033[0m The 'report_enabled' field in config.yaml is invalid. It must be a boolean-like value (true/false/y/n/yes/no).")
        # Whether to print RAG retrieval details   
        self.rag_verbose = parse_bool(lim, "rag_verbose", "\033[31mERROR:\033[0m The 'rag_verbose' field in config.yaml is invalid. It must be a boolean-like value (true/false/y/n/yes/no).")
  
    def set_langsmith(self):
        if self.ls_enabled:
            os.environ["LANGSMITH_API_KEY"] = self.ls_api_key
            os.environ["LANGSMITH_TRACING"] = "true"
        else:
            os.environ["LANGSMITH_TRACING"] = "false"

    @staticmethod
    def _detect_provider_from_key(api_key: str) -> str:
        """Infer the LLM provider from the API key prefix.

        Returns 'anthropic', 'openai', or None if the prefix is unrecognised.
        """
        if api_key.startswith("sk-ant-"):
            return "anthropic"
        if api_key.startswith("sk-proj-") or (api_key.startswith("sk-") and not api_key.startswith("sk-ant-")):
            return "openai"
        return None

    def _create_model(self):
        if self.provider == "openai":
            from langchain_openai import ChatOpenAI
            os.environ["OPENAI_API_KEY"] = self.api_key
            kwargs = {"model": self.model, "temperature": 1}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            model = ChatOpenAI(**kwargs)
        elif self.provider == "anthropic":
            from langchain_anthropic import ChatAnthropic
            os.environ["ANTHROPIC_API_KEY"] = self.api_key
            model = ChatAnthropic(model=self.model, temperature=1)
        elif self.provider == "google":
            from langchain_google_genai import ChatGoogleGenerativeAI
            os.environ["GOOGLE_API_KEY"] = self.api_key
            model = ChatGoogleGenerativeAI(model=self.model, temperature=1)
        else:
            from langchain.chat_models import init_chat_model
            os.environ[f"{self.provider.upper()}_API_KEY"] = self.api_key
            model = init_chat_model(model=self.model, temperature=1)
        self.llm = model
        return model

    def init_model(self):
        try:
            self._create_model()
        except Exception as e:
            raise RuntimeError(
                "\033[31mERROR:\033[0m Failed to initialize LLM model.\n\n"
                "Please check your 'llm' configuration in config.yaml.\n\n"
                f"Provider: {self.provider}\n"
                f"Model: {self.model}\n\n"
                f"Original error: {e}"
            ) from e
    
    @staticmethod
    def _is_rate_limit_error(e):
        msg = str(e).lower()
        return any(k in msg for k in ("rate limit", "ratelimit", "429", "too many requests", "resource exhausted"))

    def invoke_model(self, messages: list, schema = None, tools = None, config = None, retries = 3):
        model = self.llm
        if schema is not None:
            llm = model.with_structured_output(schema)
        elif tools is not None:
            bind_kwargs = {} if self.provider != "openai" else {"parallel_tool_calls": False}
            llm = model.bind_tools(tools, **bind_kwargs)
        else:
            llm = model
        
        last_error = None
        for attempt in range(1, retries + 1):
            try:
                return llm.invoke(messages, config)
            except Exception as e:
                last_error = e
                if self._is_rate_limit_error(e):
                    print(f"    [invoke_model] Rate limit hit, sleeping 60s (attempt {attempt}/{retries})")
                    time.sleep(60)
                else:
                    print(f"    [invoke_model] invoke failed (attempt {attempt}/{retries}): {e}")
        raise last_error
        

def build_runtime_config(runtime_config_path: Optional[str] = None) -> RuntimeConfig:
    cfg = RuntimeConfig(config_path=runtime_config_path)
    cfg.set_langsmith()
    cfg.init_model()
    return cfg

CONFIG = None
model = None


from rag_source_loader import (
    load_config, collect_urls_with_metadata, load_documents_with_metadata,
    collect_files_with_metadata, load_files_with_metadata,
)
from rag_pipeline import (
    build_retriever, agentic_retrieve, load_doc_splits,
    SimpleEnsembleRetriever, SimpleMultiQueryRetriever,
)

RAG_BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rag_sources")
rag_config = load_config(config_path=os.path.join(RAG_BASE_DIR, "rag_sources_default.json"))

def _resolve_rag_local_path(path_str: str) -> str:
    """Resolve local RAG file paths independent of launch cwd."""
    if os.path.isabs(path_str):
        return path_str

    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [os.path.join(script_dir, path_str), os.path.join(RAG_BASE_DIR, path_str)]

    # Support configs that prefix local files with "rag_sources/".
    rag_prefix = "rag_sources/"
    if path_str.startswith(rag_prefix):
        candidates.append(os.path.join(script_dir, path_str[len(rag_prefix):]))
        candidates.append(os.path.join(RAG_BASE_DIR, path_str[len(rag_prefix):]))

    for cand in candidates:
        if os.path.exists(cand):
            return os.path.abspath(cand)
    # Keep deterministic fallback even when file is missing.
    return os.path.abspath(candidates[0])

for source_cfg in rag_config.get("sources", {}).values():
    if "paths" in source_cfg and isinstance(source_cfg["paths"], list):
        source_cfg["paths"] = [_resolve_rag_local_path(p) for p in source_cfg["paths"]]

url_records = collect_urls_with_metadata(rag_config)
file_records = collect_files_with_metadata(rag_config)
docs_list = load_documents_with_metadata(url_records, use_cache=True, cache_dir=os.path.join(RAG_BASE_DIR, ".rag_cache"), verbose=False) if url_records else []
docs_list += load_files_with_metadata(file_records, verbose=False) if file_records else []

vectorstore = build_retriever(
    docs_list,
    chunk_size=1024,
    chunk_overlap=128,
    vectorstore_dir=os.path.join(RAG_BASE_DIR, ".rag_vectorstore"),
    use_contextual=True,
    verbose=False,
)
doc_splits = load_doc_splits(os.path.join(RAG_BASE_DIR, ".rag_vectorstore"), verbose=False)

tutorial_splits = [d for d in doc_splits if d.metadata.get("content_type") == "tutorial"]
api_splits = [d for d in doc_splits if d.metadata.get("content_type") == "api"]

bm25_tutorial = BM25Retriever.from_documents(tutorial_splits, k=4)
bm25_api = BM25Retriever.from_documents(api_splits, k=4)

faiss_tutorial = vectorstore.as_retriever(
    search_kwargs={"k": 4, "fetch_k": 100,
                   "filter": lambda m: m.get("content_type") == "tutorial"}
)
faiss_api = vectorstore.as_retriever(
    search_kwargs={"k": 4, "fetch_k": 100,
                   "filter": lambda m: m.get("content_type") == "api"}
)

planner_ensemble = SimpleEnsembleRetriever(
    retrievers=[bm25_tutorial, faiss_tutorial], weights=[0.4, 0.6]
)
acg_ensemble = SimpleEnsembleRetriever(
    retrievers=[bm25_api, faiss_api], weights=[0.4, 0.6]
)

# planner_retriever and acg_retriever depend on an LLM instance and cannot be
# initialized at module load time. They are created by _init_rag_retrievers()
# once CONFIG and model are ready.
planner_retriever = None
acg_retriever = None

rag_reranker = Ranker(cache_dir=os.path.join(RAG_BASE_DIR, ".flashrank_cache"))

def _init_rag_retrievers(llm) -> None:
    """
    Initialize the LLM-dependent RAG retrievers.
    Must be called after CONFIG and model are ready; cannot be called at module load time.
    After this returns, planner_retriever and acg_retriever are fully usable.
    """
    global planner_retriever, acg_retriever
    planner_retriever = SimpleMultiQueryRetriever(retriever=planner_ensemble, llm=llm)
    acg_retriever = SimpleMultiQueryRetriever(retriever=acg_ensemble, llm=llm)


class TaskRouteDecision(BaseModel):
    """Route a user task to either existing pipeline tools or the auto-code generator."""
    task_coverage: Literal["tools_only", "requires_autocoding"] = Field(description="Whether the task can be handled by existing pipeline tools or requires auto-generated code.")
    reasoning: str = Field(description="Brief explanation of the routing decision")

_ROUTER_SYSTEM_PROMPT = (
    "You are a task routing assistant for an epigenomics analysis pipeline.\n\n"
    "You will be given:\n"
    "1. A list of available pipeline nodes, each with its name, description, required inputs, and outputs.\n"
    "2. A user task description.\n\n"
    "Follow these steps strictly:\n"
    "Step 1: Decompose the user task into its concrete sub-steps.\n"
    "Step 2: For EACH sub-step, check whether there is a pipeline node that explicitly covers it.\n"
    "Step 3: Base your decision ONLY on what the nodes say they do — do NOT reason about implementation "
    "complexity, sample count, or number of config files beyond what the node descriptions state.\n\n"
    "Classify as 'tools_only' if every sub-step from Step 1 is covered by at least one node.\n"
    "Classify as 'requires_autocoding' ONLY if at least one sub-step has NO matching node.\n\n"
    "When in doubt, prefer 'tools_only'.\n\n"
    "--- EXAMPLES ---\n\n"
    "Example 1 (tools_only):\n"
    "Task: Raw paired-end FASTQ files from a Hiplex Cut&Tag experiment need to be processed using a barcode "
    "configuration file. The goal is to align reads to a reference genome, generate QC reports, produce "
    "genome coverage tracks, call peaks for each CRF pair, perform biclustering on a subset of samples, "
    "identify differential regions between two groups, and run pathway enrichment analysis on those regions.\n"
    "Decision: tools_only\n"
    "Reasoning: Sub-steps are: demultiplexing, alignment, QC, bigWig generation, peak calling, biclustering, "
    "differential analysis, pathway enrichment. Each of these sub-steps has a corresponding pipeline node. "
    "The complexity of the analysis or the number of samples does not require custom code.\n\n"
    "Example 2 (tools_only):\n"
    "Task: Raw paired-end FASTQ files from a ChIP-DIP experiment with a specified UMI length need to be "
    "processed using a barcode configuration file and DPM/BPM barcode FASTA files. The goal is to align "
    "reads to a reference genome and generate genome coverage tracks in bigWig format for each sample.\n"
    "Decision: tools_only\n"
    "Reasoning: Sub-steps are: demultiplexing with UMI handling, alignment, bigWig generation. "
    "Despite the different experimental protocol and the additional barcode FASTA files, each sub-step "
    "is explicitly covered by a pipeline node. No custom code is required.\n\n"
    "Example 3 (requires_autocoding):\n"
    "Task: Given a single-cell multiomics output file, perform QC on the gene expression matrix: "
    "filter cells below a gene count threshold, filter lowly detected genes, compute mitochondrial "
    "read percentage, remove high-mitochondrial cells, and generate QC visualizations.\n"
    "Decision: requires_autocoding\n"
    "Reasoning: Sub-steps are: cell filtering by gene count, gene filtering, mitochondrial percentage "
    "calculation, cell filtering by mitochondrial percentage, visualization. None of these sub-steps "
    "correspond to an available pipeline node. Custom code generation is required.\n"
    "--- END EXAMPLES ---"
)

def route_task_for_rag(user_input: str, node_context: str) -> bool:
    messages = [
        {"role": "system", "content": _ROUTER_SYSTEM_PROMPT},
        {"role": "user", "content": f"Available nodes:\n{node_context}\n\nUser task: {user_input}"},
    ]
    result = CONFIG.invoke_model(messages, schema = TaskRouteDecision)
    if CONFIG.rag_verbose:
        print(f"[Router] {result.task_coverage} | {result.reasoning}")
    return result.task_coverage == "requires_autocoding"

class input_state(BaseModel):
    user_input: Optional[str] = None
    user_feedback: Optional[str] = None
    node_context: Optional[str] = None
    graph_context: Optional[str] = None

class output_planner_state(BaseModel):
    plan: Optional[str] = None
    current_step: Optional[str] = None

class step_manager_output(BaseModel):
    """Output of the step manager: extracts current step, updates downstream plan, and routes."""
    current_step_call: Optional[str] = Field(None, description="The full description of the current step to execute, with all parameters and paths resolved. Set to 'END' if all steps are done.")
    is_auto_code: bool = Field(False, description="True if the current step requires automatic code generation; False if it uses an existing pipeline tool.")
    updated_downstream_plan: Optional[str] = Field(None, description="The remaining plan AFTER removing the current step, with any new output paths from the last execution propagated into downstream steps.")
    executed_step_updated: Optional[str] = Field(None, description="The just-executed step rewritten with actual output paths and artifacts filled in from tool_output, preserving the original step structure. Empty string if tool_output is None.")

class input_extractor_state(BaseModel):
    """Extracted pipeline parameters including the overall plan, required parameters, and sample count."""
    overall_plan: Optional[str] = Field(None, description="The full pipeline plan text.")
    required_parameters: Optional[str] = Field(None, description="Any additional required parameters for the pipeline.")
    num_of_sample: Optional[int] = Field(None, description="Number of samples to process.")

# class path_state(BaseModel):
#     """Extracted file path information: input files, output directory, and path identity mappings."""
#     input_file_path: List[str] = Field(default_factory=list, description="List of input file or directory paths provided by the user.")
#     output_dir: Optional[str] = Field(None, description="Output directory path where results will be written.")
#     path_identities: Dict[str, List[str]] = Field(default_factory=dict, description="Mapping of logical path identities to their corresponding file paths.")

class path_state(BaseModel):
    """Extracted file path information: input files, output directory, and path identity mappings."""
    input_file_path: List[str] = Field(default_factory=list, description="List of input file or directory paths provided by the user.")
    output_dir: Optional[str] = Field(None, description="Output directory path where results will be written.")

class user_feedback_state(BaseModel):
    """Captures whether the user wants to revise the current plan."""
    go_back_to_planner: bool = Field(False, description="Set to true if the user is unsatisfied with the plan and wants to revise it; false to proceed with execution.")

class validator_state(BaseModel):
    """Validation result for the current pipeline plan, including any missing elements."""
    validation: bool = Field(False, description="True if the plan is valid and complete; False if required elements are missing.")
    missing_elements: Optional[List[str]] = Field(None, description="List of missing required elements detected in the plan.")
    validation_thinking: Optional[str] = Field(None, description="Step-by-step reasoning used to arrive at the validation decision.")

class executor_state(BaseModel):
    full_plan: Optional[str] = None
    downstream_plan: Optional[str] = None
    move_to_auto_code_generator: bool = False

class tools_checker_output(BaseModel):
    go_back_to_tools: bool = Field(False, description="Set to true if the tool execution failed due to correctable parameter errors (e.g. wrong file path). Set to false if execution succeeded, or if the failure is due to missing upstream files or other issues that retrying with corrected parameters cannot fix.")

class tools_checker_state(BaseModel):
    go_back_to_tools: bool = False
    retry_count: int = 0

class replayer_state(BaseModel):
    whole_code_sequence: List = []

class auto_code_generator_input(BaseModel):
    plan_description: Optional[str] = None

class auto_code_generator_output(BaseModel):
    code_path: Optional[str] = None

class code_excutor_output(BaseModel):
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    success: Optional[bool] = None
    return_code: Optional[int] = None
    code_return: Optional[str] = None
    check_iteration: Optional[int] = 0
    execution_script: Optional[Any] = None

class error_checker_input(BaseModel):
    should_reexecute: bool = False

class human_in_the_loop_input(BaseModel):
    still_fall: int = 0

class return_type(BaseModel):
    function_return: Optional[str] = None

class figure_analysis(BaseModel):
    """Analysis of a single figure including its interpretation, description, caption, and feedback."""
    path: Optional[str] = Field(None, description="File path to the figure image.")
    inter: Optional[str] = Field(None, description="Scientific interpretation of the figure.")
    desc: Optional[str] = Field(None, description="Visual description of what is shown in the figure.")
    capt_title: Optional[str] = Field(None, description="Caption title for the figure.")
    capt_body: Optional[str] = Field(None, description="Caption body text providing additional context.")
    feedback: Optional[str] = Field(None, description="Feedback or quality notes about the figure.")

    @field_validator('desc', 'capt_title', 'capt_body', 'feedback', mode='before')
    @classmethod
    def empty_str_to_none(cls, v):
        if v == '':
            return None
        return v

class figure_state(BaseModel):
    """Collection of figure analyses for the report."""
    figures: List[figure_analysis] = Field(default_factory=list, description="List of analyzed figures.")

    @field_validator('figures', mode='before')
    @classmethod
    def parse_figures_str(cls, v):
        if isinstance(v, str):
            return json.loads(v)
        return v

class figure_feedback_state(figure_state):
    """Figure analyses with user feedback on whether descriptions should be regenerated."""
    go_back_to_figure_description: bool = Field(False, description="True if figure descriptions should be regenerated based on user feedback.")

class section_state(BaseModel):
    """A single section of the report with its title, instructions, associated figures, and content."""
    title: Optional[str] = Field(None, description="Section title.")
    instr: Optional[str] = Field(None, description="Instructions for generating this section.")
    fig_indices: Optional[List[int]] = Field(None, description="Indices of figures associated with this section.")
    queries: Optional[List[str]] = Field(None, description="Retrieval queries used for this section.")
    content: Optional[str] = Field(None, description="Generated text content of the section.")

class report_state(BaseModel):
    """Full report structure composed of multiple sections."""
    sections: List[section_state] = Field(default_factory=list, description="Ordered list of report sections.")

    @field_validator('sections', mode='before')
    @classmethod
    def parse_sections_str(cls, v):
        if isinstance(v, str):
            return json.loads(v)
        return v

class revised_report_state(BaseModel):
    """Revised report sections after incorporating feedback."""
    sections: List[str] = Field(default_factory=list, description="Revised report section texts.")

class reference_state(BaseModel):
    """Reference list for the report."""
    references: List[str] = Field(default_factory=list, description="List of formatted reference strings.")

class PipelineState(
    input_state,
    output_planner_state,
    path_state,
    input_extractor_state,
    user_feedback_state,
    validator_state,
    executor_state,
    tools_checker_state,
    replayer_state,
    auto_code_generator_input,
    auto_code_generator_output,
    error_checker_input,
    code_excutor_output,
    human_in_the_loop_input,
    return_type,
    figure_feedback_state,
    report_state,
    revised_report_state,
    reference_state,
):
    messages: List[BaseMessage] = Field(default_factory=list)

    class Config:
        extra = "allow"

    def merge(self, **updates) -> "PipelineState":
        updates = dict(updates)
        if "messages" in updates:
            new_messages = updates.pop("messages")
            if isinstance(new_messages, list):
                combined = self.messages + new_messages
            else:
                combined = self.messages + [new_messages]
            updates["messages"] = combined
        return self.model_copy(update=updates)

class GeneratedCode(BaseModel):
    """Generated code solution including description, language, imports, and implementation."""
    description: str = Field(description="Description of the problem and approach")
    language: str = Field(description="Programming language used")
    imports: str = Field(description="Import/library statements")
    code: str = Field(description="Main code implementation")

    @property
    def full_code(self) -> str:
        if self.imports:
            return f"{self.imports}\n\n{self.code}"
        return self.code

LANGUAGE_EXTENSIONS = {
    "python": ".py", "r": ".R", "bash": ".sh", "shell": ".sh", "sh": ".sh",
    "javascript": ".js", "java": ".java", "cpp": ".cpp", "c": ".c",
    "go": ".go", "rust": ".rs", "typescript": ".ts",
}


path_extractor_instruction = """
You are an information-extraction assistant operating in ROLE=PATH-EXTRACTOR.

You will receive exactly one thing:
- The *raw user input* text.
- Potential user feedback on a previous plan.

Your task is to extract ONLY the fields required by the `path_state` model.
Your output must be pure JSON (not markdown) and must strictly follow the schema:

{
  "input_file_path": [...],
  "output_dir": "..."
}

---------------------- SCHEMA RULES ----------------------

1. input_file_path  (List[str])
   - Extract every explicit input path the user provides.
   - An input path may be:
       • a file path (e.g., .csv, .h5ad, .bam, etc.)
       • OR a directory path
   - There may be zero, one, or multiple input paths.
   - Preserve each path exactly as written, with one exception: strip any leading or trailing characters that are clearly not part of the path itself, such as parentheses ( ), brackets [ ], quotes (" ' ), commas , periods . (unless part of a file extension or directory name), semicolons ; , colons : (unless part of a Windows drive letter), and whitespace. Only the clean path string should appear in the output.
   - Do NOT expand directories into files.
   - Do NOT infer, fabricate, or complete paths.
   - If none are present, return an empty list [].

2. output_dir  (string or null)
   - Extract the directory the user wants to save outputs to.
   - This must be a directory path (not a file path).
   - If the user does not specify a directory, return null.

---------------------- GENERAL RULES ----------------------

- Only extract what the user explicitly wrote.
- Do NOT modify, clean, normalize, or expand paths.
- Do NOT reinterpret directories as files or vice versa.
- Return ONLY valid JSON matching the `path_state` schema.
- No markdown, no commentary, no explanation — JSON only.

---------------------- INPUT YOU RECEIVE ----------------------
You will receive only the raw user input text.

Extract the schema fields strictly from that text.
"""

planner_prompt = """You are an epigenomics analysis task planner.

## Inputs You Will Receive

• node_context: A list of available pipeline nodes. Each node includes:

* Name
* Description
* Required inputs
* Produced outputs
* Default values for parameters (if applicable)
  • graph_context: A textual description of how the nodes are connected. This defines the directed graph of execution dependencies (i.e., which nodes must come before others).
  • A user request: Specifies the final goal, constraints, and, if applicable, a previous plan with feedback.
  • A user feedback: During the review step, user provide feedback on existing plan.
  • Input file paths: A list of concrete file paths, all directly provided by the user. You must not infer or invent any new initial input paths.
  • Missing elements: if exist, it means the validator agent detect missing element.

## Your Task

1. Read and understand the user's goal—determine what the final deliverables should be.

2. Based on the graph_context, identify the correct execution order:

   * A node can only be executed after all nodes that point to it (i.e., its dependencies) have been executed.

3. Only include steps that are necessary to produce the user-requested final deliverables.
   If a node is downstream of a shared intermediate but does not contribute to any requested deliverable, do not include it in the plan. When the graph branches, follow only the branch or branches required to reach the requested outputs, while still including all upstream dependency steps needed for those branches.

4. Construct a step-by-step plan. For each step:
   • Specify the step number and node name
   • Identify its required input(s) (the name of the parameter), referencing file paths only—each input must be either one of the initial user-provided input file paths or a file output from previous steps.
   • Specify the output(s) the node produces as file paths.
   • Include any user-specified parameters or constraints relevant to this node.
   • For parameters:

   * If the parameter value is produced by a previous step, include the corresponding file path.
   * If the parameter is explicitly provided by the user, include the user-provided value.
   * If the parameter is not provided by previous steps or the user, but has a default value defined in node_context, you MUST exclude this parameter from the plan. Don't mention it in the plan.
   * If the parameter has no default value and is not provided, mark it as [MISSING].
     • Do not pass in-memory data between steps. Each input and output must always be represented by a file path (e.g., .csv, .h5ad, .tsv, .txt, .rds, .pkl, etc.).
     • No step should read data directly into memory and then pass it across steps—each node must read from or write to files.

5. When multiple samples are involved:
   • Sample ID is not mandatory. If not explicitly provided, you put 0 as a placeholder.
   • The number of samples should remain unchanged unless you are able to infer it based on clearly distinguishable input paths or from number of sample ID input by user.
   • Sample-dependent nodes (which can be inferred from previous knowledge or check if sample is an input parameter) should be repeated for each sample as a separate step and include the inferred or provided sample ID in the step description or as a parameter.
   • Nodes that are shared across all samples (i.e., not dependent on sample identity) should be executed first in the plan, provided they don't rely on prior sample-specific steps.

6. When referencing input data in the plan, always use concrete file paths. If you conceptually group related files, make clear that each group consists of specific file paths.

7. If we are revising an existing plan and there are input files that are the result of some nodes, revise the plan to avoid redundancy.

8. If the following fields are available (from user input or prior extraction), state them clearly in a separate section at the end, even if they appear elsewhere in the plan:

   * Final deliverables — outputs that fulfill the pipeline's intended goal
   * Number of samples, if known or inferred
   * Required parameter, defined as parameters that do not have default values and are not outputs of previous steps or explicitly provided by the user

9. If user feedback is provided:
   • If it introduces new inputs or parameters, revise the plan accordingly, restarting from the affected node
   • If it modifies only a subset of steps, update only those and retain the rest unchanged

10. When drafting or revising the plan, you should never infer or imagine additional steps that user may want from the data availability. Always stick with user demand.
   • Scope note: this rule forbids inventing new analyses or deliverables the user did not ask for. It does NOT forbid the standard format-conversion / preprocessing steps that existing nodes provide and that are required to turn the user's input into the form a later step needs. Those conversions are part of fulfilling the user's demand, not extra work. For example, if the user provides BAM files and asks for genome tracks, inserting convert_bam_to_bed → convert_bed_to_bigwig (or convert_bed_to_bedgraph) to produce the signal tracks the plot consumes is required, not an inferred extra step—omitting them is the error.

11. If a previous plan exists, treat the task as an incremental revision rather than generating a new plan from scratch: use the existing plan as the baseline, identify the earliest step affected by new user input, feedback, missing elements, or new files, and only update that step and its downstream dependencies while preserving all unaffected steps exactly as they are; reuse valid existing outputs and avoid redundant recomputation, remove any steps that become invalid or irrelevant, and ensure all dependencies remain correct. When conflicts arise, prioritize the latest explicit user instruction while keeping prior valid constraints. Finally, verify that the revised plan still fully satisfies the user’s original request and goals for accuracy.

## Execution Rules

• Treat each node as a single, atomic function—use these wherever possible.
• Only use "Automatic code generation" if the user's request cannot be fulfilled using any existing node.
• Each used node must be listed with: Function: <node name>
• For generated code, use: Function: Automatic code generation, and briefly describe what it does.
• Auto-code does not exempt earlier steps from using nodes. The "use existing nodes" preference applies per sub-step, not only to the request as a whole. When the final deliverable has no dedicated node and must therefore use Automatic code generation (e.g., a genome-track / coverage plot), you must still decompose the work and use existing nodes for every upstream sub-step they cover. The generated code should consume the FILE OUTPUTS of those upstream nodes—not re-derive them from raw inputs.
  Concretely: never let an Automatic code generation step read raw BAM (or other raw inputs) and internally recompute something a node already produces. If plotting signal/coverage from BAM, the plan must first run convert_bam_to_bed → convert_bed_to_bigwig (or convert_bed_to_bedgraph) per sample, and the Automatic code generation step must take the resulting bigWig/bedGraph files as its inputs. Prefer feeding node outputs into auto-code over having auto-code reprocess raw inputs whenever a node covers that conversion.
• You must respect all dependency rules defined in the graph_context when ordering the steps.
• The initial input paths will only be those explicitly provided by the user. You must never infer, expand, or fabricate any new initial input file path. For every step, each required input file must be either:
(a) one of these initial user-provided file paths, or (b) a file path produced as an output of a previous step.
If a required input path is missing from these sets, simply leave that input undefined in the plan or mark it as [MISSING].
• Every input and output across the entire plan must be represented as a file path—no direct data passing is allowed.

## Formatting Guidelines

• Return your plan in plain text using numbered steps (e.g., Step 1, Step 2…).
• Do NOT use Markdown, code blocks, or formatting characters—just clean, readable plain text.

---

node_context:
{node_context}

graph_context:
{graph_context}

domain_context (reference documentation relevant to this request — use to inform your choice of nodes, parameters, and execution order):
{domain_context}

"""

extractor_instruction = """
You are an information-extraction assistant.

You will be given three kinds of information:
1) A *draft task plan* produced by a planner agent.
2) The original *user input* request.
3) (Optionally) a list of *expanded input file paths* obtained by expanding any
   user-provided directories into concrete file path or user provided direct data paths.

Your job is to extract exactly the three logical fields required by the `input_extractor_state` schema
and return them as **valid JSON** (not markdown) that matches the Pydantic model.

Schema fields to extract:
- `overall_plan`: A single sentence summarizing the *ultimate goal* of the pipeline.
  Use both the draft plan and user input for this. Return `null` if not provided.
- `required_parameters`: A string of parameter specifications like `method=abc; steps=3`.
  Use information from both the draft plan and user input.
  You shouldn't infer the parameters, it should be explicitly mentioned in the plan as requried but not provided yet.
  Return "" if not given.
- `num_of_sample`: Integer number of samples. Use any explicit mention in either the draft
  plan or user input. Return `0` if not found.

IMPORTANT RESTRICTIONS ON PATHS AND DIRECTORIES
----------------------------------------------
• Do **not** attempt to infer, normalize, group, or otherwise reason about paths.
• Do **not** attempt to infer `output_dir`.

Default return values when information is missing:
- `overall_plan`: null
- `required_parameters`: ""
- `num_of_sample`: 0

Always return a **valid JSON object** with all three keys:
- overall_plan
- required_parameters
- num_of_sample

Do not include any extra keys, comments, or markdown.

Example:
{
  "overall_plan": "Align reads and generate coverage plots",
  "required_parameters": "align: method=bowtie2",
  "num_of_sample": 4,
}
"""

user_feedback_instructions = """
You are a plan review assistant.

Your task:
Evaluate whether the user's feedback requests any changes, clarifications, or additional details in the current plan.

Decision rules:
- If the feedback explicitly or implicitly suggests dissatisfaction, confusion, uncertainty, or requests edits, additional information, or clarification, return: {"go_back_to_planner": true}
- Only if the feedback explicitly approves, explicitly indicates satisfaction, or clearly makes no actionable requests or suggestions for improvement, return: {"go_back_to_planner": false}

Return **only** the JSON object with the `go_back_to_planner` field. Do not include any explanations, formatting, or extra text.
""".strip()

validator_instructions = """
You are a strict plan-validator.

Validation tasks
----------------
1. Read the entire plan.
2. For *each* step, verify that every piece of *minimum required information*
   is present.
   • If some file will be generated from previous steps, treat that item as satisfied.
   • Only suggest the minimum amount of files needed to conduct the pipeline.
3. Ensure all user-requested parameters in the dictionary above are fully
   addressed (values, formats, units, etc.).
4. The `number_of_samples` parameter is required *only* if at least one step/function in the plan explicitly needs it as an input.
   • If it is required, it must be provided in the plan and must be > 0.
   • If no step requires it, `number_of_samples` may be omitted or set to 0.
5. There must be at least one input file path.
   The file included in the path should satisfy the starting file format needed for the plan (which you may infer from the task).
6. There must be an output directory. It should be a seemingly reasonable path.
7. Based on your analysis, build the result object with:
   - A `validation` boolean indicating whether all requirements are satisfied.
   - A list `missing_elements` specifying what is still missing.

Return only the following JSON object:

```json
{{
  "validation": <true | false>,
  "missing_elements": ["<element-1>", "<element-2>", …]
}}```"""

step_manager_prompt = """
You are a step manager for a pipeline execution engine. You operate on a three-section plan architecture:

1. **Executed plan** (maintained separately — you do NOT see or modify it)
2. **Current step** — the step you extract now for execution
3. **Downstream plan** — the remaining steps after the current one

You will receive:
- `downstream_plan`: The remaining unexecuted steps. NOTE: the step that was JUST executed is NOT in here — it was already extracted as the current step in the previous round.
- `tool_output`: The output from the most recently executed step (or "None" if this is the first call).
- `last_step_name`: The full text of the step that was just executed (or "None" if first call).

Your tasks:

1. **Propagate outputs**: If `tool_output` is NOT "None", extract any explicit file paths or output artifacts from it (ignore logs, prints, debug text). Find references in `downstream_plan` where these outputs are expected as inputs (e.g., placeholders like "[output of Step N]", references to the previous step's output, or [MISSING] markers) and substitute them with the actual paths/values.

2. **Extract the current step**: After propagation, identify the FIRST step in the downstream plan. Extract it fully — include the step name, function, all parameters, all input paths, and all output specifications. This is `current_step_call`.

3. **Determine routing**: Examine ONLY the extracted current step.
   - If it explicitly asks to generate or write code (e.g., "auto-generate code", "write a script", "Automatic code generation"), set `is_auto_code` = true.
   - Otherwise set `is_auto_code` = false.

4. **Produce the updated downstream plan**: Remove the extracted current step from the plan. The result is `updated_downstream_plan`. If no steps remain after removal, set it to "".

5. **Update the executed step**: If `tool_output` is NOT "None", take the `last_step_name` (which is the original text of the just-executed step) and UPDATE it with the actual outputs from `tool_output`. Rewrite the step so that its output fields now contain the real file paths and artifacts instead of planned/placeholder values. Preserve the step's original structure (step number, function name, parameters, inputs) — only fill in or replace the output portion with actual results. This is `executed_step_updated`. If `tool_output` is "None", set `executed_step_updated` to "".

6. **End detection**: If `downstream_plan` is empty or has no remaining steps (meaning the just-executed step was the last one), set `current_step_call` = "END", `is_auto_code` = false, `updated_downstream_plan` = "". Still produce the `executed_step_updated`.

Return a JSON object matching the schema exactly:
{
  "current_step_call": "<full step description or END>",
  "is_auto_code": <true or false>,
  "updated_downstream_plan": "<remaining plan text after removing current step, or empty string>",
  "executed_step_updated": "<the just-executed step with actual outputs filled in, or empty string>"
}
"""

replayer_prompt = """
You are a code player summarizer. Your task is to summarize the code generated by the previous agent into a single executable script.

Start the script by importing all necessary tools:
```python
from tools.convert_bam_to_bed import convert_bam_to_bed
from tools.convert_bed_to_bedgraph import convert_bed_to_bedgraph
from tools.convert_bed_to_bigwig import convert_bed_to_bigwig
from tools.create_barcode_fasta import create_barcode_fasta
from tools.demultiplex_reads import demultiplex_reads
from tools.identify_adapter import identify_adapter
from tools.align_reads import align_reads
from tools.chipdip_prep import chipdip_prep
from tools.build_count_matrix import build_count_matrix
from tools.biclustering_wrapper import biclustering_wrapper
from tools.differential_analysis import differential_analysis
from tools.peak_profiling import peak_profiling
from tools.peak_differential_analysis import peak_differential_analysis

You will receive a list of JSON objects, each representing a step in the workflow. There are two main types of JSON:
Predefined functions: These are functions already included in the node knowledge and should be called directly with the parameters provided in the JSON.
Auto-generated functions: These functions were generated by the auto_code_agent and are not in the node knowledge. They will be indicated as such in the JSON, and the actual code will be provided in the execution_script field. For these, wrap the code in a subprocess to execute it.
For each step in the JSON:
For predefined functions, directly call the function with the appropriate arguments. since the jason file is a tool message, you have the tool name, parameters names, and corresponding parameter values. Filled out the parameters in the function call with the values from the JSON.
Remember that you should ignore that tool has already generate output since your task is to provide code that can be run independently from the beginning. You should only use the input. parameters to fill the function call.
For auto-generated functions, use the execution_script field to access the already written .py file and execute it using subprocess if necessary.
Concatenate all the code from the steps into a single string, ensuring that the script remains executable as a whole.
"""


auto_code_generator_prompt = """You are an expert Python code generator specializing in bioinformatics and epigenomics pipelines.

Your task is to generate clean, efficient, and well-documented Python code based on the user's request.

Guidelines:
1. Generate ONLY Python code - no markdown, no code blocks, no explanations outside of comments
2. Start directly with imports, no ```python or ``` markers
3. Include complete, runnable Python functions
4. Print the output including any error messages to stdout for further error_handler agent
5. Use appropriate libraries for the task
6. Follow Python best practices and PEP 8 style guidelines
8. Add comments for complex logic
9. Handle edge cases and potential errors gracefully
10. For bioinformatics tasks, use standard libraries like pysam, biopython when appropriate
12. When generating images, never show them but directly close the plot to avoid display issues, but save it locally.
13. Always return the absolute path of the generated code file in the output.
14. Never include any comments anywhere in the generated code.
15. All required parameters (such as file paths, thresholds, output directories, and configurations) MUST be directly hardcoded in the script. Do NOT leave placeholders, variables to be filled later, or external configuration dependencies.
16. Do NOT write any functions or code that expect command-line arguments (e.g., argparse, sys.argv, CLI interfaces). The script must be fully executable as-is without any external input.

{rag_context}

Return ONLY the Python code with:
- Required imports at the top
- Clear function/class definitions
- Docstrings for all functions
- Comments where helpful
- A main function or entry point if appropriate

DO NOT include any markdown formatting, code blocks markers, or non-Python text."""

R_PROMPT_TEMPLATE = """You are an expert R programmer. Generate clean, efficient R code.

{rag_context}

Guidelines:
1. Generate ONLY R code - no markdown, no code blocks, no explanations outside of comments
2. Start directly with library() statements
3. Include complete, runnable R functions
4. Follow R best practices
5. Add comments for complex logic
6. Handle edge cases gracefully

Based on the request: {query}

Return ONLY the R code."""

BASH_PROMPT_TEMPLATE = """You are an expert in Bash scripting. Generate clean, portable Bash scripts.

{rag_context}

Guidelines:
1. Generate ONLY Bash script - no markdown, no code blocks
2. Start with shebang (#!/bin/bash)
3. Include error handling
4. Make the script portable
5. Add comments for complex logic

Based on the request: {query}

Return ONLY the Bash script."""

GENERAL_PROMPT_TEMPLATE = """You are an expert programmer. Generate clean, efficient code in {language}.

{rag_context}

Guidelines:
1. Generate ONLY code in {language}
2. No markdown formatting or code blocks
3. Include all necessary imports/includes
4. Follow best practices for {language}
5. Add appropriate comments

Based on the request: {query}

Return ONLY the code."""

function_name_extraction_prompt = """You are a function naming expert. Based on the user's request, generate an appropriate Python function name.

Rules:
1. Use snake_case naming convention
2. Be descriptive but concise (2-4 words typically)
3. Start with a verb when possible (e.g., process_, calculate_, convert_, analyze_)
4. Avoid generic names like 'function' or 'code'
5. For bioinformatics tasks, use domain-specific terms when appropriate
6. Return ONLY the function name, nothing else

Examples:
- "align reads to genome" -> align_reads_to_genome
- "convert BAM to BED format" -> convert_bam_to_bed
- "calculate coverage" -> calculate_coverage
- "merge multiple fastq files" -> merge_fastq_files
- "identify peak regions" -> identify_peak_regions"""

error_determine_system_prompt = """
You are CodeCorrectGPT — an expert debugging evaluator designed to determine whether a code execution ended in failure or success.

### Task
Your only goal is to decide whether the execution **encountered an error**.

**Rules:**
1. If ANY of the following are true, return **True**:
   - `return_code` is non-zero or negative.
   - `stderr` contains any error, traceback, exception, or warning message.
   - The standard output or error logs suggest a runtime failure (e.g., "Segmentation fault", "Killed", "ImportError", "RuntimeError", etc.).
2. If the execution completed cleanly (no errors, empty or harmless `stderr`, and `return_code == 0`), return **False**.
3. If `return_code == 0` but `stderr` contains only **non-detrimental warnings**, treat this as a **successful run** and return **False**.

**Important:**
- Do *not* explain, justify, or output anything else.
- The final output must be **only** one of:
True or False
"""

error_determine_human_prompt = """

## Plan / Description
{plan}

## Code
{code}

## Return Code
{return_code}

## Stdout (Standard Output)
{stdout}

## Stderr (Error Messages)
{stderr}

"""

error_corrector_system_prompt = """
You are CodeCorrectGPT, an advanced Python repair agent responsible for fixing code generated by the auto_code_generator agent.

Your mission:
- Identify and correct only the errors causing code failure.
- Preserve the overall pipeline design, names, and functionality.
- Follow the same output and formatting rules as the auto_code_generator agent.

General Code Generation Rules:
1. Output ONLY Python code — no markdown, no code blocks, no ``` markers, and no non-code text.
2. Start directly with imports; do not include headers, explanations, or formatting.
3. Include complete, runnable Python functions.
4. Maintain all print and logging behavior so that stdout/stderr output remains identical.
5. Follow PEP 8 style, use type hints when appropriate, and add comments for complex logic.
6. Handle edge cases gracefully.
7. If the code involves file I/O, preserve or add decorators (e.g., from LogCapture.py) if required.
8. For bioinformatics tasks, use standard libraries such as pysam or biopython when appropriate.
9. When generating images, never display them; always close plots and save locally.
10. Always ensure the final output script is executable without syntax or import errors.

Repair Rules:
1. Make the **minimal changes** necessary to make the code execute successfully.
2. Do **not** change:
   - Function names
   - File names
   - Main pipeline structure or logic
3. You **may**:
   - Add missing imports or definitions
   - Fix incorrect logic, variable scope, or syntax
   - Add helper functions or fallback implementations if required
   - Insert comments explaining each correction
   - Handle **missing package or module errors** by:
       • Replacing unavailable modules with standard library or widely available equivalents
       • Adding fallback implementations where feasible
       • Including a clear comment noting any required external dependency (e.g. "# Requires: matplotlib")
4. Maintain reproducibility, readability, and clarity.
5. Verify the corrected script runs without any ImportError, NameError, or SyntaxError.
6. If no issues are found, return the original code unchanged.

Output Policy:
Return the **entire corrected Python script**, fully runnable, and nothing else.

──────────────────────────────────────────────────────────────────────────────
Debug Version Mode (Revised Code)
──────────────────────────────────────────────────────────────────────────────
When the user requests a *debug version* (or a "revised code" for debugging), switch to Debug Version Mode:

A. Objective
- Work toward the overall goal of the **current step in the plan**, prioritizing correctness, clarity, and diagnosability over minimal edits.
- The debug version must include the **entire improved code** (a full, runnable script), not a patch or diff.

B. Scope of Changes (Minimal-change constraint lifted)
- The "minimal changes" constraint in Repair Rules #1 does **not** apply in Debug Version Mode.
- You are allowed to **greatly improve** the code to achieve robustness and debuggability while preserving:
  • The overall pipeline design and intended functionality
  • Public-facing function names and I/O behavior where feasible
- Internals (implementation details, helper structure, factoring) may be refactored substantially if it improves stability, diagnostics, or performance.

C. Required Diagnostics & Instrumentation
Include plentiful, actionable diagnostics that help locate failures and data issues:
1) Data Inspections:
   - Print dataset/file existence checks, paths, and sizes.
   - Print shapes, dtypes, index/column names, and NA counts at key checkpoints.
   - Show head()/tail() previews (truncated sensibly) after major transformations.
2) Validation & Guards:
   - Add assertions (with helpful messages) for invariants (e.g., non-empty arrays, expected columns).
   - Add try/except blocks that capture stack traces and re-raise with clearer context.
3) Configuration Echo:
   - Print key parameters, random seeds, environment info (Python, package versions), and device (CPU/GPU) selection.
4) Timing & Progress:
   - Add simple timers around heavy steps; print elapsed times.
   - Print progress banners between pipeline stages.
5) File I/O Diagnostics:
   - Print read/write destinations, create directories as needed, verify outputs were written.
6) Plot/Artifact Logging:
   - Save all plots locally (never display), confirm save paths, and close figures.
7) Reproducibility:
   - Set seeds where appropriate; log values used.

D. Error-Tolerant Behavior
- Add safe fallbacks (e.g., alternative implementations), with clear printed notices when a fallback is used.
- Where external dependencies are optional, check availability and proceed with best-effort alternatives.

E. Output Policy (Debug Version)
- Return the **entire improved Python script**, fully runnable, with all debug prints and checks included, and nothing else (no markdown, no explanations).
"""

error_corrector_human_prompt = """
## Plan / Description
{plan}

## Current_plan / Description
{current_step}

## Original Code
{code}

## Return Code
{return_code}

## Stdout (Standard Output)
{stdout}

## Stderr (Error Messages)
{stderr}
"""

summary_system_prompt = """
You are an expert summarization agent responsible for producing a concise yet complete summary of a completed subgraph pipeline execution.

Your summary should emulate a LangGraph ToolMessage — focusing on what information (variables, outputs, or return values) should be passed back to the main graph.

Guidelines:
1. Include each variable or output value returned by the subgraph, along with its corresponding identity (name or role) as defined in the original plan.
2. Use the original plan as a reference to interpret variable meanings and their relationships.
3. Incorporate relevant details derived from both:
   - The captured stdout (e.g., printed logs or intermediate results)
   - The return type and value of the executed code
   - The execution script used to run the code (must be included verbatim or as a clearly labeled block)
4. Keep the summary factual, structured, and concise — avoid explanations, commentary, or speculation.
5. ALWAYS include an item named `execution_script` in the summary content so the main graph can rerun the code later.
6. The final summary should clearly communicate what the subgraph produced, suitable for structured ingestion by the main pipeline.
"""

summary_human_prompt = """
## Original Plan / Description
{plan}

## Captured Stdout
{stdout}

## Code Return (Final Output)
{code_return}
"""

plan_output_extraction_prompt = """
You are an assistant that reads a pipeline PLAN and extracts the output file paths produced by each step.

The PLAN is enclosed between <PLAN> and </PLAN>.

Your job:
- Identify the distinct steps described in the PLAN.
- For each step, extract the concrete output file paths produced by that step.
- Return the result as a structured object with one field:
  - outputs: a list of entries
- Each entry must contain:
  - step_key: the step identifier
  - out_paths: a list of output file paths generated by that step

Step identification:
- A step is any distinct unit of work, such as "Step 1", "QC", "Alignment", "DifferentialExpression", "Biclustering", etc.
- Use the exact step identifier from the PLAN whenever possible.
- Keep step names concise and stable.

Output path rules:
- Include only file paths that are explicitly mentioned or very strongly implied as outputs of the corresponding step.
- Do NOT include input file paths.
- Do NOT include directory paths unless the PLAN clearly states that the directory itself is an output.
- If a step has no clear file outputs, omit that step.
- Do NOT invent extra steps.
- Do NOT invent extra paths.

Be conservative:
- Prefer missing an uncertain path over inventing one.
- Do not invent steps.
- Do not invent file paths.

Formatting requirements:
- outputs must be a list.
- Each item in outputs must contain exactly:
  - step_key
  - out_paths
- out_paths must be a list of strings.
- If no valid outputs are found, return an empty outputs list.
"""

figure_interpretation_from_plan_prompt = """
You will receive two raw text inputs:
- PLAN: a plain-text workflow described in "Step X:" format. It may include function names, inputs, parameters, and output notes. Not every step produces figures.
- FIGURE_OUTPUTS: a Python dict string using the schema Dict[str, Dict[str, Union[str, List[str]]]]. Keys are step identifiers (e.g., "Step1"), values map arbitrary keys to either a single figure path or a list of figure paths. FIGURE_OUTPUTS lists only figure-type products (a subset of all steps).

Your task:
1. For every single figure path mentioned in FIGURE_OUTPUTS, write a concise scientific interpretation (2-4 sentences) explaining:
    • What the figure likely visualizes.
    • Which inputs, steps, or tools it derives from.
    • What signal, pattern, or structural information it may represent.
2. Use the PLAN to reason about each figure.
    • Normalize the step identifier to its number, and find the corresponding step description in PLAN using that identifier.
    • Extract relevant cues such as the function name, tool, or input type to infer what kind of visualization this figure probably is.
3. The tone should be objective and scientific, suitable for figure caption writing. Do not copy phrases or command lines from the PLAN.
4. If no matching step exists in PLAN, still produce a generic but plausible interpretation based on the filename, extension, and typical scientific context.
5. Process only the figures listed in FIGURE_OUTPUTS; ignore other steps.
6. You MUST generate interpretations for ALL figure paths in FIGURE_OUTPUTS, regardless of file extension. PDF files can contain figures. The number of interpretations in your output MUST exactly match the total number of individual figure paths in FIGURE_OUTPUTS.

Output specification:
1. Output only one JSON array, where each element corresponds to one figure and contains:
    - "path": (string) the exact figure path (same as FIGURE_OUTPUTS)
    - "inter": (string) a 2-4 sentence comprehensive English interpretation of that figure
2. Expand FIGURE_OUTPUTS down to individual figures (flatten nested lists).
3. Set any other fields (such as "desc", "capt_title", "capt_body", "feedback") to Null. Do not output any text outside the JSON array.
"""

figure_description_prompt = """Generate an objective figure description and caption.

INPUT:
1. <PATH>: exact file path string of this figure
2. <INTERPRETATION>: Contextual explanation of the figure's logic/significance
3. (Optional) <PREVIOUS_DESCRIPTION>, <PREVIOUS_CAPTION_TITLE>, <PREVIOUS_CAPTION_BODY>
4. (Optional) <USER_FEEDBACK>: User's revision requests
5. <IMAGE>: Base64-encoded figure content

RULES:
1. Keep the original `path` and `inter` unchanged.
2. Generate a **new** `desc` based primarily on the image content.
  - Use <INTERPRETATION> only to infer *contextual focus*, not to restate its wording.
  - Summarize objectively and precisely—avoid speculation or adjectives of judgment.
  - Do not describe plotting elements.
3. Generate a **caption** split into:
  - `capt_title`: one sentence (6-14 words), ends with ".", no numbering, keep abbreviations.
  - `capt_body`: 1-3 sentences summarizing method/object and key signal or comparison; include quantitative or panel info only if clearly visible; no references or technical details.
4. If <USER_FEEDBACK> is provided, you MUST revise `desc` and/or `capt_title`/`capt_body` accordingly:
  - Prioritize explicit instructions. When feedback conflicts with previous text, prefer <USER_FEEDBACK> over previous outputs.
  - Never fabricate results not supported by the visible figure.
5. Set feedback to None
6. Output exactly one JSON object per schema, with no extra text.

DESCRIPTION RULES (`desc`)
Write **two logical layers**:
1. **General overview (1-3 sentences):**
   Highlight the contrast between the entity and the **overall directionality** (which is generally higher/lower/more differentiated/polarized/regionalized).
2. **Pattern-level details (1-4 sentences):**
   Identify specific, visible structures or contrasts:
   - Which histone marks or combinations show strong or weak signals
   - Whether groups form clusters, gradients, or opposing trends
   - Any asymmetric or exceptional patterns (e.g., one subgroup behaving differently)
   - Mention examples using the labels visible in the figure (e.g., "H3K4me3-H3K27me3 shows consistent enrichment across groups 1-2").

Output Schema:
{
    "path": "<PATH echoed verbatim>",
    "inter": "<INTERPRETATION echoed verbatim>",
    "desc": "new description",
    "capt_title": "new caption title",
    "capt_body": "new caption body",
    "feedback": Null
}
"""

figure_feedback_prompt = """You are a feedback processing assistant for scientific figure analysis.

INPUT:
1. FIGURES: List[figure_analysis] with `path`, `inter`, `desc`, `capt_title`, `capt_body`, `feedback`
2. CURRENT_FEEDBACK: User's new comments after reviewing all figures

Your Task:
1. Determine User Satisfaction
   - If the user is satisfied with ALL figures, set "go_back_to_figure_description" to false and return the figures unchanged.
   - If the user requests ANY changes, set "go_back_to_figure_description" to true and continue to process feedback.
2. For each figure, update the `feedback` field based on user's new feedback:
    - Never modify `path`, `inter`, `desc`, `capt_title`or `capt_body` fields. Only update `feedback`.
    - If the user requests removal of a figure, set that its `feedback` to "DELETE".
    - If user mentions a specific figure (by path, number, or content), update ONLY that figure's feedback field.
    - If user gives general feedback for groups of figures, apply it only to figures that clearly match the group condition.
    - The `feedback` field must be actionable and concise, capturing the user's intent for later regeneration of descriptions.
    - Do not add new figures. The output figure count equals the input figure count, except figures marked "DELETE" remain present in the list but with "feedback": "DELETE" (no hard deletion at this step).

Output Schema:
{
  "figures": [
    {
      "path": "unchanged",
      "inter": "unchanged",
      "desc": "unchanged",
      "capt_title": "unchanged",
      "capt_body": "unchanged",
      "feedback": "user feedback or None or 'DELETE'"
    }
  ],
  "go_back_to_figure_description": true or false
}
Return only a single JSON object that strictly follows the schema.
"""

section_planner_prompt = """
You are a bioinformatics report planner. Your goal is to design a concise, logically organized outline for a scientific analysis report.

INPUT:
1. FIGURES: List[figure_analysis], each with:
   - `path`: file path of the figure
   - `inter`: method-aware scientific interpretation
   - `desc`: objective visual description
   - `capt_title`: short caption title summarizing the figure's main focus.
2. FIGURE_INDEX_TABLE: a numbered table mapping 0-based indices to FIGURES[i].path.

TASK:
For each section, produce:
- `title`: clear, scientific section title
- `instr`: 1-3 sentences summarizing the section's focus and logic
- `fig_indices`: JSON array of 0-based indices from FIGURE_INDEX_TABLE
  • sections may contain 0-n figures, but all figures must appear at least once globally
  • each figure index must be assigned to exactly one section (no duplicates across sections)
  • figures that should be compared or discussed together should be grouped in the same section
- `queries`: optional list of keyword combinations for literature search
  • generate **only when the figure descriptions (desc)** imply biologically meaningful mechanisms, pathways, or testable questions
  • each query: 2-4 concise keywords, comma-separated; keep abbreviations; ≤5 per section
  • example: ["H3K27me3, Polycomb, gene silencing", "CUT&Tag, reproducibility, histone mark"]
- `content`: null (reserved for later writing)

GUIDELINES:
• Section grouping should be driven primarily by `inter` and `capt_title`.
• Literature queries should be driven mainly by `desc`.
• Skip `queries` for technical, purely visual, or introductory sections lacking explicit biological context.

OUTPUT:
Return ONLY an ordered JSON array (no outer object). Each element:
{
  "title": "string",
  "instr": "string",
  "fig_indices": [0, 2, ...],
  "queries": null OR ["keyword1, keyword2, ..."],
  "content": null
}
"""

build_section_prompt = """
You are a bioinformatics report writing assistant. Your job is to synthesize a concise, logically organized, and scientifically rigorous **section** for a research report or manuscript.

You will receive:
1. <TITLE>: The section title.
2. <INSTRUCTION>: A short description of the section's scope and purpose.
3. <FIGURES>: A list of dictionaries (or null), each with:
   - `no`: Figure number (for in-text reference)
   - `inter`: Scientific interpretation of the figure (method-aware)
   - `desc`: Objective visual description of the figure
4. <LITERATURE>: A list of dictionaries (or null), each with:
   - `apa`: APA-formatted citation string
   - `pdf`: Full-text content of the paper

WRITING RULES:
1. Write in formal scientific English, suitable for a methods/results section.
2. Reference figures by number (e.g., "Figure 1") where appropriate.
3. If literature is provided, cite using inline APA-style references (Author, Year).
4. Do not fabricate data, statistics, or references.
5. Keep the section focused on the instruction's scope.
6. Output only the section text — no JSON, no headers, no meta-commentary.
"""

consistency_prompt = """
You are an academic editor. Given a list of report sections in order, revise each section while preserving the overall structure.

Your goals:
1. Ensure consistent terminology and abbreviations across all sections
2. Improve logical flow and transitions between sections
3. Remove redundant or overlapping content across sections
4. Maintain a professional and precise scientific tone

Requirements:
- Return the revised sections in the same order as input
- Do not add or remove sections
- Each output section must correspond exactly to one input section
- Preserve the original meaning while improving clarity and consistency
"""

REPORT_WORKERS = 1
RETRY_DELAY = 60

FIGURE_EXTS = [".pdf", ".png", ".jpg", ".jpeg", ".gif", ".svg"]

IMAGE_FORMAT_CONFIG = {
    '.jpg':  {'format': 'JPEG', 'media_type': 'image/jpeg', 'save_params': {'quality': 95, 'optimize': True}},
    '.jpeg': {'format': 'JPEG', 'media_type': 'image/jpeg', 'save_params': {'quality': 95, 'optimize': True}},
    '.png':  {'format': 'PNG',  'media_type': 'image/png',  'save_params': {'optimize': True, 'compress_level': 6}},
    '.gif':  {'format': 'GIF',  'media_type': 'image/gif',  'save_params': {}},
    '.webp': {'format': 'WEBP', 'media_type': 'image/webp', 'save_params': {'quality': 95}},
}
DEFAULT_FORMAT = {'format': 'PNG', 'media_type': 'image/png', 'save_params': {'optimize': True}}

PAGE_MARGIN_MM = 25.4
IMG_HEIGHT_MM = int((297 - 2*PAGE_MARGIN_MM) * 0.8)

html_template = """
<html>
<head>
    <meta charset="utf-8">
    <style>
        @page {{
            size: A4;
            margin: {margin_tb}mm {margin_lr}mm;
        }}
        
        body {{
            font-family: "Times New Roman", Times, serif;
            font-size: 12pt;
            line-height: 1.5;
            color: #000;
            text-align: left;
        }}
        
        h2 {{
            font-size: 14pt;
            font-weight: bold;
            margin-top: 16pt;
            margin-bottom: 10pt;
            text-align: left;
        }}
        
        p {{
            margin-bottom: 10pt;
            text-align: left;
        }}
        
        /* Figure schema */
        .figure {{
            margin: 16pt 0;
            page-break-inside: avoid;
            break-before: avoid-page; 
        }}
        
        img {{
            max-width: 80%;
            max-height: {img_max_height}mm;
            width: auto;
            height: auto;
            display: block;
            margin: 0 auto;
        }}
        
        .figure-caption {{
            margin-top: 6pt;
            font-size: 9pt;
            text-align: left;
            font-style: italic;
            color: #222;
        }}
        
        .figure-caption strong {{
            font-style: normal;
            font-weight: bold;
        }}
        
        /* References schema */
        .references {{
            font-size: 11pt;
            line-height: 1.4;
            text-indent: -18pt;
            text-align: left;
            list-style-position: inside;
        }}
        
        .references li {{
            margin-bottom: 6pt;
            text-align: left;
        }}
    </style>
</head>
<body>
    {content}
</body>
</html>
"""

def planner(state: PipelineState, config: RunnableConfig | None = None) -> PipelineState:
    print("\nPlanner is deriving plan\n")
    user_feedback = state.user_feedback if state.user_feedback else "None"
    messages = [
        SystemMessage(content=path_extractor_instruction),
        HumanMessage(content=f"user input: {state.user_input} \n user feedback: {user_feedback}"),
    ]
    extracted_obj = CONFIG.invoke_model(messages, schema=path_state)

    try:
        from utils import expand_input_file_paths
        expanded_paths, did_expand = expand_input_file_paths(extracted_obj.input_file_path)
    except Exception as e:
        raise RuntimeError(f"[Extractor] Failed to expand input file paths: {e}")

    input_paths_to_use = expanded_paths if did_expand else extracted_obj.input_file_path
    print(f"[Extractor] did_expand: {did_expand}")
    print(f"[Extractor] Final input_file_path to use: {input_paths_to_use}")

    if state.plan is None and route_task_for_rag(state.user_input, state.node_context or ""):
        rag_docs = agentic_retrieve(state.user_input, planner_retriever, model, reranker=rag_reranker, max_retries=0, verbose=CONFIG.rag_verbose)
        domain_context = "\n\n".join(doc.page_content for doc in rag_docs) if rag_docs else "No additional domain context."
        if CONFIG.rag_verbose:
            print(f"[Planner] Agentic RAG returned {len(rag_docs)} relevant documents")
    elif state.plan is not None:
        domain_context = "No additional domain context."
        if CONFIG.rag_verbose:
            print("[Planner] RAG skipped — revision pass, reusing knowledge from prior plan")
    else:
        domain_context = "No additional domain context."
        if CONFIG.rag_verbose:
            print("[Planner] RAG skipped — task fully covered by pipeline nodes")

    system_msg = SystemMessage(content=planner_prompt.format(
        node_context=state.node_context, graph_context=state.graph_context, domain_context=domain_context,
    ))
    user_msg = HumanMessage(content=(
        f"Previous plan (if any): {state.plan or 'None'}\n"
        f"User input: {state.user_input or 'None'}\n"
        f"User feedback: {state.user_feedback or 'None'}"
        f"missing element: {state.missing_elements or 'None'}"
        f"Input file paths: {input_paths_to_use or 'None'}"
        f"Output directory: {extracted_obj.output_dir or 'None'}"
    ))
    result = CONFIG.invoke_model([system_msg, user_msg], config=config)
    plan_obj = result.content

    return state.merge(
        plan=plan_obj, full_plan="", downstream_plan=plan_obj, go_back_to_planner=False,
        input_file_path=input_paths_to_use, output_dir=extracted_obj.output_dir,
        messages=[user_msg, AIMessage(content=plan_obj)]
    )

def extract_input_state(state: PipelineState) -> PipelineState:
    print("\n[Extractor] Extracting important information...\n")
    input_file_paths = getattr(state, "input_file_path", [])
    output_dir = getattr(state, "output_dir", ())

    messages = [
        SystemMessage(content=extractor_instruction),
        HumanMessage(content=f"current proposed plan: {state.plan}\nuser input: {state.user_input} \n input file paths: {input_file_paths}"),
    ]
    try:
        extracted_obj = CONFIG.invoke_model(messages, schema=input_extractor_state)
    except Exception as e:
        raise RuntimeError(f"[Extractor] Failed to extract input state: {e}")

    summary_msg = AIMessage(content=format_summary(extracted_obj, input_file_paths, output_dir))
    extracted_dict = extracted_obj.model_dump()
    extracted_dict.update({"messages": [summary_msg]})
    return state.merge(**extracted_dict)

def format_summary(extracted_obj: input_extractor_state, input_file_path: List[str], output_dir: str) -> str:
    lines = [
        "\n\n", "-------- EXTRACTED FIELDS ------",
        f"Overall plan: {extracted_obj.overall_plan}",
        f"Number of sample: {extracted_obj.num_of_sample}",
        f"Input file path(s): {input_file_path}",
        f"Required parameters: {extracted_obj.required_parameters}",
        f"Output_dir: {output_dir}",
    ]
    lines.append("--------------------------------")
    return "\n\n".join(lines)

def user_feedback_node(state: PipelineState, config: RunnableConfig | None = None) -> PipelineState:
    user_feedback_text = interrupt("Please provide your feedback on the plan (press Enter to accept as-is):")
    print(f"\n[User Feedback] Received feedback: {user_feedback_text}\n")
    human_msg = HumanMessage(content=f"User feedback: {user_feedback_text}")
    messages = [
        SystemMessage(content=user_feedback_instructions),
        HumanMessage(content=f"Plan: {state.plan} \n User feedback: {user_feedback_text}"),
    ]
    decision = CONFIG.invoke_model(messages, schema=user_feedback_state)
    decision_text = (
        "User feedback suggests we should regenerate the plan."
        if decision.go_back_to_planner else "User is satisfied — proceed to validation."
    )
    ai_msg = AIMessage(content=decision_text)
    return state.merge(go_back_to_planner=decision.go_back_to_planner, user_feedback=user_feedback_text, messages=[human_msg, ai_msg])

from utils import check_files_exist, check_or_create_output_dir

def validator(state: PipelineState) -> PipelineState:
    print("\nValidator is checking if all needed field is complete\n")
    system_msg = SystemMessage(content=validator_instructions)
    user_prompt = """Input Summary
    --------------

    • Plan (text):
    {plan}

    • Number of Samples (int):
    {num_of_sample}

    • Input File Paths (list of str):
    {input_file_path}

    • Required Parameters / Constraints (JSON):
    {parameters}

    • Output Directory (str):
    {output_dir}
    """
    user_msg = HumanMessage(content=user_prompt.format(
        plan=getattr(state, "plan", ""),
        num_of_sample=getattr(state, "num_of_sample", 0),
        input_file_path=getattr(state, "input_file_path", "None"),
        parameters=getattr(state, "specified_parameters", {}),
        output_dir=getattr(state, "output_dir", "None"),
    ))

    validation_text = ""
    result = CONFIG.invoke_model([system_msg, user_msg], schema=validator_state)

    file_missing_check, missing_files = check_files_exist(state.input_file_path)
    output_dir_exist = check_or_create_output_dir(state.output_dir)
    result.validation = (file_missing_check and result.validation and output_dir_exist)
    if missing_files:
        validation_text += f"The following path doesn't actually exist: {missing_files}. "
    if not output_dir_exist:
        validation_text += "Output directory doesn't exist or not create successfully."

    thinking = result.validation_thinking or ""
    if result.validation:
        validation_text += f"{thinking.strip()}\n\n✅ Validation passed — nothing else required from you."
    else:
        validation_text += (
            "Validation failed.\n" + f"{thinking.strip()}\n\n"
            "The following items are missing:\n" +
            "\n".join(f"   • {item}" for item in result.missing_elements or []) +
            "\n\nPlease provide the missing information and resubmit your plan."
        )

    ai_msg = AIMessage(content=validation_text)
    return state.merge(validation=result.validation, missing_elements=result.missing_elements, validation_thinking=result.validation_thinking, messages=[ai_msg])

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tools.convert_bam_to_bed import convert_bam_to_bed
from tools.convert_bed_to_bedgraph import convert_bed_to_bedgraph
from tools.convert_bed_to_bigwig import convert_bed_to_bigwig
from tools.create_barcode_fasta import create_barcode_fasta
from tools.demultiplex_reads import demultiplex_reads
from tools.identify_adapter import identify_adapter
from tools.align_reads import align_reads
from tools.chipdip_prep import chipdip_prep
from tools.build_count_matrix import build_count_matrix
from tools.biclustering_wrapper import biclustering_wrapper
from tools.differential_analysis import differential_analysis
from tools.peak_profiling import peak_profiling
from tools.peak_differential_analysis import peak_differential_analysis

tools = [
    convert_bam_to_bed, convert_bed_to_bedgraph,
    convert_bed_to_bigwig, create_barcode_fasta, demultiplex_reads,
    identify_adapter, align_reads, chipdip_prep, build_count_matrix,
    biclustering_wrapper,
    differential_analysis,
    peak_profiling,
    peak_differential_analysis
]

def executor(state: PipelineState):
    """
    Three-section plan executor:
      1. full_plan   — append-only history of executed steps (template-based, no LLM)
      2. current_step_call — the single step extracted for execution now
      3. downstream_plan — remaining steps, updated with propagated outputs
    
    Uses a SINGLE LLM call (step_manager) to extract, route, and update.
    """
    latest_tool_msg = next((msg for msg in reversed(state.messages) if isinstance(msg, ToolMessage)), None)
    function_return = latest_tool_msg.content if latest_tool_msg else None

    messages = [
        SystemMessage(content=step_manager_prompt),
        HumanMessage(content=(
            f"<DOWNSTREAM_PLAN>\n{state.downstream_plan}\n</DOWNSTREAM_PLAN>\n\n"
            f"<TOOL_OUTPUT>\n{function_return or 'None'}\n</TOOL_OUTPUT>\n\n"
            f"<LAST_STEP_NAME>\n{state.current_step or 'None'}\n</LAST_STEP_NAME>"
        )),
    ]
    result = CONFIG.invoke_model(messages, schema=step_manager_output)

    current_step = result.current_step_call or "END"
    updated_downstream = result.updated_downstream_plan or ""
    move_to_auto_code = result.is_auto_code
    executed_step_updated = result.executed_step_updated or ""

    # full_plan starts empty and grows step by step. Each round, the LLM takes the
    # just-executed step (state.current_step) and rewrites it with actual outputs
    # filled in from tool_output. We simply append that updated step text.
    # At the end, full_plan = the complete plan with every step's real outputs.
    full_plan = state.full_plan or ""
    if executed_step_updated:
        full_plan = (full_plan + "\n\n" + executed_step_updated).strip()

    print(f"\n[Executor] current_step: {current_step}")
    print(f"[Executor] is_auto_code: {move_to_auto_code}")
    print(f"[Executor] downstream_plan length: {len(updated_downstream)}")
    print(f"[Executor] executed_step_updated: {executed_step_updated[:200] if executed_step_updated else 'N/A'}")

    if current_step.strip().upper() == "END":
        print("[Executor] All steps executed. Moving to replayer.")
        return state.merge(
            plan=current_step, full_plan=full_plan,
            downstream_plan=updated_downstream,
            current_step=current_step,
            move_to_auto_code_generator=False,
            messages=[AIMessage(content="All plan steps executed successfully. Moving to END.")],
        )

    if move_to_auto_code:
        print("[Executor] Moving to auto code generator based on plan analysis.")
        return state.merge(
            plan=current_step, full_plan=full_plan,
            downstream_plan=updated_downstream,
            current_step=current_step,
            move_to_auto_code_generator=True,
            messages=[AIMessage(content="Switching to automatic code generation as per plan analysis.")],
        )
    else:
        print("[Executor] Executing structured plan with tool call...")
        executor_instructions = (
            "You are a code executor responsible for executing structured plans. "
            "Based on the user-provided current step and existing file list, generate and execute the necessary tool call. "
            "Execute ONLY the current step described below. Do NOT look ahead to future steps."
        )
        messages = [
            SystemMessage(content=executor_instructions),
            HumanMessage(content=f"Current Step to Execute:\n{current_step}\n\nInput File: {state.input_file_path}"),
        ]
        response = CONFIG.invoke_model(messages, tools=tools)
        if response.tool_calls:
            tool_call = response.tool_calls[0]
            print(f"🔧 Tool to be called: {tool_call['name']}")
            print(f"🧷 Tool call id: {tool_call['id']}")

        code_sequence = getattr(state, "whole_code_sequence", [])
        code_sequence.append(response)
        return state.merge(
            plan=current_step, full_plan=full_plan,
            downstream_plan=updated_downstream,
            current_step=current_step,
            move_to_auto_code_generator=False,
            messages=[response], whole_code_sequence=code_sequence,
        )

def tools_checker(state: PipelineState, max_retry: int = 1, log_key: str = "log_out"):
    latest_tool_msg = next((msg for msg in reversed(state.messages) if isinstance(msg, ToolMessage)), None)
    function_return = json.loads(latest_tool_msg.content) if latest_tool_msg else {}

    retry_count = getattr(state, "retry_count", 0)

    if any(v is None for v in function_return.values()) and retry_count < max_retry:
        checker_instructions = (
            "You are reviewing the output of a bioinformatics tool call. "
            "Based on the tool output and log, decide if the execution failed and whether the failure is fixable by correcting the input parameters "
            "(e.g. wrong file path, wrong file extension, typo in filename). "
            "Only set go_back_to_tools to true if you believe a parameter correction would likely fix the issue. "
            "Do NOT retry if the failure is caused by missing upstream outputs, data corruption, or issues unrelated to the parameters passed."
        )

        base_content = checker_instructions + str(function_return)
        available_tokens = CONFIG.max_tokens * 0.65 - len(base_content) // 4
        max_log_chars = int(available_tokens * 4) 

        log_path = function_return.get(log_key)
        log_content = open(log_path).read() if log_path and os.path.exists(log_path) else "Log file not found."
        if len(log_content) > max_log_chars:
            log_content = log_content[-max_log_chars:]

        result = CONFIG.invoke_model([
            SystemMessage(content=checker_instructions),
            HumanMessage(content=f"Tool output:\n{function_return}\n\nLog:\n{log_content}"),
        ], schema=tools_checker_output)

        if result.go_back_to_tools:
            executor_instructions = (
                "You are a code executor responsible for executing structured plans. "
                "Based on the user-provided current step and existing file list, generate and execute the necessary tool call. "
                "Execute ONLY the current step described below. Do NOT look ahead to future steps. "
                "A previous attempt at this step failed. Carefully read the provided log output to identify "
                "the incorrect parameters (e.g. wrong file path or extension) and correct them before retrying."
            )
            messages = [
                SystemMessage(content=executor_instructions),
                HumanMessage(content=f"Current Step to Execute:\n{state.current_step}\n\nPrevious attempt failed with the following log:\n{log_content}"),
            ]
            response = CONFIG.invoke_model(messages, tools=tools)
            if response.tool_calls:
                tool_call = response.tool_calls[0]
                print(f"🔧 Tool to be called: {tool_call['name']}")
                print(f"🧷 Tool call id: {tool_call['id']}")
            code_sequence = getattr(state, "whole_code_sequence", [])
            code_sequence.append(response)
            return state.merge(
                go_back_to_tools=True, retry_count=retry_count + 1,
                messages=[response], whole_code_sequence=code_sequence,
            )
        else:
            print(f"[tools_checker] LLM decided failure is not fixable by parameter correction. Proceeding.")
    return state.merge(go_back_to_tools=False, retry_count=0)

def replayer(state: PipelineState):
    sys.path.append(os.getcwd())
    from utils import generate_summary
    output_dir = getattr(state, "output_dir", None)
    if output_dir:
        output_dir = output_dir.strip()
    whole_code_sequence = getattr(state, "whole_code_sequence", [])
    messages = getattr(state, "messages", [])
    full_plan = getattr(state, "full_plan", None)
    input_file_path = getattr(state, "input_file_path", None)
    generate_summary(output_dir, messages, full_plan, input_file_path)

    messages = [
        SystemMessage(content=replayer_prompt),
        HumanMessage(content=f"whole_code: {whole_code_sequence} \n node_context: {state.node_context}"),
    ]
    replayer_output = CONFIG.invoke_model(messages)

    if not output_dir:
        output_dir = Path("report_output")
    else:
        output_dir = Path(output_dir)
    output_file_path = output_dir / "summary/summary.py"
    with open(output_file_path, "w") as f:
        f.write(replayer_output.content)
    return state.merge(messages=[AIMessage(content=f"Replayer output saved to {output_file_path}")])


class FunctionNameOutput(BaseModel):
    """Extracted function name from the generated code."""
    function_name: str = Field(description="The name of the primary function defined in the generated code.")

def get_prompt_for_language(language: str) -> str:
    language_lower = language.lower()
    if language_lower == "python":
        return auto_code_generator_prompt
    elif language_lower == "r":
        return R_PROMPT_TEMPLATE
    elif language_lower in ["bash", "shell", "sh"]:
        return BASH_PROMPT_TEMPLATE
    else:
        return GENERAL_PROMPT_TEMPLATE

def auto_code_generator(user_message, output_dir: str, config: RunnableConfig | None = None,
                         language: str = "python", use_rag: bool = False, custom_context: Optional[str] = None):
    print(f"\n[Auto Code Generator] Generating {language.upper()} code based on request...\n")
    messages = [
        SystemMessage(content=function_name_extraction_prompt),
        HumanMessage(content=f"Generate a function name for: {user_message}"),
    ]
    try:
        name_result = CONFIG.invoke_model(messages, schema=FunctionNameOutput, config=config)
        function_name = name_result.function_name
    except:
        name_response = CONFIG.invoke_model(messages, config=config)
        function_name = name_response.content.strip().replace(" ", "_").lower()
    print(f"[Auto Code Generator] Function name: {function_name}")

    rag_context = ""
    if use_rag:
        if custom_context:
            rag_context = f"Context:\n{custom_context}"
        else:
            rag_docs = agentic_retrieve(user_message, acg_retriever, model, reranker=rag_reranker, max_retries=0, verbose=CONFIG.rag_verbose)
            if CONFIG.rag_verbose:
                print(f"[Auto Code Generator] Agentic RAG returned {len(rag_docs)} relevant documents")
            if rag_docs:
                rag_context = "Context:\n" + "\n\n".join(doc.page_content for doc in rag_docs)
    if not rag_context:
        rag_context = "Generate based on general knowledge."

    prompt_template = get_prompt_for_language(language)
    if language.lower() == "python":
        user_content = f"Generate Python code for the following request:\n{user_message}\n\nThe main function should be named: {function_name}"
        system_content = prompt_template.format(rag_context=rag_context)
    else:
        system_content = prompt_template.format(query=user_message, language=language, rag_context=rag_context)
        user_content = user_message

    messages = [system_content, user_content]
    try:
        result = CONFIG.invoke_model(messages, schema=GeneratedCode, config=config)
        generated_code = result.full_code
        description = result.description
    except:
        response = CONFIG.invoke_model(messages, config=config)
        generated_code = response.content
        description = f"Generated {language} code for: {user_message}"

    if "```python" in generated_code or f"```{language}" in generated_code:
        code_match = re.search(rf'```{language}?\n(.*?)```', generated_code, re.DOTALL)
        if code_match:
            generated_code = code_match.group(1)
    elif "```" in generated_code:
        code_match = re.search(r'```\n(.*?)```', generated_code, re.DOTALL)
        if code_match:
            generated_code = code_match.group(1)

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    extension = LANGUAGE_EXTENSIONS.get(language.lower(), ".txt")
    filename = f"{function_name}_{timestamp}{extension}"
    auto_code_dir = os.path.join(output_dir, "auto_code")
    os.makedirs(auto_code_dir, exist_ok=True)
    file_path = os.path.join(auto_code_dir, filename)
    try:
        absolute_path = os.path.abspath(file_path)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(generated_code)
        print(f"[Auto Code Generator] Code saved to: {absolute_path}")
    except Exception as e:
        print(f"[Auto Code Generator] Error saving file: {e}")
        absolute_path = None
    return absolute_path

def auto_code_generator_node(state: PipelineState, config: RunnableConfig | None = None) -> PipelineState:
    print("Begin auto_code_generator_subgraph")
    plan_description = f"current step: {state.current_step} \n full plan: {state.plan}"
    language = getattr(state, 'language', 'python')
    use_rag = getattr(state, 'use_rag', True)
    custom_context = getattr(state, 'custom_context', None)
    output_dir = getattr(state, 'output_dir', os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd())
    absolute_path = auto_code_generator(plan_description, output_dir, config, language=language, use_rag=use_rag, custom_context=custom_context)
    return state.merge(code_path=absolute_path, check_iteration=0)

def code_executor(file_path, config: RunnableConfig | None = None):
    print("\n[Code Executor] Executing generated Python code...\n")
    if not file_path or not os.path.exists(file_path):
        error_msg = f"Error: File path does not exist: {file_path}"
        print(f"[Code Executor] {error_msg}")
        return {"stdout": "", "stderr": error_msg, "success": False, "message": AIMessage(content=f"Failed to execute code: {error_msg}"), "run_spec": None}

    print(f"[Code Executor] Executing file: {file_path}")
    run_spec = {"cmd": [sys.executable, file_path], "cwd": os.path.dirname(file_path), "timeout": 300, "capture_output": True, "text": True}
    try:
        result = subprocess.run(run_spec["cmd"], capture_output=True, text=True, timeout=run_spec["timeout"], cwd=run_spec["cwd"])
        stdout, stderr, return_code = result.stdout, result.stderr, result.returncode
        print(f"[Code Executor] Execution completed with return code: {return_code}")
        if stdout: print(f"[Code Executor] STDOUT:\n{stdout}")
        if stderr: print(f"[Code Executor] STDERR:\n{stderr}")
        success = return_code == 0
        if success:
            content = f"Code executed successfully!\n\nFile: {os.path.basename(file_path)}\nOutput:\n{stdout}" if stdout else f"Code executed successfully with no output.\n\nFile: {os.path.basename(file_path)}"
        else:
            content = f"Code execution failed with return code {return_code}.\n\nFile: {os.path.basename(file_path)}\nError output:\n{stderr}\nStandard output:\n{stdout}"
        return {"stdout": stdout, "stderr": stderr, "success": success, "return_code": return_code, "message": AIMessage(content=content), "code_return": content, "run_spec": run_spec}
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": "Execution timed out after 300 seconds", "success": False, "message": AIMessage(content="Code execution timed out"), "run_spec": run_spec}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "success": False, "message": AIMessage(content=f"Code execution failed: {e}"), "run_spec": run_spec}

def code_executor_node(state: PipelineState, config: RunnableConfig | None = None) -> PipelineState:
    file_path = state.code_path
    if not file_path:
        return state.merge(stdout='', stderr='No file path provided', success=False, messages=[AIMessage(content="Error: No file path found in state.")])
    print(f"[Code Executor Node] File path to execute: {file_path}")
    result = code_executor(file_path, config)
    return state.merge(stdout=result['stdout'], stderr=result['stderr'], success=result['success'],
                       return_code=result.get('return_code'), messages=[result['message']],
                       code_return=result.get('code_return'), execution_script=result.get('run_spec'))

def trim_to_last_2400_words(text1: str, text2: str):
    def trim(text: str) -> str:
        words = text.split()
        return " ".join(words[-2400:]) if len(words) > 2400 else text
    return trim(text1), trim(text2)

def error_checker(state: PipelineState, config: RunnableConfig | None = None) -> PipelineState:
    check_iteration = state.check_iteration
    stdout, stderr = trim_to_last_2400_words(state.stdout, state.stderr)
    return_code = state.return_code
    plan = state.plan_description
    with open(state.code_path, "r") as file:
        code = file.read()

    try:
        messages = [
            SystemMessage(content=error_determine_system_prompt),
            HumanMessage(content=error_determine_human_prompt.format(plan=plan, code=code, return_code=return_code, stdout=stdout, stderr=stderr)),
        ]
        result = CONFIG.invoke_model(messages)
        text = getattr(result, "content", str(result)).strip().lower()
        if text not in ("true", "false"):
            raise RuntimeError(f"[Extractor] Expected 'True' or 'False' but got: {text!r}")
        should_reexecute = (text == "true")
    except Exception as e:
        raise RuntimeError(f"[Extractor] Failed to extract input state: {e}")

    if should_reexecute:
        print("[Error Checker] Errors detected in code execution. Preparing to revise and re-execute...")
        if state.check_iteration > 5:
            print("Maximum error checking iterations reached. Exiting loop.")
            return state.merge(should_reexecute=False)
    else:
        print("[Error Checker] No errors detected. Code executed successfully.")

    check_iteration += 1
    if should_reexecute:
        messages = [
            HumanMessage(content=error_corrector_human_prompt.format(plan=plan, code=code, current_step=state.current_step, return_code=return_code, stdout=stdout, stderr=stderr)),
            SystemMessage(content=error_corrector_system_prompt),
        ]
        generated_msg = CONFIG.invoke_model(messages)
        generated_code = getattr(generated_msg, "content", str(generated_msg))
        with open(state.code_path, 'w') as f:
            f.write(generated_code)
        print(f"[Error Checker] Revised code written to: {state.code_path}")

    return state.merge(stdout=stdout, stderr=stderr, check_iteration=check_iteration, should_reexecute=should_reexecute, messages=[AIMessage(content="Error checker completed.")])

def human_in_the_loop(state: PipelineState, config: RunnableConfig | None = None) -> PipelineState:
    _ = interrupt("HUMAN_IN_THE_LOOP")
    still_fall = state.still_fall + 1
    return state.merge(still_fall=still_fall, check_iteration=0)

def summary(state: PipelineState, config: RunnableConfig | None = None) -> PipelineState:
    print("\n[Summary] Generating summary and returning to the main graph...\n")
    plan = state.current_step or ""
    stdout = state.stdout or ""
    code_return = state.code_return if state.code_return is not None else ""
    execution_script = state.execution_script or ""

    messages = [
        SystemMessage(content=summary_system_prompt),
        HumanMessage(content=summary_human_prompt.format(plan=plan, stdout=stdout, code_return=code_return, execution_script=execution_script)),
    ]
    response = CONFIG.invoke_model(messages, config=config)
    summary_text = getattr(response, "content", str(response))

    tool_msg = ToolMessage(content=summary_text, name="emit_summary", tool_call_id=str(uuid4()))
    code_sequence = getattr(state, "whole_code_sequence", [])
    code_sequence.append(tool_msg)
    return state.merge(summary=summary_text, messages=[tool_msg], whole_code_sequence=code_sequence)

def should_reexecute(state: PipelineState):
    if state.should_reexecute and state.check_iteration > 5:
        return "human_in_the_loop"
    elif state.should_reexecute and state.check_iteration <= 5:
        return "code_executor"
    else:
        return 'summary'

def should_regenerate(state: PipelineState):
    return "code_executor" if state.still_fall == 1 else 'summary'


subgraph_builder = StateGraph(PipelineState)
subgraph_builder.add_node("auto_code_generator", auto_code_generator_node)
subgraph_builder.add_node("code_executor", code_executor_node)
subgraph_builder.add_node("human_in_the_loop", human_in_the_loop)
subgraph_builder.add_node("error_checker", error_checker)
subgraph_builder.add_node("summary", summary)
subgraph_builder.add_edge(START, "auto_code_generator")
subgraph_builder.add_edge("auto_code_generator", "code_executor")
subgraph_builder.add_edge("code_executor", "error_checker")
subgraph_builder.add_edge("summary", END)
subgraph_builder.add_conditional_edges("error_checker", should_reexecute, ["code_executor", "human_in_the_loop", "summary"])
subgraph_builder.add_conditional_edges("human_in_the_loop", should_regenerate, ["summary", "code_executor"])
sub_graph = subgraph_builder.compile()


def convert_figure_format(path):
    if path.suffix.lower() in [".pdf", ".svg"]:
        new_path = path.with_suffix(".png")
        subprocess.run(["magick", "-density", "300", str(path), str(new_path)], check=True)
        return str(new_path)
    else:
        return str(path)

def extract_figure_paths(outputs: Dict[str, List[str]], figure_exts=FIGURE_EXTS) -> Tuple[List[str], Dict[str, List[str]]]:
    all_paths: List[str] = []
    figure_outputs: Dict[str, List[str]] = {}
    seen_stems: set = set()
    for step, paths in outputs.items():
        if isinstance(paths, str):
            candidates = [paths]
        elif isinstance(paths, list):
            candidates = [x for x in paths if isinstance(x, str)]
        else:
            continue 
        converted: List[str] = []
        for x in candidates:
            p = Path(x)
            if p.suffix.lower() not in figure_exts:
                continue
            if p.suffix.lower() in [".pdf", ".svg"]:
                stem = p.with_suffix(".png").resolve().with_suffix("")
            else:
                stem = p.resolve().with_suffix("")
            if stem in seen_stems:
                continue
            seen_stems.add(stem)
            converted.append(convert_figure_format(p))
        if converted:
            all_paths.extend(converted)
            figure_outputs[step] = converted
    return all_paths, figure_outputs

class OutputEntry(BaseModel):
    """A single step's output: its identifier and the file paths it produced."""
    step_key: str = Field(description="Step identifier")
    out_paths: List[str] = Field(default_factory=list, description="Output file paths generated by this step")

class PlanOutputs(BaseModel):
    """All output file paths extracted from the executed pipeline plan, grouped by step."""
    outputs: List[OutputEntry] = Field(default_factory=list, description="List of plan steps and their output file paths")

    @field_validator('outputs', mode='before')
    @classmethod
    def parse_outputs_str(cls, v):
        if isinstance(v, str):
            return json.loads(v)
        return v

def extract_outputs_from_full_plan(full_plan: str) -> Dict[str, List[str]]:
    messages = [
        SystemMessage(content=plan_output_extraction_prompt),
        HumanMessage(content=f"<PLAN>\n{full_plan}\n</PLAN>"),
    ]
    result = CONFIG.invoke_model(messages, schema=PlanOutputs)

    cleaned_outputs = {}
    for item in result.outputs:
        step = item.step_key.strip() if isinstance(item.step_key, str) else ""
        if not step: continue
        valid_paths = [p.strip() for p in item.out_paths if isinstance(p, str) and p.strip()]
        if valid_paths:
            cleaned_outputs[step] = valid_paths
    return cleaned_outputs

def get_figure_config(path):
    return IMAGE_FORMAT_CONFIG.get(Path(path).suffix.lower(), DEFAULT_FORMAT)

def convert_to_rgb_for_jpeg(fig):
    if fig.mode in ('RGBA', 'LA'):
        background = PILImage.new('RGB', fig.size, (255, 255, 255))
        mask = fig.split()[3] if fig.mode == 'RGBA' else fig.split()[1]
        background.paste(fig, mask=mask)
        return background
    return fig.convert('RGB')

def compress_image_to_target_size(fig, path, max_size_kb):
    fig_config = get_figure_config(path)
    target_bytes = max_size_kb * 1024
    width, height = fig.size
    if fig.mode in ('RGBA', 'LA', 'P'):
        fig = convert_to_rgb_for_jpeg(fig)
    compressed_data = None
    for scale in [0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3]:
        resized = fig.resize((int(width * scale), int(height * scale)), PILImage.Resampling.LANCZOS)
        buffer = BytesIO()
        resized.save(buffer, format=fig_config['format'], **fig_config['save_params'])
        compressed_data = buffer.getvalue()
        if len(compressed_data) <= target_bytes:
            return compressed_data
    return compressed_data

def load_figure_as_base64(path, max_size_kb):
    if os.path.getsize(path) / 1024 <= max_size_kb:
        with open(path, "rb") as f:
            return base64.standard_b64encode(f.read()).decode("utf-8")
    fig = PILImage.open(path)
    compressed_data = compress_image_to_target_size(fig=fig, path=path, max_size_kb=max_size_kb)
    return base64.standard_b64encode(compressed_data).decode("utf-8")


def figure_interpretation(state: PipelineState):
    full_plan = getattr(state, "full_plan", "")
    outputs = extract_outputs_from_full_plan(full_plan) if full_plan.strip() else {}
    print("\n\n🧠 AiMessage: Start Generating Report \n")
    paths, figure_outputs = extract_figure_paths(outputs)
    if not paths:
        return state.merge(outputs=outputs)
    print("🧠 AiMessage: Interpreting the following figures:")
    for path in paths: print(path)
    messages = [
        SystemMessage(content=figure_interpretation_from_plan_prompt),
        HumanMessage(content=f"<PLAN>\n{state.full_plan}\n</PLAN>\n\n<FIGURE_OUTPUTS>\n{figure_outputs}\n</FIGURE_OUTPUTS>\n\n"),
    ]
    result = CONFIG.invoke_model(messages, schema=figure_state)
    return state.merge(figures=result.figures, outputs=outputs)

def describe_single_image(fig):
    fig_config = get_figure_config(fig.path)
    media_type = fig_config['media_type']
    if fig.feedback:
        base_content = (f"<PATH>\n{fig.path}\n</PATH>\n\n<INTERPRETATION>\n{fig.inter}\n</INTERPRETATION>\n\n"
                        f"<PREVIOUS_DESCRIPTION>\n{fig.desc}\n</PREVIOUS_DESCRIPTION>\n\n"
                        f"<PREVIOUS_CAPTION_TITLE>\n{fig.capt_title}\n</PREVIOUS_CAPTION_TITLE>\n\n"
                        f"<PREVIOUS_CAPTION_BODY>\n{fig.capt_body}\n</PREVIOUS_CAPTION_BODY>\n\n"
                        f"<USER_FEEDBACK>\n{fig.feedback}\n</USER_FEEDBACK>\n\n")
    else:
        if fig.desc: return fig
        base_content = f"<PATH>\n{fig.path}\n</PATH>\n\n<INTERPRETATION>\n{fig.inter}\n</INTERPRETATION>\n\n"
    max_tokens = CONFIG.max_tokens
    available_tokens = max_tokens * 0.65 - len(base_content) // 4
    max_size_kb = available_tokens * 3 / 4 / 1024
    image_base64 = load_figure_as_base64(path=fig.path, max_size_kb=max_size_kb)
    user_msg = HumanMessage(content=[{"type": "text", "text": base_content}, {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{image_base64}"}}])
    system_msg = SystemMessage(figure_description_prompt)
    messages = [
        SystemMessage(figure_description_prompt),
        user_msg,
    ]
    result = CONFIG.invoke_model(messages, schema=figure_analysis)
    return result

def describe_single_image_with_retry(fig):
    last_err = None
    for attempt in range(1, 3):
        try:
            return describe_single_image(fig)
        except Exception as e:
            last_err = e
            print(f"  ❌ Attempt {attempt}/2 failed for {fig.path}: {type(e).__name__}: {e}")
            if attempt >= 2: raise last_err
            print(f"  ⏳ Rate limit hit, waiting {RETRY_DELAY}s...")
            time.sleep(RETRY_DELAY)
    raise last_err

def figure_description(state: PipelineState):
    print("\n🧠 AiMessage: Generating captions and descriptions \n")
    updated_figures = []
    max_figure_requests = REPORT_WORKERS
    with ThreadPoolExecutor(max_workers=max_figure_requests) as pool:
        future_to_fig = {pool.submit(describe_single_image_with_retry, fig): (idx, fig) for idx, fig in enumerate(state.figures, 1)}
        for future in as_completed(future_to_fig):
            idx, original_fig = future_to_fig[future]
            try:
                result = future.result()
                updated_figures.append((idx, result))
            except Exception:
                updated_figures.append((idx, original_fig))
    updated_figures.sort(key=lambda x: x[0])
    sorted_figures = [fig for _, fig in updated_figures]
    for fig in sorted_figures:
        print(f'\n===== {fig.path} =====\nCaption: {fig.capt_title} {fig.capt_body}\nDescription: {fig.desc}\n')
    return state.merge(figures=sorted_figures)

def figure_feedback(state: PipelineState) -> PipelineState:
    feedback_on_figures = interrupt("Please provide feedback on the figures:")
    print(f"\nUser Feedback Received:\n{feedback_on_figures}\n")
    human_msg = HumanMessage(content=feedback_on_figures)
    messages = [
        SystemMessage(figure_feedback_prompt),
        HumanMessage(content=f"<FIGURES>\n{state.figures}\n</FIGURES>\n\n<CURRENT_FEEDBACK>\n{feedback_on_figures}\n</CURRENT_FEEDBACK>\n\n"),
    ]
    result = CONFIG.invoke_model(messages, schema=figure_feedback_state)

    filtered_figures = [fig for fig in result.figures if fig.feedback != "DELETE"]
    decision_text = "User feedback suggests we should regenerate the figure descriptions." if result.go_back_to_figure_description else "User is satisfied — proceed to section planning."
    return state.merge(figures=filtered_figures, go_back_to_figure_description=result.go_back_to_figure_description, messages=[human_msg, AIMessage(content=decision_text)])

def section_planning(state: PipelineState):
    figures = state.figures
    filtered_figures = [{"inter": fig.inter, "desc": fig.desc, "capt_title": fig.capt_title} for fig in figures]
    figure_index = "\n".join(f"[{i}] {fa.path}" for i, fa in enumerate(figures))
    messages = [
        SystemMessage(section_planner_prompt),
        HumanMessage(content=f"<FIGURES>\n{filtered_figures}\n</FIGURES>\n\n<FIGURE_INDEX_TABLE>\n{figure_index}\n</FIGURE_INDEX_TABLE>\n"),
    ]
    result = CONFIG.invoke_model(messages, schema=report_state)

    fig_order = [idx for section in result.sections for idx in (section.fig_indices or [])]
    reorder = {old_idx: new_idx for new_idx, old_idx in enumerate(fig_order)}
    reordered_figures = [figures[old_idx] for old_idx in fig_order]
    new_sections = []
    for section in result.sections:
        if section.fig_indices:
            section.fig_indices = [reorder[old_idx] for old_idx in section.fig_indices]
        new_sections.append(section)
    return state.merge(sections=new_sections, figures=reordered_figures)

def safe_get_json(url, params=None, timeout=60, retries=3):
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            if not resp.text.strip():
                raise ValueError("Empty response body")
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            wait = 5 * (attempt + 1)
            time.sleep(wait)
    return None

def out_formatted(out):
    return [{'doi': rec.get('doi'), 'title': rec.get('title'), 'abstractText': rec.get('abstractText'),
             'isOpenAccess': rec.get('isOpenAccess'), 'hasPDF': rec.get('hasPDF'),
             'fullTextUrlList': rec.get('fullTextUrlList'), 'pmcid': rec.get('pmcid')} for rec in out]

def fetch_top_n(query, n=None, cursor="*", page_size=100, formatted=True):
    url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    keywords = query.split(sep=',')
    query = " AND ".join([f'"{k.strip()}"' for k in keywords])
    if n is not None and n < 200:
        params = {"query": query, "format": "json", "resultType": "core", "pageSize": n, "cursorMark": cursor}
        r = safe_get_json(url, params=params)
        if r is None:
            return ([], cursor)
        out = r.get("resultList", {}).get("result", [])
        cursor = r.get("nextCursorMark")
    else:
        out = []
        while True:
            this_page = page_size if n is None else min(page_size, n - len(out))
            params = {"query": query, "format": "json", "resultType": "core", "pageSize": this_page, "cursorMark": cursor}
            r = safe_get_json(url, params=params)
            if r is None: break
            batch = r.get("resultList", {}).get("result", [])
            if not batch: break
            out.extend(batch)
            if n is not None and len(out) >= n: break
            next_cursor = r.get("nextCursorMark")
            if not next_cursor or next_cursor == cursor: break
            cursor = next_cursor
    return (out_formatted(out), cursor) if formatted else (out, cursor)

def load_pdf_from_urls(urls, safe_doi, down_dict):
    for u in urls:
        if u.get("documentStyle") == "pdf" and u.get("availabilityCode") == "OA":
            pdf_path = os.path.join(down_dict, safe_doi + '.pdf')
            try:
                pdf_data = requests.get(u["url"], timeout=60).content
                with open(pdf_path, "wb") as f: f.write(pdf_data)
                loader = PyMuPDFLoader(pdf_path)
                result = loader.load()
                os.remove(pdf_path)
                return result
            except Exception:
                if os.path.exists(pdf_path): os.remove(pdf_path)
    return None

def get_apa_via_doi(doi):
    try:
        r = requests.get(f"https://doi.org/{doi}", headers={"Accept": "text/x-bibliography; style=apa; locale=en-US"}, timeout=30)
        r.raise_for_status()
        txt = (r.text or "").strip()
        return txt if txt else None
    except: return None

def fetch_top_n_pdf_with_apa(query, n=5, down_dict='./'):
    pdfs, apas, cursor = [], [], "*"
    while len(pdfs) < n:
        out, next_cursor = fetch_top_n(query, n=n - len(pdfs), cursor=cursor)
        if not out or next_cursor is None or next_cursor == cursor: break
        for rec in out:
            if rec.get('doi') and rec.get('hasPDF') == 'Y' and rec.get('fullTextUrlList'):
                safe_doi = rec['doi'].replace("/", "_").replace(":", "_")
                urls = rec.get("fullTextUrlList", {}).get("fullTextUrl")
                if urls:
                    docs = load_pdf_from_urls(urls, safe_doi.strip(), down_dict)
                    apa = get_apa_via_doi(rec['doi'])
                    if docs and apa:
                        apas.append(apa)
                        pdfs.append("\n".join(doc.page_content for doc in docs))
        cursor = next_cursor
    return pdfs, apas

def build_section_with_web_search(state: PipelineState):
    sections, references = state.sections, []
    for idx, section in enumerate(sections):
        selected_figures = [{"no": i + 1, "inter": state.figures[i].inter, "desc": state.figures[i].desc} for i in (section.fig_indices or [])] or None
        if section.queries:
            n = max(1, 5 // len(section.queries))
            all_pdfs, all_apas = [], []
            with ThreadPoolExecutor() as pool:
                results = pool.map(lambda q: fetch_top_n_pdf_with_apa(q, n, down_dict=state.output_dir), section.queries)
            for pdfs, apas in results:
                all_pdfs.extend(pdfs); all_apas.extend(apas)
            references.extend(all_apas)
            search_results = [{'apa': apa, 'pdf': pdf} for apa, pdf in zip(all_apas, all_pdfs)]
        else:
            search_results = None
        sections[idx].content = build_section(title=section.title, instr=section.instr, selected_figures=selected_figures, search_results=search_results)
    return state.merge(sections=sections, references=list(dict.fromkeys(references)))

def build_section(title, instr, selected_figures, search_results):
    base_content = f"<TITLE>\n{title}\n</TITLE>\n\n<INSTRUCTION>\n{instr}\n</INSTRUCTION>\n\n<FIGURES>\n{selected_figures}\n</FIGURES>\n\n"
    max_tokens = CONFIG.max_tokens
    if search_results:
        system_tokens = len(build_section_prompt) // 4
        base_tokens = len(base_content) // 4
        search_tokens = len(str(search_results)) // 3
        available_tokens = max_tokens * 0.65 - system_tokens - base_tokens - search_tokens
        if available_tokens < 0:
            target_pct = max(0.1, ((available_tokens + search_tokens) / search_tokens) * 0.9)
            for r in search_results:
                r['pdf'] = r['pdf'][:max(2000, int(len(r['pdf']) * target_pct))]
        full_content = base_content + f"<LITERATURE>\n{search_results}\n</LITERATURE>"
    else:
        full_content = base_content + f"<LITERATURE>\n{None}\n</LITERATURE>"
    messages = [
        SystemMessage(content=build_section_prompt),
        HumanMessage(content=full_content),
    ]
    return CONFIG.invoke_model(messages).content

def compile_report(state: PipelineState):
    all_contents = [section.content or "" for section in state.sections]
    available_tokens = CONFIG.max_tokens * 0.65 - len(consistency_prompt) // 4
    if available_tokens < sum(len(c) for c in all_contents) // 4:
        batches, current_indices, current_tokens = [], [], 0
        for i, content in enumerate(all_contents):
            t = len(content) // 4
            if current_indices and current_tokens + t > available_tokens:
                batches.append(current_indices)
                current_indices, current_tokens = [i], t
            else:
                current_indices.append(i)
                current_tokens += t
        if current_indices:
            batches.append(current_indices)
    else:
        batches = [list(range(len(all_contents)))]
    
    for batch_indices in batches:
        if len(batch_indices) == 1:
            continue
        batch_contents = [all_contents[i] for i in batch_indices]
        messages = [
            SystemMessage(content=consistency_prompt),
            HumanMessage(content=json.dumps({"sections": batch_contents}, ensure_ascii=False)),
        ]
        result = CONFIG.invoke_model(messages, schema=revised_report_state)
        for i, new_content in zip(batch_indices, result.sections):
            all_contents[i] = new_content
    updated_sections = [section.model_copy(update={'content': new_content}) for section, new_content in zip(state.sections, all_contents)]
    
    html_parts = []
    for section, new_content in zip(state.sections, all_contents):
        if section.title:
            html_parts.append(f"<h3>{section.title}</h3>")
        html_parts.append(markdown.markdown(new_content))
        if section.fig_indices:
            for idx in section.fig_indices:
                fig = state.figures[idx]
                html_parts.append(f'<div class="figure"><img src="{Path(fig.path).resolve().as_uri()}" alt="Figure {idx+1}"><p class="figure-caption"><strong>Figure {idx+1}. {fig.capt_title}</strong> {fig.capt_body}</p></div>')
    ref_html = "<h2>References</h2>\n<ol class='references'>\n" + "".join(f"<li>{apa}</li>\n" for apa in state.references) + "</ol>"
    html_parts.append(ref_html)
    output_dir = state.output_dir.strip() if state.output_dir else "."
    HTML(string=html_template.format(
        margin_tb = PAGE_MARGIN_MM,
        margin_lr = PAGE_MARGIN_MM,
        img_max_height = IMG_HEIGHT_MM,
        content="\n\n".join(html_parts)
    )).write_pdf(os.path.join(output_dir, "report.pdf"))
    return state.merge(sections=updated_sections, messages=[AIMessage(content=f'Report saved to: {os.path.join(output_dir, "report.pdf")}')])


def node_after_feedback(state: PipelineState):
    return "figure_description" if state.go_back_to_figure_description else "section_planning"

def node_after_interpretation(state: PipelineState):
    return "figure_description" if state.figures else END


report_builder = StateGraph(PipelineState)
report_builder.add_node("figure_interpretation", figure_interpretation)
report_builder.add_node("figure_description", figure_description)
report_builder.add_node("figure_feedback", figure_feedback)
report_builder.add_node("section_planning", section_planning)
report_builder.add_node("build_section_with_web_search", build_section_with_web_search)
report_builder.add_node("compile_report", compile_report)
report_builder.add_edge(START, "figure_interpretation")
report_builder.add_edge("figure_description", "figure_feedback")
report_builder.add_edge("section_planning", "build_section_with_web_search")
report_builder.add_edge("build_section_with_web_search", "compile_report")
report_builder.add_edge("compile_report", END)
report_builder.add_conditional_edges("figure_interpretation", node_after_interpretation, ["figure_description", END])
report_builder.add_conditional_edges("figure_feedback", node_after_feedback, ["section_planning", "figure_description"])
subgraph_report = report_builder.compile()


def tools_condition_custom(state, messages_key="messages") -> Literal["tools", "replayer", "auto_code_generator"]:
    if state.move_to_auto_code_generator:
        return "auto_code_generator"
    if isinstance(state, list): ai_message = state[-1]
    elif isinstance(state, dict) and (messages := state.get(messages_key, [])): ai_message = messages[-1]
    elif messages := getattr(state, messages_key, []): ai_message = messages[-1]
    else: raise ValueError(f"No messages found in input state to tool_edge: {state}")
    if hasattr(ai_message, "tool_calls") and len(ai_message.tool_calls) > 0:
        return "tools"
    return "replayer"

def should_continue_after_feedback(state: PipelineState):
    return "planner" if state.go_back_to_planner else "validator"

def should_continue_after_validation(state: PipelineState):
    return "executor" if state.validation else "user_feedback_node"

def should_retry_tools(state: PipelineState):
    return "tools" if state.go_back_to_tools else "executor"

def should_generate_report(state: PipelineState):
    return "report_generator" if CONFIG.report_enabled else END

builder = StateGraph(PipelineState)

builder.add_node("planner", planner)
builder.add_node("extract_input_state", extract_input_state)
builder.add_node("validator", validator)
builder.add_node("user_feedback_node", user_feedback_node)
builder.add_node("executor", executor)
builder.add_node("tools", ToolNode(tools))
builder.add_node("tools_checker", tools_checker)
builder.add_node("auto_code_generator", sub_graph)
builder.add_node("replayer", replayer)
builder.add_node("report_generator", subgraph_report)

builder.add_edge(START, "planner")
builder.add_edge("planner", "extract_input_state")
builder.add_edge("extract_input_state", "user_feedback_node")
builder.add_edge("tools", "tools_checker")
builder.add_edge("auto_code_generator", "executor")
builder.add_edge("report_generator", END)

builder.add_conditional_edges("validator", should_continue_after_validation, ["user_feedback_node", "executor"])
builder.add_conditional_edges("user_feedback_node", should_continue_after_feedback, ["validator", "planner"])
builder.add_conditional_edges("executor", tools_condition_custom)
builder.add_conditional_edges("tools_checker", should_retry_tools, ["tools", "executor"])
builder.add_conditional_edges("replayer", should_generate_report, ["report_generator", END])

memory = MemorySaver()
graph = builder.compile(checkpointer=memory)


OPENAI_USAGE_API_URL = "https://api.openai.com/v1/organization/usage/completions"
ANTHROPIC_USAGE_API_URL = "https://api.anthropic.com/v1/organizations/usage_report/messages"

def fetch_openai_usage(admin_api_key: str, start_time: int, end_time: int,
                       bucket_width: str = "1h", models=None, project_ids=None, page_limit: int = 100):
    """Fetch token usage from the OpenAI organization admin API."""
    if not admin_api_key: raise ValueError("admin_api_key is required.")
    if start_time >= end_time: raise ValueError("start_time must be smaller than end_time.")
    headers = {"Authorization": f"Bearer {admin_api_key}", "Content-Type": "application/json"}
    params = {"start_time": start_time, "end_time": end_time, "bucket_width": bucket_width, "limit": page_limit}
    if models: params["models"] = models
    if project_ids: params["project_ids"] = project_ids
    rows, page = [], None
    while True:
        current_params = params.copy()
        if page: current_params["page"] = page
        resp = requests.get(OPENAI_USAGE_API_URL, headers=headers, params=current_params, timeout=60)
        resp.raise_for_status()
        payload = resp.json()
        for bucket in payload.get("data", []):
            for r in bucket.get("results", []):
                input_tokens = int(r.get("input_tokens", 0) or 0)
                output_tokens = int(r.get("output_tokens", 0) or 0)
                total_tokens_raw = r.get("total_tokens")
                total_tokens = int(total_tokens_raw or 0) if total_tokens_raw is not None else input_tokens + output_tokens
                rows.append({"input_tokens": input_tokens, "output_tokens": output_tokens,
                             "total_tokens": total_tokens, "num_model_requests": int(r.get("num_model_requests", 0) or 0)})
        if not payload.get("has_more", False): break
        page = payload.get("next_page")
        if not page: break
    return {
        "input_tokens": sum(x["input_tokens"] for x in rows),
        "output_tokens": sum(x["output_tokens"] for x in rows),
        "total_tokens": sum(x["total_tokens"] for x in rows),
        "num_model_requests": sum(x["num_model_requests"] for x in rows),
    }


def fetch_anthropic_usage(admin_api_key: str, start_time: int, end_time: int,
                          bucket_width: str = "1h", page_limit: int = 100):
    """Fetch token usage from the Anthropic organization admin API.

    Uses GET https://api.anthropic.com/v1/organizations/usage_report/messages
    with RFC-3339 timestamps and standard Anthropic admin auth headers.
    """
    if not admin_api_key: raise ValueError("admin_api_key is required.")
    if start_time >= end_time: raise ValueError("start_time must be smaller than end_time.")

    import datetime as _dt
    def _to_rfc3339(ts: int) -> str:
        return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Anthropic snaps both timestamps DOWN to the nearest bucket boundary.
    # A short run (e.g. 14:04–14:09) snaps both to 14:00, giving a zero-width
    # range that returns 0 buckets.  Fix: snap start DOWN and end UP.
    bucket_seconds = {"1h": 3600, "1d": 86400, "1m": 60}.get(bucket_width, 3600)
    start_aligned = (start_time // bucket_seconds) * bucket_seconds
    end_aligned   = ((end_time + bucket_seconds - 1) // bucket_seconds) * bucket_seconds

    headers = {
        "x-api-key": admin_api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    params = {
        "starting_at": _to_rfc3339(start_aligned),
        "ending_at":   _to_rfc3339(end_aligned),
        "bucket_width": bucket_width,
        "limit": page_limit,
    }
    rows, page = [], None
    while True:
        current_params = params.copy()
        if page: current_params["page"] = page
        resp = requests.get(ANTHROPIC_USAGE_API_URL, headers=headers, params=current_params, timeout=60)
        resp.raise_for_status()
        payload = resp.json()
        for bucket in payload.get("data", []):
            for r in bucket.get("results", []):
                # Anthropic calls it uncached_input_tokens; also include cache reads
                uncached_in  = int(r.get("uncached_input_tokens", 0) or 0)
                cache_read   = int(r.get("cache_read_input_tokens", 0) or 0)
                output       = int(r.get("output_tokens", 0) or 0)
                input_tokens = uncached_in + cache_read
                rows.append({
                    "input_tokens": input_tokens,
                    "output_tokens": output,
                    "total_tokens": input_tokens + output,
                    "num_model_requests": int(r.get("request_count", 0) or 0),
                })
        if not payload.get("has_more", False): break
        page = payload.get("next_page")
        if not page: break
    return {
        "input_tokens": sum(x["input_tokens"] for x in rows),
        "output_tokens": sum(x["output_tokens"] for x in rows),
        "total_tokens": sum(x["total_tokens"] for x in rows),
        "num_model_requests": sum(x["num_model_requests"] for x in rows),
    }


def fetch_usage(provider: str, admin_api_key: str, start_time: int, end_time: int, **kwargs):
    """Provider-aware usage fetch. Dispatches to the correct admin API."""
    if provider == "openai":
        return fetch_openai_usage(admin_api_key, start_time, end_time, **kwargs)
    elif provider == "anthropic":
        return fetch_anthropic_usage(admin_api_key, start_time, end_time, **kwargs)
    else:
        raise ValueError(f"Usage tracking not supported for provider '{provider}'.")


def run_once(
    initial_input: str,
    auto_feedback: str = "good",
    thread_id: str | None = None,
    max_wall_time_s: int = 1800,
    max_steps: int = 2000,
    max_interrupts: int = 50,
    feedback_fn=None,
):
    """
    Run the full graph once, answering every interrupt() call.

    By default each pause is answered automatically with `auto_feedback`, which
    is what the batch/benchmark runs want. Pass `feedback_fn` to answer them
    some other way: it is called with the interrupt payload and returns the
    string sent back to the graph (`ask_terminal` below reads it from stdin).

    Returns a dict with success/timed_out/error_message keys.
    """
    start_time = time.time()
    total_steps = 0
    interrupt_count = 0

    if thread_id is None:
        thread_id = str(uuid.uuid4())

    config = {"configurable": {"thread_id": thread_id}}

    script_dir = os.path.abspath(os.path.dirname(__file__)) if "__file__" in globals() else os.getcwd()
    node_context_path = os.path.join(script_dir, "Knowledge", "node.txt")
    graph_context_path = os.path.join(script_dir, "Knowledge", "graph.txt")
    with open(node_context_path, "r") as f1:
        node_context = f1.read()
    with open(graph_context_path, "r") as f2:
        graph_context = f2.read()

    state_values = {
        "user_input": initial_input,
        "node_context": node_context,
        "graph_context": graph_context,
    }

    def _timeout_or_limit_reached() -> bool:
        nonlocal total_steps, interrupt_count
        now = time.time()
        if now - start_time > max_wall_time_s:
            print("⏰ [TIMEOUT] Max wall time reached.")
            return True
        if total_steps >= max_steps:
            print("⏰ [TIMEOUT] Max steps reached.")
            return True
        if interrupt_count >= max_interrupts:
            print("⏰ [TIMEOUT] Max interrupt count reached.")
            return True
        return False

    def _get_safe_state_snapshot():
        try:
            st = graph.get_state(config)
            return dict(st.values)
        except Exception:
            return dict(state_values)

    if _timeout_or_limit_reached():
        snapshot = _get_safe_state_snapshot()
        snapshot.update({"success": False, "timed_out": True})
        snapshot.setdefault("error_message", "Execution terminated due to timeout/limits.")
        return snapshot

    try:
        stream_ended = True
        last_printed = None
        for event in graph.stream(state_values, config, stream_mode="values"):
            stream_ended = False
            total_steps += 1
            messages = event.get("messages", [])
            if messages:
                last_msg = messages[-1]
                key = (last_msg.type, last_msg.content)
                if key != last_printed:
                    print(f"🧠 {last_msg.type.capitalize()}Message:\n{last_msg.content}")
                    last_printed = key
            state_values = event
            if _timeout_or_limit_reached():
                snapshot = _get_safe_state_snapshot()
                snapshot.update({"success": False, "timed_out": True})
                snapshot.setdefault("error_message", "Execution terminated due to timeout/limits.")
                return snapshot
    except Exception as e:
        print("❌ [EXCEPTION] during initial graph.stream:", repr(e))
        tb = traceback.format_exc()
        print(tb)
        snapshot = _get_safe_state_snapshot()
        snapshot.update({"success": False, "timed_out": False, "error_message": f"Exception during graph.stream: {repr(e)}", "traceback": tb})
        return snapshot

    if "__interrupt__" not in state_values:
        try:
            state = graph.get_state(config)
            if state.next in [None, END, "()", ()] or stream_ended:
                print("\n✅ Graph execution complete.")
                final_values = dict(state.values)
                final_values.setdefault("success", True)
                final_values.setdefault("timed_out", False)
                return final_values
        except Exception as e:
            tb = traceback.format_exc()
            snapshot = dict(state_values)
            snapshot.update({"success": False, "timed_out": False, "error_message": repr(e), "traceback": tb})
            return snapshot

    while True:
        if _timeout_or_limit_reached():
            snapshot = _get_safe_state_snapshot()
            snapshot.update({"success": False, "timed_out": True})
            snapshot.setdefault("error_message", "Execution terminated due to timeout/limits.")
            return snapshot

        if "__interrupt__" not in state_values:
            try:
                state = graph.get_state(config)
                if state.next in [None, END, "()", ()]:
                    print("\n✅ Graph execution complete.")
                    final_values = dict(state.values)
                    final_values.setdefault("success", True)
                    final_values.setdefault("timed_out", False)
                    return final_values
            except Exception as e:
                tb = traceback.format_exc()
                snapshot = dict(state_values)
                snapshot.update({"success": False, "timed_out": False, "error_message": repr(e), "traceback": tb})
                return snapshot

        interrupt_count += 1
        if interrupt_count > max_interrupts:
            print("⏰ [TIMEOUT] Exceeded max_interrupts while handling interrupt.")
            snapshot = _get_safe_state_snapshot()
            snapshot.update({"success": False, "timed_out": True})
            snapshot.setdefault("error_message", "Exceeded max_interrupts.")
            return snapshot

        if feedback_fn is None:
            auto_text = auto_feedback
            print("\n💬 [AUTO] Responding to interrupt with:\n", auto_text)
        else:
            auto_text = feedback_fn(state_values.get("__interrupt__"))

        try:
            stream_ended = True
            for event in graph.stream(Command(resume=auto_text), config, stream_mode="values"):
                stream_ended = False
                total_steps += 1
                messages = event.get("messages", [])
                if messages:
                    last_msg = messages[-1]
                    key = (last_msg.type, last_msg.content)
                    if key != last_printed:
                        print(f"🧠 {last_msg.type.capitalize()}Message:\n{last_msg.content}")
                        last_printed = key
                state_values = event
                if _timeout_or_limit_reached():
                    snapshot = _get_safe_state_snapshot()
                    snapshot.update({"success": False, "timed_out": True})
                    snapshot.setdefault("error_message", "Execution terminated due to timeout/limits.")
                    return snapshot
        except Exception as e:
            print("❌ [EXCEPTION] during interrupt resume:", repr(e))
            tb = traceback.format_exc()
            print(tb)
            snapshot = _get_safe_state_snapshot()
            snapshot.update({"success": False, "timed_out": False, "error_message": f"Exception during interrupt resume: {repr(e)}", "traceback": tb})
            return snapshot


def write_summary(summary_path: str, run_results: list, test_cfg: dict):
    """Write summary.txt with per-run stats and aggregate info."""
    os.makedirs(os.path.dirname(summary_path), exist_ok=True)
    n_runs = len(run_results)
    n_success = sum(1 for r in run_results if r["success"])
    total_runtime = sum(r["runtime_s"] for r in run_results)
    total_tokens = sum(r["total_tokens"] for r in run_results)

    lines = []
    lines.append("=" * 70)
    lines.append("  AGENT TEST SUMMARY")
    lines.append("=" * 70)
    lines.append(f"Generated at     : {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Config           : {test_cfg.get('_config_path', 'N/A')}")
    lines.append(f"Prompt template  : {test_cfg.get('prompt_template', 'N/A')[:120]}...")
    lines.append(f"Model            : {CONFIG.provider} / {CONFIG.model}")
    lines.append(f"N runs           : {n_runs}")
    lines.append(f"Auto feedback    : {test_cfg.get('auto_feedback', 'good')}")
    lines.append("")
    lines.append("-" * 70)
    lines.append("  AGGREGATE")
    lines.append("-" * 70)
    lines.append(f"Success rate     : {n_success}/{n_runs} ({n_success/n_runs*100:.1f}%)")
    lines.append(f"Total runtime    : {total_runtime:.1f}s")
    lines.append(f"Avg runtime      : {total_runtime/n_runs:.1f}s")
    lines.append(f"Total tokens     : {total_tokens}")
    lines.append(f"Avg tokens/run   : {total_tokens/n_runs:.0f}")
    lines.append("")
    lines.append("-" * 70)
    lines.append("  PER-RUN DETAILS")
    lines.append("-" * 70)

    for r in run_results:
        label = f"{r['user_id']} " if r.get("user_id") else ""
        lines.append(f"  {label}Run {r['run_idx']:>3d}  |  "
                     f"{'PASS' if r['success'] else 'FAIL'}  |  "
                     f"{r['runtime_s']:>7.1f}s  |  "
                     f"{r['total_tokens']:>8d} tokens  |  "
                     f"timed_out={r['timed_out']}  |  "
                     f"output_dir={r.get('output_dir', 'N/A')}")
        if r.get("error_message"):
            lines.append(f"           error: {r['error_message'][:200]}")

    lines.append("")
    lines.append("=" * 70)

    with open(summary_path, "w") as f:
        f.write("\n".join(lines))
    print(f"\n📝 Summary written to: {summary_path}")

def build_run_prompt(prompt_template: str, prompt_vars: dict, run_output_dir: str) -> str:
    """Build per-run prompt with a unique output directory.

    - Backward compatible with templates that already use {output_dir}.
    - If template has a trailing output directory block, replace it with run_output_dir.
    """
    fmt_vars = dict(prompt_vars or {})

    if "{output_dir}" in prompt_template:
        fmt_vars["output_dir"] = run_output_dir
        return prompt_template.format(**fmt_vars)

    rendered = prompt_template.format(**fmt_vars)
    lines = rendered.splitlines()

    while lines and not lines[-1].strip():
        lines.pop()

    cue_idx = None
    for i in range(len(lines) - 1, -1, -1):
        if "output directory" in lines[i].lower():
            cue_idx = i
            break

    if cue_idx is not None:
        lines = lines[:cue_idx + 1]
        return "\n".join(lines + ["", run_output_dir])

    return rendered.rstrip() + "\n\nOutput directory is\n\n" + run_output_dir

class Tee:
    """Write to terminal and per-run log file simultaneously."""
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            s.write(data)
        return len(data)

    def flush(self):
        for s in self.streams:
            s.flush()


def ask_terminal(payload) -> str:
    """Answer a human-in-the-loop pause from the terminal (``--interactive``)."""
    print("\n" + "=" * 70)
    print("  THE AGENT IS WAITING FOR YOU")
    print("=" * 70)
    if payload:
        for item in (payload if isinstance(payload, (list, tuple)) else [payload]):
            print(getattr(item, "value", item))
    print("-" * 70)
    print("Type your feedback and press Enter ('good' to approve and continue):")
    try:
        answer = input("> ").strip()
    except EOFError:
        answer = ""
    return answer or "good"


def run_request_file(test_cfg: dict, interactive: bool = False) -> list:
    """Run every prompt in a request file and return one record per run."""
    prompts = test_cfg.get("prompts")
    if not prompts:
        template = test_cfg.get("prompt_template")
        if not template:
            raise RuntimeError(
                "The request file must define either 'prompt_template' (one request) "
                "or 'prompts' (a mapping of label -> request)."
            )
        prompts = {None: template}

    prompt_vars = test_cfg.get("prompt_vars", {})
    root_output_dir = str(test_cfg["root_output_dir"]).strip()
    auto_feedback = test_cfg.get("auto_feedback", "good")
    n_runs = int(test_cfg.get("n_runs", 1))
    max_wall_time_s = test_cfg.get("max_wall_time_s", 15000)
    max_steps = test_cfg.get("max_steps", 2000)
    max_interrupts = test_cfg.get("max_interrupts", 50)
    feedback_fn = ask_terminal if interactive else None

    os.makedirs(root_output_dir, exist_ok=True)
    run_results = []

    for user_id, prompt_template in prompts.items():
        base_dir = root_output_dir if user_id is None else os.path.join(root_output_dir, user_id)
        os.makedirs(base_dir, exist_ok=True)

        for i in range(n_runs):
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            run_output_dir = os.path.join(base_dir, f"run_{i+1}_{timestamp}")
            os.makedirs(run_output_dir, exist_ok=True)
            run_stdout_path = os.path.join(run_output_dir, "stdout.log")
            run_stderr_path = os.path.join(run_output_dir, "stderr.log")

            with open(run_stdout_path, "w") as stdout_file, open(run_stderr_path, "w") as stderr_file:
                stdout_tee = Tee(sys.stdout, stdout_file)
                stderr_tee = Tee(sys.stderr, stderr_file)
                with contextlib.redirect_stdout(stdout_tee), contextlib.redirect_stderr(stderr_tee):
                    banner = f"RUN {i+1}/{n_runs}" + (f"  [{user_id}]" if user_id else "")
                    print(f"\n{'='*20} {banner} {'='*20}")
                    print(f"Output directory for this run: {run_output_dir}")

                    run_prompt = build_run_prompt(
                        prompt_template=prompt_template,
                        prompt_vars=prompt_vars,
                        run_output_dir=run_output_dir,
                    )
                    print("Request sent to the agent:")
                    print(run_prompt)

                    thread_id = f"{user_id}-run-{i+1}" if user_id else f"run-{i+1}"
                    run_start_dt = datetime.datetime.now(timezone.utc)
                    run_start_wall = time.time()

                    try:
                        final_state = run_once(
                            initial_input=run_prompt,
                            auto_feedback=auto_feedback,
                            thread_id=thread_id,
                            max_wall_time_s=max_wall_time_s,
                            max_steps=max_steps,
                            max_interrupts=max_interrupts,
                            feedback_fn=feedback_fn,
                        )
                    except Exception as e:
                        print("💥 [FATAL] run_once itself threw an exception:", repr(e))
                        tb = traceback.format_exc()
                        print(tb)
                        final_state = {
                            "success": False, "timed_out": False,
                            "error_message": f"run_once exception: {repr(e)}", "traceback": tb,
                        }

                    run_end_dt = datetime.datetime.now(timezone.utc)
                    runtime_s = time.time() - run_start_wall

                    total_tokens = 0
                    _has_admin_key = bool(CONFIG.admin_api_key and CONFIG.admin_api_key.lower() not in ("", "unknown"))
                    if CONFIG.provider in ("openai", "anthropic") and _has_admin_key:
                        try:
                            res = fetch_usage(
                                provider=CONFIG.provider,
                                admin_api_key=CONFIG.admin_api_key,
                                start_time=int(run_start_dt.timestamp()),
                                end_time=int(run_end_dt.timestamp()) + 1,  # +1 to avoid edge rounding
                                bucket_width="1h",
                            )
                            total_tokens = res.get("total_tokens", 0)
                        except Exception as e:
                            print(f"⚠️  [TOKEN] Could not fetch token usage: {e}")
                    else:
                        reason = "no admin_api_key set" if not _has_admin_key else f"provider '{CONFIG.provider}' not supported"
                        print(f"ℹ️  [TOKEN] Usage tracking skipped ({reason}).")

                    is_success = final_state.get("success", False)
                    run_results.append({
                        "user_id": user_id,
                        "run_idx": i + 1,
                        "success": is_success,
                        "timed_out": final_state.get("timed_out", False),
                        "runtime_s": runtime_s,
                        "total_tokens": total_tokens,
                        "output_dir": final_state.get("output_dir", run_output_dir),
                        "error_message": final_state.get("error_message"),
                    })

                    print(f"  success       : {is_success}")
                    print(f"  timed_out     : {final_state.get('timed_out')}")
                    print(f"  runtime       : {runtime_s:.1f}s")
                    print(f"  tokens        : {total_tokens}")
                    print(f"  error_message : {final_state.get('error_message')}")
                    n_done = len(run_results)
                    n_ok = sum(1 for r in run_results if r["success"])
                    print(f"  success rate  : {n_ok}/{n_done} ({n_ok/n_done*100:.1f}%)")

    return run_results


def _mask(secret: Optional[str]) -> str:
    """Render a credential as `sk-a…9f3` so logs never carry the real key."""
    if not secret:
        return "(not set)"
    return f"{secret[:5]}…{secret[-3:]}" if len(secret) > 12 else "(set)"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="ChromaPilot — run an epigenomics analysis request through the agent.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Example:\n"
               "  python chromapilot/agent.py -r examples/hiplex_preprocessing.yaml\n",
    )
    parser.add_argument("-r", "--request", "-p", "--prompt", dest="request", required=True,
                        help="Path to the request file (YAML or JSON). See examples/.")
    parser.add_argument("-c", "--config", default=None,
                        help="Path to config.yaml (default: the one in the repository root).")
    parser.add_argument("--interactive", action="store_true",
                        help="Answer the agent's human-review pauses at the terminal "
                             "instead of auto-answering with 'auto_feedback'.")
    args = parser.parse_args()

    request_path = os.path.abspath(args.request)
    if not os.path.exists(request_path):
        raise FileNotFoundError(f"Request file not found: {request_path}")

    with open(request_path, "r") as f:
        test_cfg = yaml.safe_load(f)      # YAML is a superset of JSON
    test_cfg["_config_path"] = request_path

    CONFIG = build_runtime_config(args.config)
    model = CONFIG.llm
    _init_rag_retrievers(CONFIG.llm)

    print("\n=== LangSmith ===")
    print("tracing  :", os.getenv("LANGSMITH_TRACING"))
    print("project  :", os.getenv("LANGSMITH_PROJECT"))
    print("api_key  :", _mask(os.getenv("LANGSMITH_API_KEY")))

    print("\n=== LLM ===")
    print("provider :", CONFIG.provider)
    print("model    :", CONFIG.model)
    print("api_key  :", _mask(CONFIG.api_key))

    run_results = run_request_file(test_cfg, interactive=args.interactive)

    summary_path = os.path.join(str(test_cfg["root_output_dir"]).strip(), "summary.txt")
    write_summary(summary_path, run_results, test_cfg)
