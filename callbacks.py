"""callbacks.py — Pipeline helpers, CSV export, and all Dash callbacks."""
import os
import csv as _csv
import base64
import binascii
import json
import time
import hashlib
from datetime import datetime

import dash
from dash import html, Input, Output, State, MATCH

from cache import CACHE_FILE, VALIDATION_FILE, _load_cache, _save_cache
from pdf_extract import extract_text_from_pdf
from llm_clients import (
    LLM_PROVIDER, PARAM_LABELS, analyze_with_llm,
    load_known_cements, KNOWN_CEMENTS_FILE,
)
from layout import (
    app, long_callback_manager, _build_stepper,
    MAX_UPLOAD_SIZE_MB, TEMP_FOLDER,
    build_results_table, build_totals_bar, build_metadata_table, build_diff_section,
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
# VALIDATION HELPERS
# ─────────────────────────────────────────────────────────────────────────────
# Normalized substrings (hyphens/spaces → underscores, lowercase) that uniquely
# identify each sample PDF regardless of its full filename.
_SAMPLE_WELL_PATTERNS = ("6608_11_3", "6608_10_13")
_SAMPLE_WELL_LABELS = {"6608_11_3": "6608/11-3", "6608_10_13": "6608/10-13"}

# Reverse map: CSV display header → PARAM_LABELS key (built once at import time)
_LABEL_TO_KEY = {v: k for k, v in PARAM_LABELS.items()}


def _norm_filename(name: str) -> str:
    """Lowercase the stem, replace hyphens/spaces with underscores."""
    stem = os.path.splitext(name)[0]
    return stem.lower().replace("-", "_").replace(" ", "_")


def _match_sample_wells(results: list) -> dict:
    """
    Return {pattern: result_dict} for sample wells present in results.

    Matching is done on normalised filename stems, so it is robust to
    case differences and hyphen/space/underscore variations.
    """
    found = {}
    for r in results:
        src_norm = _norm_filename(r.get("_source_file", ""))
        for pat in _SAMPLE_WELL_PATTERNS:
            if pat in src_norm and pat not in found:
                found[pat] = r
    return found


def _build_validation_content(results: list):
    """
    Build the body of the validation card.

    Reads Validation.csv, matches rows to results by normalised filename,
    then compares each of the 15 parameters.  Returns a list of Dash
    components ready to be placed in validation-content.children.
    """
    if not os.path.exists(VALIDATION_FILE):
        return [html.Div(
            f"Validation file not found: {VALIDATION_FILE}",
            className="alert-error",
        )]

    # Load expected rows keyed by normalised source filename
    expected_by_norm: dict = {}
    try:
        with open(VALIDATION_FILE, newline="", encoding="utf-8-sig") as f:
            reader = _csv.DictReader(f)
            for row in reader:
                key = _norm_filename(row.get("Source File", ""))
                expected_by_norm[key] = row
    except (OSError, _csv.Error) as exc:
        return [html.Div(f"Could not read {VALIDATION_FILE}: {exc}", className="alert-error")]

    matched = _match_sample_wells(results)
    if not matched:
        return [html.Div("No sample wells found in the current results.", className="alert-error")]

    sections = []
    param_keys = list(PARAM_LABELS.keys())

    for pat in _SAMPLE_WELL_PATTERNS:   # deterministic order
        if pat not in matched:
            continue
        result = matched[pat]
        label = _SAMPLE_WELL_LABELS[pat]
        src_norm = _norm_filename(result.get("_source_file", ""))

        # Find the expected row whose normalised filename matches the result
        exp_row = expected_by_norm.get(src_norm)
        if exp_row is None:
            # Fallback: try substring matching (handles minor filename differences)
            for k, v in expected_by_norm.items():
                if pat in k:
                    exp_row = v
                    break

        if exp_row is None:
            sections.append(html.Div(
                f"No expected-value row found for well {label} in {VALIDATION_FILE}.",
                className="alert-error",
            ))
            continue

        rows = []
        passed = 0
        for key in param_keys:
            col_label = PARAM_LABELS[key]
            extracted = (result.get(key) or "Not stated").strip()
            expected_val = (exp_row.get(col_label) or "").strip()

            if not expected_val:
                badge = html.Span("—", style={"color": "var(--text-muted)"})
                expected_display = html.Td("—", style={"color": "var(--text-muted)", "fontStyle": "italic"})
            elif extracted.lower() == expected_val.lower():
                passed += 1
                badge = html.Span("PASS", className="val-badge-pass")
                expected_display = html.Td(expected_val)
            else:
                badge = html.Span("FAIL", className="val-badge-fail")
                expected_display = html.Td(expected_val, style={"color": "var(--red)"})

            rows.append(html.Tr([
                html.Td(col_label),
                expected_display,
                html.Td(extracted),
                html.Td(badge),
            ]))

        scored_total = sum(1 for key in param_keys if (exp_row.get(PARAM_LABELS[key]) or "").strip())
        failed = scored_total - passed

        sections.append(html.Div(f"Well: {label}", className="val-well-header"))
        sections.append(html.Div(
            html.Table([
                html.Thead(html.Tr([
                    html.Th("Parameter"),
                    html.Th("Expected"),
                    html.Th("Extracted"),
                    html.Th("Result"),
                ])),
                html.Tbody(rows),
            ], className="val-table"),
            className="results-wrapper",
        ))
        sections.append(html.Div([
            html.Span(f"{passed}/{scored_total}", className="ok" if failed == 0 else "fail"),
            f" parameters matched for well {label}.",
            " All correct!" if failed == 0 else f" {failed} mismatch{'es' if failed > 1 else ''}.",
        ], className="val-summary"))

    return sections


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
    header = ["Source File"] + list(PARAM_LABELS.values())
    lines = [",".join(f'"{h}"' for h in header)]

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
        "Pages", "OCR Used", "OCR Pages Done", "OCR Pages Skipped",
        "Extraction Time (s)", "LLM Time (s)",
        "Input Tokens", "Output Tokens",
    ]
    lines = [",".join(f'"{h}"' for h in header)]
    for r in results:
        m = r.get("_meta", {})
        row = [
            r.get("_source_file", ""),
            m.get("timestamp", ""),
            m.get("llm_provider", ""),
            m.get("llm_model", ""),
            str(m.get("page_count", "")),
            m.get("ocr_used", ""),
            str(m.get("ocr_pages_done",    0)),
            str(m.get("ocr_pages_skipped", 0)),
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


def process_single_pdf(contents: str, filename: str, progress_cb=None, provider: str = None,
                       force_reprocess: bool = False, write_cache: bool = True) -> tuple:
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
    cache_key = f"{provider}:{hashlib.sha256(file_bytes).hexdigest()}"
    if not force_reprocess:
        cache = _load_cache()
        if cache_key in cache:
            cached = cache[cache_key].copy()
            cached["_source_file"] = filename
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
        "ocr_pages_done":    extract_meta.get("ocr_pages_done",    0),
        "ocr_pages_skipped": extract_meta.get("ocr_pages_skipped", 0),
        "extraction_time_s": t_extract,
        "llm_time_s":        t_llm,
        "input_tokens":      llm_meta.get("input_tokens", 0),
        "output_tokens":     llm_meta.get("output_tokens", 0),
    }

    # ── Write to cache ───────────────────────────────────────────────────────
    # Skipped when write_cache=False (reprocess mode: user must Accept first).
    # Reload before writing so parallel batches don't overwrite each other's
    # entries — re-reading first keeps as many prior entries as possible.
    if write_cache:
        try:
            fresh_cache = _load_cache()
            fresh_cache[cache_key] = result.copy()
            _save_cache(fresh_cache)
        except (OSError, json.JSONDecodeError):
            pass

    return result, ""


def _compute_diff(filename: str, old_result: dict, new_result: dict) -> dict | None:
    """
    Compare two result dicts on PARAM_LABELS keys.

    Returns a diff dict for build_diff_section, or None if nothing changed.
    """
    changed = []
    unchanged = []
    for key, label in PARAM_LABELS.items():
        old_val = (old_result.get(key) or "Not stated").strip()
        new_val = (new_result.get(key) or "Not stated").strip()
        if old_val != new_val:
            changed.append({"key": key, "label": label, "cached_val": old_val, "new_val": new_val})
        else:
            unchanged.append({"key": key, "label": label, "val": old_val})

    if not changed:
        return None

    return {
        "filename":   filename,
        "cached_ts":  (old_result.get("_meta") or {}).get("timestamp", "—"),
        "new_ts":     (new_result.get("_meta") or {}).get("timestamp", "—"),
        "changed":    changed,
        "unchanged":  unchanged,
    }


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


@app.callback(
    Output("pending-upload",  "data"),
    Output("cache-hit-info",  "data"),
    Output("run-trigger",     "data"),
    Output("cache-hit-modal", "is_open"),
    Input("upload-pdf",       "contents"),
    State("upload-pdf",       "filename"),
    State("selected-provider", "data"),
    prevent_initial_call=True,
)
def on_upload(contents_list, filenames_list, provider):
    """
    Fast pre-check that fires the moment files land in the upload zone.

    Hashes each file's bytes, checks the cache, then either:
      • opens the modal (if any file already has a cached result), or
      • fires run-trigger immediately (no modal needed).
    The pipeline long_callback is triggered by run-trigger, NOT by the upload
    itself, so this callback acts as the gatekeeper.
    """
    if not contents_list:
        return dash.no_update, [], dash.no_update, False

    if not isinstance(contents_list, list):
        contents_list = [contents_list]
        filenames_list = [filenames_list]

    provider = provider or LLM_PROVIDER
    cache = _load_cache()
    hits = []

    for contents, filename in zip(contents_list, filenames_list):
        if not filename.lower().endswith(".pdf"):
            continue
        try:
            _, content_string = contents.split(",", 1)
            file_bytes = base64.b64decode(content_string)
        except (ValueError, binascii.Error):
            continue
        cache_key = f"{provider}:{hashlib.sha256(file_bytes).hexdigest()}"
        if cache_key in cache:
            m = (cache[cache_key].get("_meta") or {})
            hits.append({
                "filename":  filename,
                "cache_key": cache_key,
                "cached_ts": m.get("timestamp", "—"),
                "provider":  provider,
            })

    pending = {"contents": contents_list, "filenames": filenames_list, "provider": provider}

    if hits:
        return pending, hits, dash.no_update, True
    return pending, [], {"mode": "normal", "ts": time.time()}, False


@app.long_callback(
    output=[
        Output("pipeline-results", "data"),
        Output("pipeline-status",  "data"),
        Output("filename-display", "children", allow_duplicate=True),
        Output("diff-store",       "data"),
    ],
    inputs=Input("run-trigger", "data"),
    state=[
        State("pending-upload",    "data"),
        State("pipeline-results",  "data"),
        State("cache-hit-info",    "data"),
        State("selected-provider", "data"),
    ],
    progress=Output("stepper-row", "children", allow_duplicate=True),
    manager=long_callback_manager,
    prevent_initial_call=True,
)
def run_pipeline(set_progress, trigger, pending, existing_results, hit_info, selected_provider):
    """
    Process uploaded PDFs and append results to the accumulated store.

    Modes (set by run-trigger):
      "normal"    — standard flow; cache hits served from cache automatically.
      "reprocess" — force fresh LLM call for cache-hit files; compare with
                    the old cached result and populate diff-store.
    """
    if not trigger or not pending:
        return existing_results or [], {"step": 0, "error": ""}, "", []

    mode = trigger.get("mode", "normal")
    contents_list = pending.get("contents",  [])
    filenames_list = pending.get("filenames", [])
    provider_val = pending.get("provider") or selected_provider or LLM_PROVIDER

    if not contents_list:
        return existing_results or [], {"step": 0, "error": ""}, "", []

    if not isinstance(contents_list, list):
        contents_list = [contents_list]
        filenames_list = [filenames_list]

    # Names of files that were cache hits (used only in reprocess mode)
    hit_filenames = {h["filename"] for h in (hit_info or [])}

    # Snapshot cached results BEFORE we overwrite them (reprocess only)
    cache_snapshots: dict = {}
    if mode == "reprocess" and hit_info:
        snap_cache = _load_cache()
        for h in hit_info:
            ck = h.get("cache_key", "")
            if ck and ck in snap_cache:
                cache_snapshots[h["filename"]] = snap_cache[ck].copy()

    try:
        set_progress(_build_stepper(1))
    except (OSError, RuntimeError):
        pass

    accumulated = list(existing_results or [])
    errors = []
    badges = []
    diff_new_results = []  # reprocessed results held for accept/reject
    diff_old_results = []  # corresponding cached snapshots (may be None)
    diff_cache_keys = []  # corresponding cache keys for write-on-accept

    for contents, filename in zip(contents_list, filenames_list):
        force = (mode == "reprocess") and (filename in hit_filenames)
        result, error = process_single_pdf(
            contents, filename,
            progress_cb=set_progress,
            provider=provider_val,
            force_reprocess=force,
            write_cache=not force,
        )
        if result:
            if force:
                diff_new_results.append(result)
                diff_old_results.append(cache_snapshots.get(filename))
                try:
                    _, cs = contents.split(",", 1)
                    fb = base64.b64decode(cs)
                    diff_cache_keys.append(f"{provider_val}:{hashlib.sha256(fb).hexdigest()}")
                except (ValueError, binascii.Error):
                    diff_cache_keys.append(None)
                badges.append(html.Div(["~ ", filename, " — pending"], className="file-badge pending"))
            else:
                accumulated.append(result)
                badges.append(html.Div(["+ ", filename], className="file-badge done"))
        else:
            errors.append(error)
            badges.append(html.Div(["! ", filename], className="file-badge error", title=error))

    # Build diff objects (only include files with actual changes)
    diffs = []
    for new_r, old_r in zip(diff_new_results, diff_old_results):
        if old_r is None:
            continue
        diff = _compute_diff(new_r.get("_source_file", ""), old_r, new_r)
        if diff is not None:
            diffs.append(diff)

    diff_store_out = {
        "diffs":       diffs,
        "new_results": diff_new_results,
        "old_results": diff_old_results,
        "cache_keys":  diff_cache_keys,
    } if diff_new_results else {}

    all_attempted = accumulated + diff_new_results
    if errors and not all_attempted:
        status, final_step, final_error = {"step": 3, "error": " | ".join(errors)}, 3, True
    elif errors:
        status, final_step, final_error = {"step": 4, "error": "Some files failed: " + " | ".join(errors)}, 4, False
    else:
        status, final_step, final_error = {"step": 4, "error": ""}, 4, False

    try:
        set_progress(_build_stepper(final_step, error=final_error))
    except (OSError, RuntimeError):
        pass

    return accumulated, status, html.Div(badges, className="file-queue"), diff_store_out


@app.callback(
    Output("modal-body-content", "children"),
    Input("cache-hit-info",      "data"),
)
def render_modal_body(hit_info):
    """Populate the modal with the list of files that have cached results."""
    if not hit_info:
        return ""
    items = [
        html.Div([
            html.Span(h["filename"],  className="modal-hit-filename"),
            html.Span(
                f"Cached {h['cached_ts']}  ·  {h['provider']}",
                className="modal-hit-meta",
            ),
        ], className="modal-hit-item")
        for h in hit_info
    ]
    n = len(hit_info)
    label = f"{n} PDF{'s' if n != 1 else ''}"
    return html.Div([
        html.P(
            f"{label} already {'have' if n != 1 else 'has'} cached results "
            "with the selected provider:",
            className="modal-question",
            style={"marginBottom": "10px", "marginTop": "0"},
        ),
        html.Div(items),
        html.P(
            "Load from cache (instant, no API call) or reprocess to get fresh "
            "results and see a side-by-side comparison of any differences?",
            className="modal-question",
        ),
    ])


@app.callback(
    Output("pipeline-results",  "data",      allow_duplicate=True),
    Output("cache-hit-modal",   "is_open",   allow_duplicate=True),
    Output("filename-display",  "children",  allow_duplicate=True),
    Output("pipeline-status",   "data",      allow_duplicate=True),
    Input("btn-use-cache",      "n_clicks"),
    State("cache-hit-info",     "data"),
    State("pipeline-results",   "data"),
    prevent_initial_call=True,
)
def use_cache_click(n_clicks, hit_info, existing_results):
    """Read cache-hit files directly from disk — no subprocess needed."""
    if not n_clicks:
        return dash.no_update, dash.no_update, dash.no_update, dash.no_update

    cache = _load_cache()
    loaded = []
    badges = []

    for h in (hit_info or []):
        ck = h.get("cache_key", "")
        fn = h.get("filename",  "")
        if ck and ck in cache:
            r = cache[ck].copy()
            r["_source_file"] = fn
            loaded.append(r)
            badges.append(html.Div(["+ ", fn], className="file-badge done"))

    accumulated = list(existing_results or []) + loaded
    return (
        accumulated,
        False,
        html.Div(badges, className="file-queue"),
        {"step": 4, "error": ""},
    )


@app.callback(
    Output("run-trigger",     "data",    allow_duplicate=True),
    Output("cache-hit-modal", "is_open", allow_duplicate=True),
    Input("btn-reprocess",    "n_clicks"),
    prevent_initial_call=True,
)
def reprocess_click(n_clicks):
    """Close the cache-hit modal and fire run-trigger in reprocess mode."""
    if not n_clicks:
        return dash.no_update, dash.no_update
    return {"mode": "reprocess", "ts": time.time()}, False


@app.callback(
    Output("diff-content",    "children"),
    Output("diff-card",       "style"),
    Output("diff-action-row", "style"),
    Input("diff-store",       "data"),
)
def render_diff(diff_store):
    """Show section 05 whenever reprocessed results are awaiting accept/reject."""
    if not diff_store:
        return "", {"display": "none"}, {"display": "none"}
    diffs = diff_store.get("diffs",       []) if isinstance(diff_store, dict) else []
    has_pending = bool(diff_store.get("new_results")) if isinstance(diff_store, dict) else False
    if not has_pending and not diffs:
        return "", {"display": "none"}, {"display": "none"}
    return build_diff_section(diffs), {"display": "block"}, {"display": "flex"}


@app.callback(
    Output("pipeline-results",  "data",      allow_duplicate=True),
    Output("diff-store",        "data",      allow_duplicate=True),
    Output("cache-load-status", "children",  allow_duplicate=True),
    Output("cache-load-status", "className", allow_duplicate=True),
    Input("btn-accept-diff",    "n_clicks"),
    State("diff-store",         "data"),
    State("pipeline-results",   "data"),
    prevent_initial_call=True,
)
def accept_diff(n_clicks, diff_store, existing_results):
    """Accept reprocessed results: write to cache and add to pipeline results."""
    if not n_clicks or not diff_store:
        return (dash.no_update,) * 4
    new_results = diff_store.get("new_results", []) or []
    cache_keys = diff_store.get("cache_keys",  []) or []
    try:
        fresh_cache = _load_cache()
        for r, ck in zip(new_results, cache_keys):
            if ck:
                fresh_cache[ck] = r.copy()
        _save_cache(fresh_cache)
    except (OSError, json.JSONDecodeError):
        pass
    accumulated = list(existing_results or []) + new_results
    n = len(new_results)
    return accumulated, {}, f"Accepted {n} reprocessed result{'s' if n != 1 else ''}.", "cache-status ok"


@app.callback(
    Output("pipeline-results",  "data",      allow_duplicate=True),
    Output("diff-store",        "data",      allow_duplicate=True),
    Output("cache-load-status", "children",  allow_duplicate=True),
    Output("cache-load-status", "className", allow_duplicate=True),
    Input("btn-reject-diff",    "n_clicks"),
    State("diff-store",         "data"),
    State("pipeline-results",   "data"),
    prevent_initial_call=True,
)
def reject_diff(n_clicks, diff_store, existing_results):
    """Reject reprocessed results: discard new data, fall back to cached results."""
    if not n_clicks or not diff_store:
        return (dash.no_update,) * 4
    old_results = [r for r in (diff_store.get("old_results", []) or []) if r is not None]
    accumulated = list(existing_results or []) + old_results
    n = len(old_results)
    return accumulated, {}, f"Rejected — kept {n} cached result{'s' if n != 1 else ''}.", "cache-status ok"


@app.callback(
    Output("validate-btn-row", "style"),
    Input("pipeline-results",  "data"),
)
def show_validate_btn(results):
    """Show the Validate button only when at least one sample well is in results."""
    if results and _match_sample_wells(results):
        return {"display": "flex"}
    return {"display": "none"}


@app.callback(
    Output("validation-content", "children"),
    Output("validation-card",    "style"),
    Input("btn-validate",        "n_clicks"),
    State("pipeline-results",    "data"),
    prevent_initial_call=True,
)
def run_validation(n_clicks, results):
    """Build and display the validation results table."""
    if not n_clicks or not results:
        return "", {"display": "none"}
    return _build_validation_content(results), {"display": "block"}


@app.callback(
    Output("validation-card", "style", allow_duplicate=True),
    Input("btn-close-validation", "n_clicks"),
    prevent_initial_call=True,
)
def close_validation(n_clicks):
    """Hide the validation card without clearing the results."""
    if not n_clicks:
        return dash.no_update
    return {"display": "none"}


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
    step = status.get("step", 0)
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

    n = len(results)
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


# ─────────────────────────────────────────────────────────────────────────────
# CEMENT LEARNING (human-in-the-loop)
# ─────────────────────────────────────────────────────────────────────────────
def _collect_new_cements(results: list) -> list:
    """Return unique new cement names in results not already in the known list."""
    if not results:
        return []
    known = {c.lower() for c in load_known_cements()}
    seen = set()
    names = []
    for r in results:
        for raw in (r.get("new_cements") or []):
            name = (raw or "").strip()
            key = name.lower()
            if name and key not in known and key not in seen:
                seen.add(key)
                names.append(name)
    return names


def _append_known_cement(name: str) -> None:
    """Append a cement name to known_cements.txt if it is not already present."""
    name = (name or "").strip()
    if not name or name.lower() in {c.lower() for c in load_known_cements()}:
        return
    try:
        with open(KNOWN_CEMENTS_FILE, "a", encoding="utf-8") as f:
            f.write(name + "\n")
    except OSError:
        pass


def _cement_row(name: str) -> html.Div:
    """Build one confirmation row (message + Confirm + Dismiss) for a cement name."""
    return html.Div(
        [
            html.Span(
                ["New cement name found: ", html.Strong(f"'{name}'"),
                 ". Add to the known list?"],
                className="cement-msg",
            ),
            html.Div([
                html.Button(
                    "Confirm",
                    id={"type": "cement-confirm", "index": name},
                    className="btn-success", n_clicks=0,
                ),
                html.Button(
                    "Dismiss",
                    id={"type": "cement-dismiss", "index": name},
                    className="btn-danger", n_clicks=0,
                ),
            ], className="btn-row"),
        ],
        id={"type": "cement-row", "index": name},
        className="cement-row",
    )


@app.callback(
    Output("cement-card",         "style"),
    Output("cement-card-content", "children"),
    Input("pipeline-results",     "data"),
)
def update_cement_card(results):
    """Show the cement card with a row per newly discovered cement name."""
    names = _collect_new_cements(results)
    if not names:
        return {"display": "none"}, ""
    return {"display": "block"}, [_cement_row(n) for n in names]


@app.callback(
    Output({"type": "cement-row",     "index": MATCH}, "style"),
    Input({"type": "cement-confirm",  "index": MATCH}, "n_clicks"),
    Input({"type": "cement-dismiss",  "index": MATCH}, "n_clicks"),
    State({"type": "cement-confirm",  "index": MATCH}, "id"),
    prevent_initial_call=True,
)
def resolve_cement(confirm_clicks, dismiss_clicks, confirm_id):
    """Confirm (append to file) or dismiss a single cement name, hiding its row."""
    triggered = dash.ctx.triggered_id
    if not triggered:
        return dash.no_update
    if triggered.get("type") == "cement-confirm":
        if not confirm_clicks:
            return dash.no_update
        _append_known_cement(confirm_id["index"])
    elif not dismiss_clicks:
        return dash.no_update
    return {"display": "none"}


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
    Output("meta-table-content", "children"),
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
    Output("upload-pdf",         "contents"),
    Output("pipeline-results",   "data",     allow_duplicate=True),
    Output("pipeline-status",    "data",     allow_duplicate=True),
    Output("filename-display",   "children", allow_duplicate=True),
    Output("error-banner",       "style",    allow_duplicate=True),
    Output("btn-toggle-meta",    "n_clicks"),
    Output("diff-store",         "data",     allow_duplicate=True),
    Output("pending-upload",     "data",     allow_duplicate=True),
    Output("cache-hit-info",     "data",     allow_duplicate=True),
    Output("validation-card",    "style",    allow_duplicate=True),
    Output("validation-content", "children", allow_duplicate=True),
    Output("cache-load-status",  "children", allow_duplicate=True),
    Output("cache-load-status",  "className", allow_duplicate=True),
    Input("btn-reset",           "n_clicks"),
    prevent_initial_call=True,
)
def reset_app(n_clicks):
    """Clear ALL results and reset to the initial state."""
    if not n_clicks:
        return (dash.no_update,) * 13
    return (
        None, [], {"step": 0, "error": ""}, "", {"display": "none"}, 0, {},
        None, [], {"display": "none"}, "", "", "cache-status",
    )


@app.callback(
    Output("pipeline-results",  "data",      allow_duplicate=True),
    Output("cache-load-status", "children",  allow_duplicate=True),
    Output("cache-load-status", "className", allow_duplicate=True),
    Output("cache-msg-timer",   "disabled",  allow_duplicate=True),
    Output("cache-msg-timer",   "n_intervals"),
    Input("btn-load-cache",     "n_clicks"),
    State("pipeline-results",   "data"),
    prevent_initial_call=True,
)
def load_from_cache(n_clicks, existing_results):
    """
    Read well_cache.json and merge its valid entries into pipeline-results.

    Deduplication: entries whose _source_file is already in the results
    table are skipped so repeated clicks never add duplicate rows.

    Auto-dismiss: on success the status message is cleared after 2.5 s
    by enabling cache-msg-timer (a dcc.Interval), which fires once and
    is disabled again by the auto_clear_cache_msg callback.
    """
    _no_timer = (dash.no_update, "", "cache-status", True, dash.no_update)
    if not n_clicks:
        return _no_timer

    cache = _load_cache()

    if not cache:
        return (
            dash.no_update,
            f"Cache file not found or empty ({CACHE_FILE}).",
            "cache-status warning",
            True, dash.no_update,
        )

    expected_keys = set(PARAM_LABELS.keys())
    existing_sources = {r.get("_source_file") for r in (existing_results or [])}
    valid_entries: list = []
    skipped = 0

    for entry in cache.values():
        if not isinstance(entry, dict):
            skipped += 1
            continue
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
            True, dash.no_update,
        )

    new_entries = [e for e in valid_entries if e.get("_source_file") not in existing_sources]

    if not new_entries:
        return (
            dash.no_update,
            "Already loaded — no new entries.",
            "cache-status warning",
            False, 0,
        )

    accumulated = list(existing_results or []) + new_entries

    n = len(new_entries)
    msg = f"Loaded {n} result{'s' if n != 1 else ''} from cache."
    if skipped:
        msg += f" ({skipped} skipped — missing keys)"

    return accumulated, msg, "cache-status ok", False, 0


@app.callback(
    Output("cache-load-status", "children",  allow_duplicate=True),
    Output("cache-load-status", "className", allow_duplicate=True),
    Output("cache-msg-timer",   "disabled",  allow_duplicate=True),
    Input("cache-msg-timer",    "n_intervals"),
    prevent_initial_call=True,
)
def auto_clear_cache_msg(_n):
    """Clear the 'Loaded N results' status text after the 2.5 s timer fires."""
    return "", "cache-status", True


@app.callback(
    Output("selected-provider", "data"),
    Input("provider-dropdown",  "value"),
    prevent_initial_call=True,
)
def sync_provider_store(value):
    """Keep the selected-provider store in sync with the UI dropdown."""
    return value
