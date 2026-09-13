import logging
import io
import sys
import subprocess
import functools
import os
import inspect
import pandas as pd
from typing import Callable, Dict, Any, Optional
import threading


class TeeFile:
  """writes to files and caches at the same time, ensuring order"""

  def __init__(self, file_obj, buffer_obj):
    self.file = file_obj
    self.buffer = buffer_obj
    self._lock = threading.Lock()

  def write(self, text):
    with self._lock:
      self.file.write(text)
      self.buffer.write(text)
      self.file.flush()

  def flush(self):
    with self._lock:
      self.file.flush()

  def fileno(self):
    return self.file.fileno()

  # def __getattr__(self, name):
  #     return getattr(self.file, name)


class LogCapture:
  """Unified Log Manager: write all output to the same log file"""

  def __init__(self, log_file_path: str):
    self.log_file_path = log_file_path
    self.capture_buffer = io.StringIO()
    self.log_file = None
    self.log_handler = None
    self.original_level = None
    self.original_subprocess_run = None
    self.original_subprocess_call = None
    self.tee_unified = None

  def __enter__(self):
    os.makedirs(os.path.dirname(self.log_file_path), exist_ok=True)
    self.log_file = open(self.log_file_path, 'w')
    self.tee_unified = TeeFile(self.log_file, self.capture_buffer)

    logger = logging.getLogger()
    self.original_level = logger.level
    self.log_handler = logging.StreamHandler(self.tee_unified)
    logger.setLevel(logging.INFO)
    logger.addHandler(self.log_handler)

    self.original_stdout = sys.stdout
    self.original_stderr = sys.stderr
    sys.stdout = self.tee_unified
    sys.stderr = self.tee_unified

    self._patch_subprocess()
    return self

  def __exit__(self, exc_type, exc_val, exc_tb):
    if self.original_stdout:
      sys.stdout = self.original_stdout
    if self.original_stderr:
      sys.stderr = self.original_stderr

    if self.log_handler:
      logger = logging.getLogger()
      logger.removeHandler(self.log_handler)
      if self.original_level is not None:
        logger.setLevel(self.original_level)

    if self.original_subprocess_call:
      subprocess.call = self.original_subprocess_call
    if self.original_subprocess_run:
      subprocess.run = self.original_subprocess_run

    if self.log_file:
      self.log_file.close()

  def _patch_subprocess(self):
    self.original_subprocess_run = subprocess.run
    self.original_subprocess_call = subprocess.call

    def patched_run(*args, **kwargs):
      command = args[0] if args else kwargs.get('args', 'Unknown command')
      print(f"[SUBPROCESS] Executing: {command}")

      caller_wants_stdout = (
          kwargs.get("stdout") == subprocess.PIPE
          or kwargs.get("stderr") == subprocess.PIPE
          or kwargs.get("capture_output", False)
      )
      if caller_wants_stdout:
        result = self.original_subprocess_run(*args, **kwargs)
      else:
        kwargs['stdout'] = self.tee_unified
        kwargs['stderr'] = self.tee_unified
        kwargs['text'] = True
        result = self.original_subprocess_run(*args, **kwargs)
      print(f"[SUBPROCESS] Return code: {result.returncode}")
      return result

    def patched_call(*args, **kwargs):
      command = args[0] if args else kwargs.get('args', 'Unknown command')
      print(f"[SUBPROCESS CALL] Executing: {command}")

      caller_wants_capture = (
          kwargs.get("stdout") == subprocess.PIPE
          or kwargs.get("stderr") == subprocess.PIPE
          or kwargs.get("capture_output", False)
      )

      if not caller_wants_capture:
        kwargs.setdefault("stdout", self.log_file)
        kwargs.setdefault("stderr", self.log_file)
        kwargs.setdefault("text", True)
        self.log_file.flush()

      result = self.original_subprocess_call(*args, **kwargs)
      print(f"[SUBPROCESS CALL] Return code: {result}")
      return result

    subprocess.run = patched_run
    subprocess.call = patched_call

  def get_captured_logs(self) -> str:
    if self.capture_buffer:
      return self.capture_buffer.getvalue()
    return ""


def auto_log_capture(output_param: str = 'out_dir', sample_param: str = 'sample', log_subfolder: str = 'log'):
  def decorator(func: Callable) -> Callable:
    @functools.wraps(func)
    def wrapped(*args, **kwargs):
      sig = inspect.signature(func)
      bound_args = sig.bind(*args, **kwargs)
      bound_args.apply_defaults()
      if output_param not in bound_args.arguments:
        return func(*args, **kwargs)
      if sample_param in bound_args.arguments:
        sample = bound_args.arguments[sample_param]
      else:
        sample = None
      output_dic = bound_args.arguments[output_param]
      sample = bound_args.arguments.get(sample_param)
      log_filename = f"{func.__name__}.log" if sample is None else f"{sample}_{func.__name__}.log"
      log_out = os.path.join(output_dic, log_subfolder, log_filename)
      
      try:
        with LogCapture(log_out) as capture:
          result = func(*args, **kwargs)
          if isinstance(result, dict):
            # result['log_out'] = capture.get_captured_logs()
            result['log_out'] = os.path.abspath(log_out)
          else:
            result = {
                'result': result,
                # 'log_out': capture.get_captured_logs()
                'log_out': os.path.abspath(log_out)
            }
          return result
      except Exception as e:
        return {'result': None, 'log_out': log_out, 'error': str(e)}
    return wrapped
  return decorator
