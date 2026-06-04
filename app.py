"""
app.py — Well Report Analyzer (entry point).

Thin entry point: loads .env, applies the httpx proxy workaround, ensures the
temp folder exists, imports all modules (registering Dash callbacks as a
side-effect), then starts the server.

Re-exports all public names so that `import app as A` in test_app.py still
resolves A.function_name, A.CONSTANT, etc. without changes to the test suite.

Run with:
    python app.py

Requires a .env file with your API key (copy from .env.example).
"""
import os
from pathlib import Path

# ── Load .env file ────────────────────────────────────────────────────────────
# Must happen before any local module import so that env-var-dependent
# initialisation code in pdf_extract.py (Tesseract / Poppler paths) reads
# the correct values.
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).parent / ".env")
except ImportError:
    print("[WARNING] python-dotenv not installed. Reading API keys from system env only.")

# ── Corporate proxy workaround ───────────────────────────────────────────────
# httpx 0.28+ removed the 'proxies' parameter. Corporate proxy tools (e.g.
# Zscaler) on managed machines still inject it via the old API, which crashes
# the anthropic and openai SDKs on initialisation. Patching both Client and
# AsyncClient here drops the argument before httpx processes it.
try:
    import httpx
    _orig_client = httpx.Client.__init__
    _orig_async_client = httpx.AsyncClient.__init__

    def _client_init(self, *a, **kw):
        kw.pop("proxies", None)
        _orig_client(self, *a, **kw)

    def _async_client_init(self, *a, **kw):
        kw.pop("proxies", None)
        _orig_async_client(self, *a, **kw)

    httpx.Client.__init__ = _client_init
    httpx.AsyncClient.__init__ = _async_client_init
except (ImportError, AttributeError, TypeError):
    pass

# ── Re-export everything so `import app as A` keeps working in test_app.py ───
from cache import CACHE_FILE, VALIDATION_FILE, _load_cache, _save_cache    # noqa: E402
from pdf_extract import OCR_THRESHOLD, _format_table, extract_text_from_pdf  # noqa: E402
from llm_clients import (                                                  # noqa: E402
    LLM_PROVIDER, PARAM_LABELS, ANALYSIS_PROMPT, _parse_llm_json, analyze_with_llm,
    load_known_cements, KNOWN_CEMENTS_FILE,
)
from layout import (                                                       # noqa: E402
    app, server, STYLES, MAX_UPLOAD_SIZE_MB, TEMP_FOLDER, long_callback_manager,
    _build_stepper, _value_td, build_results_table, build_metadata_table, build_totals_bar,
)
import callbacks  # registers all @app.callback / @app.long_callback functions  # noqa: E402
from callbacks import (                                                    # noqa: E402
    results_to_csv, metadata_to_csv, _cleanup, process_single_pdf,
    _QUEUED_BADGE_STYLE,
    show_queued_files, run_pipeline, update_stepper, update_results,
    download_csv, update_meta, toggle_meta, download_metadata_csv,
    reset_app, load_from_cache, sync_provider_store,
    accept_diff, reject_diff,
    show_validate_btn, run_validation, close_validation,
    _match_sample_wells, _build_validation_content,
)

# Public surface re-exported for `import app as A` in test_app.py. Declared so
# tools recognise these as intentional re-exports rather than unused imports.
__all__ = [
    "callbacks",
    "CACHE_FILE", "VALIDATION_FILE", "_load_cache", "_save_cache",
    "OCR_THRESHOLD", "_format_table", "extract_text_from_pdf",
    "LLM_PROVIDER", "PARAM_LABELS", "ANALYSIS_PROMPT", "_parse_llm_json", "analyze_with_llm",
    "load_known_cements", "KNOWN_CEMENTS_FILE",
    "app", "server", "STYLES", "MAX_UPLOAD_SIZE_MB", "TEMP_FOLDER", "long_callback_manager",
    "_build_stepper", "_value_td", "build_results_table", "build_metadata_table", "build_totals_bar",
    "results_to_csv", "metadata_to_csv", "_cleanup", "process_single_pdf", "_QUEUED_BADGE_STYLE",
    "show_queued_files", "run_pipeline", "update_stepper", "update_results", "download_csv",
    "update_meta", "toggle_meta", "download_metadata_csv", "reset_app", "load_from_cache",
    "sync_provider_store", "accept_diff", "reject_diff", "show_validate_btn", "run_validation",
    "close_validation", "_match_sample_wells", "_build_validation_content",
]

# ── Ensure temp folder exists ─────────────────────────────────────────────────
os.makedirs(TEMP_FOLDER, exist_ok=True)

# ─────────────────────────────────────────────────────────────────────────────
# RUN
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("-" * 55)
    print("  Well Report Analyzer")
    print(f"  LLM Provider : {LLM_PROVIDER}")
    print(f"  Temp folder  : {os.path.abspath(TEMP_FOLDER)}")
    print("-" * 55)
    print("  Open http://127.0.0.1:8050 in your browser")
    print("-" * 55)
    app.run(debug=False, host="0.0.0.0", port=8050)
