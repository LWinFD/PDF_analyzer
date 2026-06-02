"""
callbacks.py — Pipeline helpers, CSV export, and all Dash callbacks.
"""
import os
import base64
import binascii
import json
import time
import hashlib
from datetime import datetime

import dash
from dash import html, Input, Output, State

from cache import CACHE_FILE, _load_cache, _save_cache
from pdf_extract import extract_text_from_pdf
from llm_clients import LLM_PROVIDER, PARAM_LABELS, analyze_with_llm
from layout import (
    app, long_callback_manager, _build_stepper,
    MAX_UPLOAD_SIZE_MB, TEMP_FOLDER,
    build_results_table, build_totals_bar, build_metadata_table,
)

# ── Inline style for the neutral "queued" badges ─────────────────────────────
# The existing CSS only defines amber / green / red badges (.file-badge.done,
# .file-badge.error).  We use an inline style here so the STYLES block is left
# untouched, while still giving queued files a distinct neutral grey look.
_QUEUED_BADGE_STYLE = {
    "background":   "rgba(139,148,158,0.10)",
    "border":       "1px solid rgba(139,148,158,0.45)",
    "borderRadius": "6px",
    "padding":      "4px 10px",
    "fontFamily":   "'IBM Plex Mono', monospace",
    "fontSize":     "11px",
    "color":        "#8b949e",
    "letterSpacing": "0.5px",
}


# ─────────────────────────────────────────────────────────────────────────────
# CSV EXPORT
# ─────────────────────────────────────────────────────────────────────────────
def results_to_csv(results: list) -> str:
    """
    Convert accumulated results list to CSV.
    Columns: Source File, then each parameter in PARAM_LABELS order.
    One data row per PDF.  The internal _meta and _source_file keys are excluded.
    """
    param_keys = list(PARAM_LABELS.keys())
    header     = ["Source File"] + list(PARAM_LABELS.values())
    lines      = [",".join(f'"{h}"' for h in header)]

    for r in results:
        row = [r.get("_source_file", "")]
        for key in param_keys:
            row.append(r.get(key, "Not stated") or "Not stated")
        lines.append(",".join(f'"{v}"' for v in row))

    return "\n".join(lines)


def metadata_to_csv(results: list) -> str:
    """
    Export per-PDF performance and provenance metadata to CSV.
    One data row per PDF, columns match the metadata table in the UI.
    """
    header = [
        "Source File", "Timestamp", "LLM Provider", "LLM Model",
        "Pages", "OCR Used", "Extraction Time (s)", "LLM Time (s)",
        "Input Tokens", "Output Tokens",
    ]
    lines = [",".join(f'"{h}"' for h in header)]
    for r in results:
        m   = r.get("_meta", {})
        row = [
            r.get("_source_file", ""),
            m.get("timestamp", ""),
            m.get("llm_provider", ""),
            m.get("llm_model", ""),
            str(m.get("page_count", "")),
            m.get("ocr_used", ""),
            str(m.get("extraction_time_s", "")),
            str(m.get("llm_time_s", "")),
            str(m.get("input_tokens", "")),
            str(m.get("output_tokens", "")),
        ]
        lines.append(",".join(f'"{v}"' for v in row))
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# PIPELINE HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def _cleanup(path: str):
    """Silently remove a temp file — never raises."""
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def process_single_pdf(contents: str, filename: str, progress_cb=None, provider: str = None) -> tuple:
    """
    Full pipeline for one PDF: validate → decode → extract → analyze.

    If `progress_cb` is provided it is called with a stepper component at the
    boundaries between phases, so a long_callback can stream live progress.

    Each successful result dict contains a "_meta" key with performance and
    provenance data:
        timestamp        — ISO-style string of when processing finished
        llm_provider     — value of LLM_PROVIDER at call time
        llm_model        — model name as reported by the provider API
        page_count       — number of pages in the PDF
        ocr_used         — "Yes" / "No"
        extraction_time_s — wall-clock seconds spent in pdfplumber/OCR
        llm_time_s       — wall-clock seconds spent in the LLM API call
        input_tokens     — tokens sent to the LLM (0 if provider didn't report)
        output_tokens    — tokens returned by the LLM (0 if not reported)

    Returns:
        (result_dict, "")          on success
        (None, error_message)      on any failure
    """
    if not filename.lower().endswith(".pdf"):
        return None, f"'{filename}' is not a PDF — skipped."

    try:
        _, content_string = contents.split(",", 1)
        file_bytes = base64.b64decode(content_string)
    except (ValueError, binascii.Error) as e:
        return None, f"'{filename}': failed to decode upload — {e}"

    size_mb = len(file_bytes) / (1024 * 1024)
    if size_mb > MAX_UPLOAD_SIZE_MB:
        return None, (
            f"'{filename}' is {size_mb:.1f} MB — exceeds the "
            f"{MAX_UPLOAD_SIZE_MB} MB limit."
        )

    if provider is None:
        provider = LLM_PROVIDER

    # ── Cache check ──────────────────────────────────────────────────────────
    # Hash the raw bytes so renamed copies of the same PDF are still recognised.
    # The key is prefixed with the active LLM provider so the same PDF processed
    # by different providers gets its own independent cache entry.
    cache_key = f"{provider}:{hashlib.sha256(file_bytes).hexdigest()}"
    cache     = _load_cache()
    if cache_key in cache:
        cached = cache[cache_key].copy()
        cached["_source_file"] = filename   # reflect the current filename
        return cached, ""

    temp_path = os.path.join(TEMP_FOLDER, filename)
    try:
        with open(temp_path, "wb") as f:
            f.write(file_bytes)
    except OSError as e:
        return None, f"'{filename}': could not save temp file — {e}"

    # ── Live: Upload finished, Extract is now ACTIVE ────────────────────────
    if progress_cb is not None:
        try:
            progress_cb(_build_stepper(2))
        except (OSError, RuntimeError):
            pass

    try:
        t_extract_start = time.time()
        raw_text, extract_meta = extract_text_from_pdf(temp_path)
        t_extract = round(time.time() - t_extract_start, 2)
    except (RuntimeError, OSError) as e:
        _cleanup(temp_path)
        return None, f"'{filename}': extraction failed — {e}"

    if not raw_text.strip():
        _cleanup(temp_path)
        return None, (
            f"'{filename}': no text could be extracted. "
            "The file may be image-only without OCR installed."
        )

    # ── Live: Extract finished, Analyze is now ACTIVE ───────────────────────
    if progress_cb is not None:
        try:
            progress_cb(_build_stepper(3))
        except (OSError, RuntimeError):
            pass

    try:
        t_llm_start = time.time()
        result, llm_meta = analyze_with_llm(raw_text, provider=provider)
        t_llm = round(time.time() - t_llm_start, 2)
    except (RuntimeError, ValueError, json.JSONDecodeError) as e:
        _cleanup(temp_path)
        return None, f"'{filename}': LLM analysis failed — {e}"

    _cleanup(temp_path)
    result["_source_file"] = filename
    result["_meta"] = {
        "timestamp":         datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "llm_provider":      provider,
        "llm_model":         llm_meta.get("model_name", ""),
        "page_count":        extract_meta.get("page_count", 0),
        "ocr_used":          "Yes" if extract_meta.get("ocr_used") else "No",
        "extraction_time_s": t_extract,
        "llm_time_s":        t_llm,
        "input_tokens":      llm_meta.get("input_tokens", 0),
        "output_tokens":     llm_meta.get("output_tokens", 0),
    }

    # ── Write to cache ───────────────────────────────────────────────────────
    # Reload before writing so parallel batches don't overwrite each other's
    # entries — re-reading first keeps as many prior entries as possible.
    try:
        fresh_cache = _load_cache()
        fresh_cache[cache_key] = result.copy()
        _save_cache(fresh_cache)
    except (OSError, json.JSONDecodeError):
        pass   # cache write failure is non-fatal — result still returned

    return result, ""


# ─────────────────────────────────────────────────────────────────────────────
# CALLBACKS
# ─────────────────────────────────────────────────────────────────────────────

@app.callback(
    Output("filename-display", "children", allow_duplicate=True),
    Input("upload-pdf", "filename"),
    prevent_initial_call=True,
)
def show_queued_files(filenames):
    """
    Fast callback — fires the moment files are selected, BEFORE any processing.
    Reads only `dcc.Upload.filename` so it never waits on the pipeline.
    Each filename is shown as a neutral grey "queued" badge with a hourglass
    glyph.  The long_callback later overwrites these with done/error badges.
    """
    if not filenames:
        return ""
    if not isinstance(filenames, list):
        filenames = [filenames]

    badges = [
        html.Div(
            ["⧖ ", fn],
            style=_QUEUED_BADGE_STYLE,
            title=fn,
        )
        for fn in filenames
    ]
    return html.Div(badges, className="file-queue")


@app.long_callback(
    output=[
        Output("pipeline-results", "data"),
        Output("pipeline-status",  "data"),
        Output("filename-display", "children", allow_duplicate=True),
    ],
    inputs=Input("upload-pdf", "contents"),
    state=[
        State("upload-pdf",        "filename"),
        State("pipeline-results",  "data"),
        State("selected-provider", "data"),
    ],
    progress=Output("stepper-row", "children", allow_duplicate=True),
    running=[
        # Force the dcc.Loading spinner over the results card to stay visible
        # for the full duration of the pipeline (not just the brief moment a
        # regular callback would normally trigger it).
        (Output("loading-results", "display"), "show", "auto"),
    ],
    manager=long_callback_manager,
    prevent_initial_call=True,
)
def run_pipeline(set_progress, contents_list, filenames_list, existing_results, selected_provider):
    """
    Process every uploaded PDF and APPEND new results to the accumulated store.

    Runs in a subprocess via DiskcacheManager so that `set_progress` can push
    live stepper updates while extraction + LLM analysis are in flight.

    Stepper progression pushed by this callback:

        START    ──► step 1 (Upload  active)   ← pushed right here, before loop
        per file ──► step 2 (Extract active)   ← pushed inside process_single_pdf
                 ──► step 3 (Analyze active)   ← pushed inside process_single_pdf
        END      ──► step 4 (all done green)   ← pushed right here, after loop
                                                 (or error state if anything failed)

    Existing results from previous uploads are preserved.
    Only the Reset button clears them.
    """
    if not contents_list:
        return existing_results or [], {"step": 0, "error": ""}, ""

    # Dash may pass a single item (not a list) when only one file is selected
    if not isinstance(contents_list, list):
        contents_list  = [contents_list]
        filenames_list = [filenames_list]

    # ── Live: Upload is the active step the instant we start ────────────────
    # Without this push the stepper would stay on "idle" until the first file
    # finishes decoding + saving, which on small PDFs happens too fast to see.
    try:
        set_progress(_build_stepper(1))
    except (OSError, RuntimeError):
        pass

    accumulated = list(existing_results or [])
    errors      = []
    badges      = []

    for contents, filename in zip(contents_list, filenames_list):
        # process_single_pdf pushes step 2 (before extract) and step 3 (before
        # LLM) via this same set_progress callback.
        result, error = process_single_pdf(
            contents, filename,
            progress_cb=set_progress,
            provider=selected_provider or LLM_PROVIDER,
        )
        if result:
            accumulated.append(result)
            badges.append(html.Div(["+ ", filename], className="file-badge done"))
        else:
            errors.append(error)
            badges.append(html.Div(["! ", filename], className="file-badge error",
                                   title=error))

    # ── Decide the final status ─────────────────────────────────────────────
    if errors and not accumulated:
        # Every file failed — leave the stepper on Analyze with a red X.
        status      = {"step": 3, "error": " | ".join(errors)}
        final_step  = 3
        final_error = True
    elif errors:
        # Mixed batch — overall we did reach Done, but flag the issues.
        status      = {"step": 4, "error": "Some files failed: " + " | ".join(errors)}
        final_step  = 4
        final_error = False
    else:
        # Everything succeeded.
        status      = {"step": 4, "error": ""}
        final_step  = 4
        final_error = False

    # ── Live: paint the final state immediately ─────────────────────────────
    # If consecutive batches both end with status={step:4,error:""}, the data
    # store doesn't change, so `update_stepper` would NOT re-fire.  Pushing
    # this last set_progress guarantees the user sees the final state
    # regardless.
    try:
        set_progress(_build_stepper(final_step, error=final_error))
    except (OSError, RuntimeError):
        pass

    return accumulated, status, html.Div(badges, className="file-queue")


@app.callback(
    Output("stepper-row",  "children"),
    Output("error-banner", "children"),
    Output("error-banner", "style"),
    Input("pipeline-status", "data"),
)
def update_stepper(status):
    """Sync the step indicator and error banner with pipeline status."""
    if not status:
        return _build_stepper(0), "", {"display": "none"}
    step  = status.get("step", 0)
    error = status.get("error", "")
    if error:
        banner = html.Div([html.Strong("Error: "), error], className="alert-error")
        # If we still reached step 4 (some succeeded), keep the stepper green
        # and only show the error banner; otherwise paint the failed step red.
        if step >= 4:
            return _build_stepper(step), banner, {"display": "block"}
        return _build_stepper(step, error=True), banner, {"display": "block"}
    return _build_stepper(step), "", {"display": "none"}


@app.callback(
    Output("results-card", "children"),
    Output("results-card", "style"),
    Input("pipeline-results", "data"),
)
def update_results(results):
    """
    Render the accumulated results table whenever the store changes.

    Results are NEVER auto-cleared — they accumulate until Reset is clicked.
    Uploading more PDFs adds new rows to the same table.
    """
    if not results:
        return "", {"display": "none"}

    n     = len(results)
    table = build_results_table(results)

    card_content = [
        html.Div("03 - Extracted Parameters", className="section-label"),
        html.Div([
            html.Span(str(n)),
            f" PDF{'s' if n != 1 else ''} analyzed — upload more above to add rows",
        ], className="results-summary"),
        table,
        html.Div([
            html.Button("Download Results", id="btn-download", className="btn-primary",  n_clicks=0),
            html.Button("Reset & Clear",    id="btn-reset",    className="btn-danger",   n_clicks=0),
        ], className="btn-row"),
    ]
    return card_content, {"display": "block"}


@app.callback(
    Output("download-csv", "data"),
    Input("btn-download",  "n_clicks"),
    State("pipeline-results", "data"),
    prevent_initial_call=True,
)
def download_csv(n_clicks, results):
    """Export drilling-parameter results to CSV (no metadata columns)."""
    if not results or not n_clicks:
        return dash.no_update
    return {
        "content":  results_to_csv(results),
        "filename": "well_report_results.csv",
        "type":     "text/csv",
    }


@app.callback(
    Output("meta-card",         "style"),
    Output("meta-totals-bar",   "children"),
    Output("meta-table-content","children"),
    Input("pipeline-results",   "data"),
)
def update_meta(results):
    """Show/refresh the metadata card whenever results change."""
    if not results:
        return {"display": "none"}, "", ""
    return (
        {"display": "block"},
        build_totals_bar(results),
        build_metadata_table(results),
    )


@app.callback(
    Output("meta-section",    "style"),
    Output("btn-toggle-meta", "children"),
    Input("btn-toggle-meta",  "n_clicks"),
    prevent_initial_call=True,
)
def toggle_meta(n_clicks):
    """Expand or collapse the metadata table on every button click."""
    if n_clicks % 2 == 1:
        return {"display": "block"}, "Hide Metadata ▴"
    return {"display": "none"}, "Show Metadata ▾"


@app.callback(
    Output("download-metadata-csv", "data"),
    Input("btn-download-meta",      "n_clicks"),
    State("pipeline-results",       "data"),
    prevent_initial_call=True,
)
def download_metadata_csv(n_clicks, results):
    """Export per-PDF performance and provenance metadata to CSV."""
    if not results or not n_clicks:
        return dash.no_update
    return {
        "content":  metadata_to_csv(results),
        "filename": "well_report_metadata.csv",
        "type":     "text/csv",
    }


@app.callback(
    Output("upload-pdf",       "contents"),
    Output("pipeline-results", "data",     allow_duplicate=True),
    Output("pipeline-status",  "data",     allow_duplicate=True),
    Output("filename-display", "children", allow_duplicate=True),
    Output("error-banner",     "style",    allow_duplicate=True),
    Output("btn-toggle-meta",  "n_clicks"),
    Input("btn-reset",         "n_clicks"),
    prevent_initial_call=True,
)
def reset_app(n_clicks):
    """
    Clear ALL results and reset to the initial state.
    Also resets the metadata toggle so it starts collapsed for the next batch.
    Only triggered by user clicking 'Reset & Clear'.
    """
    if not n_clicks:
        return (dash.no_update,) * 6
    return None, [], {"step": 0, "error": ""}, "", {"display": "none"}, 0


@app.callback(
    Output("pipeline-results",  "data",      allow_duplicate=True),
    Output("cache-load-status", "children"),
    Output("cache-load-status", "className"),
    Input("btn-load-cache",     "n_clicks"),
    State("pipeline-results",   "data"),
    prevent_initial_call=True,
)
def load_from_cache(n_clicks, existing_results):
    """
    Read well_cache.json from disk and merge its valid entries into the
    pipeline-results store — no re-upload required.

    Reading a local JSON file is instantaneous, so this uses a regular
    @app.callback rather than a long_callback.

    Validation: every cache entry must contain all keys defined in PARAM_LABELS.
    Entries that fail this check are skipped and their count reported in the
    status message.

    Merging: valid entries are appended to whatever results are already
    displayed, matching the same accumulation behaviour as uploading new PDFs.

    Status messages:
        ok      — "Loaded N results from cache."  (green)
        warning — empty / missing file / no valid entries  (amber-dim)
    """
    if not n_clicks:
        return dash.no_update, "", "cache-status"

    cache = _load_cache()

    if not cache:
        return (
            dash.no_update,
            f"Cache file not found or empty ({CACHE_FILE}).",
            "cache-status warning",
        )

    expected_keys = set(PARAM_LABELS.keys())
    valid_entries: list = []
    skipped = 0

    for entry in cache.values():
        if not isinstance(entry, dict):
            skipped += 1
            continue
        # Entry must have every expected parameter key.
        if not expected_keys.issubset(entry.keys()):
            skipped += 1
            continue
        valid_entries.append(entry)

    if not valid_entries:
        detail = f" ({skipped} entries skipped — missing expected keys)" if skipped else ""
        return (
            dash.no_update,
            f"No valid entries found in cache{detail}.",
            "cache-status warning",
        )

    accumulated = list(existing_results or []) + valid_entries

    n   = len(valid_entries)
    msg = f"Loaded {n} result{'s' if n != 1 else ''} from cache."
    if skipped:
        msg += f" ({skipped} skipped — missing keys)"

    return accumulated, msg, "cache-status ok"


@app.callback(
    Output("selected-provider", "data"),
    Input("provider-dropdown",  "value"),
    prevent_initial_call=True,
)
def sync_provider_store(value):
    """Keep the selected-provider store in sync with the UI dropdown."""
    return value
