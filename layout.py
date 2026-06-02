"""
layout.py — Dash app object, CSS, UI component builders, and app layout.

Also owns the long_callback_manager (DiskcacheManager) and the shared
configuration constants MAX_UPLOAD_SIZE_MB and TEMP_FOLDER, which are
defined here rather than in callbacks.py to keep the import graph acyclic
(callbacks.py imports `app` from this module).
"""
import os

import dash
from dash import dcc, html, DiskcacheManager
import dash_bootstrap_components as dbc
import diskcache

from cache import CACHE_FILE
from llm_clients import PARAM_LABELS, LLM_PROVIDER

# ─────────────────────────────────────────────────────────────────────────────
# SHARED CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────
MAX_UPLOAD_SIZE_MB = 50         # reject files larger than this
TEMP_FOLDER        = "./temp"   # temporary folder for uploaded PDFs

# ── Long-callback (background) manager ────────────────────────────────────────
# Backs the `run_pipeline` long_callback so it can run in a subprocess and push
# live step-progress updates to the stepper via `set_progress`.  The cache
# folder is created automatically.  Diskcache + multiprocess are required.
CACHE_FOLDER = "./.cache"
os.makedirs(CACHE_FOLDER, exist_ok=True)
long_callback_manager = DiskcacheManager(diskcache.Cache(CACHE_FOLDER))


# ─────────────────────────────────────────────────────────────────────────────
# CSS — industrial dark theme with amber accents
# ─────────────────────────────────────────────────────────────────────────────
STYLES = """
:root {
    --bg-primary:   #0d1117;
    --bg-surface:   #161b22;
    --bg-card:      #1c2230;
    --border:       #30363d;
    --amber:        #e6a817;
    --amber-dim:    #9b6f0e;
    --amber-glow:   rgba(230,168,23,0.10);
    --red:          #f85149;
    --red-dim:      rgba(248,81,73,0.12);
    --green:        #3fb950;
    --green-dim:    rgba(63,185,80,0.18);
    --blue:         #58a6ff;
    --text-primary: #e6edf3;
    --text-muted:   #8b949e;
    --mono:         'IBM Plex Mono', monospace;
    --sans:         'IBM Plex Sans', sans-serif;
    --display:      'Syne', sans-serif;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
    background: var(--bg-primary);
    color: var(--text-primary);
    font-family: var(--sans);
    min-height: 100vh;
    background-image:
        radial-gradient(ellipse at 20% 0%, rgba(230,168,23,0.05) 0%, transparent 55%),
        radial-gradient(ellipse at 80% 100%, rgba(63,185,80,0.03) 0%, transparent 55%);
}
.app-header {
    background: var(--bg-surface); border-bottom: 1px solid var(--border);
    padding: 18px 40px; display: flex; align-items: center; gap: 14px;
}
.app-icon     { font-size: 26px; filter: drop-shadow(0 0 8px var(--amber)); }
.app-title    { font-family: var(--display); font-size: 22px; font-weight: 800;
                color: var(--text-primary); letter-spacing: -0.4px; }
.app-subtitle { font-family: var(--mono); font-size: 10px; color: var(--text-muted);
                letter-spacing: 2px; text-transform: uppercase; margin-top: 2px; }
.main-content { max-width: 1200px; margin: 0 auto; padding: 36px 24px; }
.section-card {
    background: var(--bg-card); border: 1px solid var(--border);
    border-radius: 12px; padding: 26px 28px; margin-bottom: 22px;
}
.section-label {
    font-family: var(--mono); font-size: 10px; letter-spacing: 2px;
    color: var(--text-muted); text-transform: uppercase; margin-bottom: 18px;
    padding-bottom: 10px; border-bottom: 1px solid var(--border);
}
.upload-zone {
    border: 2px dashed var(--border); border-radius: 10px; padding: 40px 28px;
    text-align: center; cursor: pointer; background: var(--bg-card);
    transition: border-color 0.2s, background 0.2s;
}
.upload-zone:hover { border-color: var(--amber); background: var(--amber-glow); }
.upload-icon  { font-size: 36px; display: block; margin-bottom: 14px; }
.upload-title { font-family: var(--display); font-size: 17px; font-weight: 700;
                color: var(--text-primary); margin-bottom: 7px; }
.upload-hint  { font-family: var(--mono); font-size: 11px; color: var(--text-muted);
                letter-spacing: 1px; }

/* ── PIPELINE STEPPER ─────────────────────────────────────────────────────── */
.stepper { display: flex; align-items: center; padding: 8px 0; }
.step    { display: flex; flex-direction: column; align-items: center; flex: 1; }

.step-dot {
    width: 32px; height: 32px; border-radius: 50%;
    background: var(--bg-card); border: 2px solid var(--border);
    display: flex; align-items: center; justify-content: center;
    font-family: var(--mono); font-size: 13px; font-weight: 600;
    color: var(--text-muted);
    position: relative; z-index: 1;
    transition: border-color 0.3s ease, background 0.3s ease,
                color 0.3s ease, box-shadow 0.3s ease;
}

/* Active = currently in progress — pulses with amber glow + spinning ring */
@keyframes step-pulse {
    0%, 100% { box-shadow: 0 0 0 0   rgba(230,168,23,0.55); }
    50%      { box-shadow: 0 0 0 9px rgba(230,168,23,0);    }
}
@keyframes step-spin { to { transform: rotate(360deg); } }

.step-dot.active {
    border-color: var(--amber);
    background: var(--amber-glow);
    color: var(--amber);
    animation: step-pulse 1.6s ease-out infinite;
}
.step-dot.active::after {
    content: '';
    position: absolute;
    top: -5px; left: -5px; right: -5px; bottom: -5px;
    border-radius: 50%;
    border: 2px solid transparent;
    border-top-color: var(--amber);
    border-right-color: var(--amber);
    animation: step-spin 1.1s linear infinite;
    pointer-events: none;
}

/* Done = completed — solid green with checkmark */
.step-dot.done {
    border-color: var(--green);
    background: var(--green-dim);
    color: var(--green);
    box-shadow: 0 0 0 3px rgba(63,185,80,0.08);
}

/* Error = something failed at this step */
.step-dot.error {
    border-color: var(--red);
    background: var(--red-dim);
    color: var(--red);
}

.step-label {
    font-family: var(--mono); font-size: 9px; letter-spacing: 1px;
    color: var(--text-muted); margin-top: 10px; text-transform: uppercase;
    transition: color 0.3s ease;
}
.step-label.active { color: var(--amber); font-weight: 600; }
.step-label.done   { color: var(--green); }
.step-label.error  { color: var(--red); }

.step-line {
    flex: 1; height: 3px; background: var(--border);
    margin-top: -18px; border-radius: 2px;
    transition: background 0.5s ease, box-shadow 0.5s ease;
}
.step-line.done {
    background: var(--green);
    box-shadow: 0 0 8px rgba(63,185,80,0.35);
}

/* ── FILE BADGES ──────────────────────────────────────────────────────────── */
.file-queue { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }
.file-badge {
    background: var(--amber-glow); border: 1px solid var(--amber-dim);
    border-radius: 6px; padding: 4px 10px;
    font-family: var(--mono); font-size: 11px; color: var(--amber);
}
.file-badge.error { background: var(--red-dim); border-color: var(--red); color: var(--red); }
.file-badge.done  { background: rgba(63,185,80,0.1); border-color: var(--green); color: var(--green); }

/* ── RESULTS TABLE ────────────────────────────────────────────────────────── */
.results-wrapper { overflow-x: auto; }
.results-table   { width: 100%; border-collapse: collapse; font-size: 13px;
                   min-width: 900px; }
.results-table th {
    font-family: var(--mono); font-size: 9px; letter-spacing: 1.5px;
    color: var(--text-muted); text-transform: uppercase; padding: 10px 14px;
    text-align: left; border-bottom: 2px solid var(--border);
    white-space: nowrap; font-weight: 500; background: var(--bg-card);
}
.results-table td {
    padding: 12px 14px; border-bottom: 1px solid rgba(48,54,61,0.6);
    vertical-align: middle;
}
.results-table td:first-child {
    font-family: var(--mono); font-size: 11px; color: var(--text-muted);
    max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.val-not-stated { color: var(--amber-dim) !important; font-style: italic; }
.val-yes        { color: var(--green) !important; font-weight: 600; }
.val-no         { color: var(--red) !important; }
.val-class      { color: var(--amber) !important; font-weight: 600; }
.val-name       { color: var(--blue) !important; font-weight: 500; }
.results-table tr:last-child td { border-bottom: none; }
.results-table tr:hover td { background: rgba(255,255,255,0.02); }
.results-summary {
    font-family: var(--mono); font-size: 11px; color: var(--text-muted);
    margin-bottom: 14px; letter-spacing: 0.5px;
}
.results-summary span { color: var(--amber); font-weight: 600; }
.alert-error {
    background: var(--red-dim); border: 1px solid var(--red);
    border-radius: 8px; padding: 12px 16px; color: var(--red);
    font-family: var(--mono); font-size: 12px; margin-bottom: 18px;
}
.btn-primary {
    background: var(--amber); color: #0d1117; border: none;
    border-radius: 8px; padding: 10px 20px; font-family: var(--mono);
    font-size: 12px; font-weight: 600; cursor: pointer;
    letter-spacing: 0.5px; transition: opacity 0.2s;
}
.btn-primary:hover { opacity: 0.85; }
.btn-danger {
    background: transparent; color: var(--red); border: 1px solid var(--red);
    border-radius: 8px; padding: 10px 20px; font-family: var(--mono);
    font-size: 12px; cursor: pointer; letter-spacing: 0.5px;
    transition: background 0.2s;
}
.btn-danger:hover { background: var(--red-dim); }
.btn-row { display: flex; gap: 12px; margin-top: 18px; flex-wrap: wrap;
           align-items: center; }
.hint-text { font-family: var(--mono); font-size: 11px; color: var(--text-muted);
             margin-left: auto; }

/* ── METADATA SECTION ─────────────────────────────────────────────────────── */
.meta-toggle-btn {
    background: transparent; border: 1px solid var(--border);
    color: var(--text-muted); border-radius: 6px; padding: 7px 16px;
    font-family: var(--mono); font-size: 11px; cursor: pointer;
    letter-spacing: 0.5px; transition: border-color 0.2s, color 0.2s;
}
.meta-toggle-btn:hover { border-color: var(--amber); color: var(--amber); }

.btn-secondary {
    background: transparent; color: var(--text-muted);
    border: 1px solid var(--border); border-radius: 8px;
    padding: 10px 20px; font-family: var(--mono); font-size: 12px;
    cursor: pointer; letter-spacing: 0.5px;
    transition: border-color 0.2s, color 0.2s;
}
.btn-secondary:hover { border-color: var(--amber); color: var(--amber); }

.totals-bar  { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 20px; }
.totals-chip {
    background: var(--bg-primary); border: 1px solid var(--border);
    border-radius: 8px; padding: 10px 16px; min-width: 110px;
}
.chip-label {
    display: block; font-family: var(--mono); font-size: 9px;
    letter-spacing: 1.5px; text-transform: uppercase;
    color: var(--text-muted); margin-bottom: 4px;
}
.chip-value {
    display: block; font-family: var(--mono); font-size: 16px;
    font-weight: 600; color: var(--amber);
}

.meta-table { width: 100%; border-collapse: collapse; font-size: 12px; min-width: 900px; }
.meta-table th {
    font-family: var(--mono); font-size: 9px; letter-spacing: 1.5px;
    color: var(--text-muted); text-transform: uppercase; padding: 8px 12px;
    text-align: left; border-bottom: 1px solid var(--border);
    white-space: nowrap; font-weight: 500;
}
.meta-table td {
    padding: 10px 12px; border-bottom: 1px solid rgba(48,54,61,0.4);
    color: var(--text-muted); font-family: var(--mono); font-size: 11px;
}
.meta-table td:first-child { color: var(--text-primary); max-width: 180px;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.meta-table tr:last-child td { border-bottom: none; }
.meta-table tr:hover td { background: rgba(255,255,255,0.02); }
.meta-value-time   { color: var(--blue)  !important; }
.meta-value-tokens { color: var(--green) !important; }
.meta-ctrl-row { display: flex; gap: 10px; align-items: center; margin-bottom: 0; }
.meta-divider  { height: 1px; background: var(--border); margin: 18px 0; }

/* ── CACHE LOAD ROW ───────────────────────────────────────────────────────── */
.cache-row { display: flex; align-items: center; margin-top: 14px; gap: 14px; flex-wrap: wrap; }
.cache-status {
    font-family: var(--mono); font-size: 11px; color: var(--text-muted);
    letter-spacing: 0.3px; transition: color 0.2s;
}
.cache-status.ok      { color: var(--green); }
.cache-status.warning { color: var(--amber-dim); }

/* ── PROVIDER DROPDOWN — dark-theme overrides ────────────────────────────── */
/* Targets Dash's react-select wrapper rendered inside #provider-dropdown.    */
#provider-dropdown .Select-control {
    background: var(--bg-primary) !important;
    border-color: var(--border) !important;
    box-shadow: none !important;
}
#provider-dropdown .Select-control:hover { border-color: var(--amber) !important; }
#provider-dropdown .Select-value-label,
#provider-dropdown .Select-placeholder   { color: var(--text-primary) !important; }
#provider-dropdown .Select-arrow         { border-top-color: var(--text-muted) !important; }
#provider-dropdown .Select-menu-outer {
    background: var(--bg-card) !important;
    border: 1px solid var(--border) !important;
    z-index: 9999 !important;
    /* Open upward so the menu never covers elements below the dropdown. */
    top: auto !important;
    bottom: 100% !important;
    margin-top: 0 !important;
    margin-bottom: 1px !important;
    border-radius: 8px 8px 0 0 !important;
}
#provider-dropdown .Select-option {
    background: var(--bg-card) !important;
    color: var(--text-primary) !important;
    font-family: var(--mono);
    font-size: 12px;
}
#provider-dropdown .Select-option.is-focused {
    background: var(--amber-glow) !important;
    color: var(--amber) !important;
}
#provider-dropdown .Select-option.is-selected {
    background: var(--bg-surface) !important;
    color: var(--amber) !important;
    font-weight: 600;
}
"""


# ─────────────────────────────────────────────────────────────────────────────
# DASH APP SETUP
# ─────────────────────────────────────────────────────────────────────────────
app = dash.Dash(
    __name__,
    external_stylesheets=[
        dbc.themes.BOOTSTRAP,
        "https://fonts.googleapis.com/css2?family=Syne:wght@400;700;800"
        "&family=IBM+Plex+Mono:wght@400;500"
        "&family=IBM+Plex+Sans:wght@300;400;500&display=swap",
    ],
    title="Well Report Analyzer",
    long_callback_manager=long_callback_manager,
)
server = app.server

# Inject custom CSS via index_string (compatible with all Dash versions)
app.index_string = app.index_string.replace(
    "</head>", f"<style>{STYLES}</style></head>"
)


# ─────────────────────────────────────────────────────────────────────────────
# UI COMPONENT BUILDERS
# ─────────────────────────────────────────────────────────────────────────────
def _build_stepper(active_step: int, error: bool = False) -> html.Div:
    """
    Build the 4-step pipeline progress indicator.

    Semantics:
        active_step  meaning                                  visual
        ───────────  ───────────────────────────────────────  ─────────────────
        0            idle (nothing happening yet)             all gray dots
        1            Upload  is currently running             1 amber, rest gray
        2            Extract is currently running             1 green, 2 amber
        3            Analyze is currently running             1+2 green, 3 amber
        4 (or more)  everything finished                      ALL green ✓
        any + error  the step at `active_step` failed         that step shown red

    Returns a SINGLE html.Div (not a list) so that Dash's `set_progress` can't
    mistake the 4 dots + 3 lines for "a tuple of 7 values for 7 separate
    progress outputs".  Earlier this caused only the first element (Upload)
    to render during live updates.  Wrapping everything in one Div makes the
    payload unambiguous: one value for the one `Output("stepper-row","children")`.
    """
    steps = ["Upload", "Extract", "Analyze", "Done"]
    # Once we reach the final step (and there's no error), the pipeline has
    # actually finished — paint everything green, including Done.
    everything_done = (not error) and active_step >= len(steps)

    els = []
    for i, label in enumerate(steps, start=1):
        if error and i == active_step:
            dot_cls, lbl_cls, symbol = "step-dot error", "step-label error", "X"
        elif everything_done or i < active_step:
            dot_cls, lbl_cls, symbol = "step-dot done",  "step-label done",  "✓"
        elif i == active_step:
            dot_cls, lbl_cls, symbol = "step-dot active", "step-label active", str(i)
        else:
            dot_cls, lbl_cls, symbol = "step-dot",        "step-label",        str(i)

        els.append(html.Div([
            html.Div(symbol, className=dot_cls),
            html.Div(label,  className=lbl_cls),
        ], className="step"))

        if i < len(steps):
            line_done = everything_done or i < active_step
            els.append(html.Div(
                className="step-line done" if line_done else "step-line"
            ))

    # Wrap in a single Div carrying the .stepper flex class so the children
    # lay out horizontally regardless of where this component is mounted.
    return html.Div(els, className="stepper")


def _value_td(val: str) -> html.Td:
    """Return a styled <td> based on the parameter value."""
    v = (val or "").strip()
    if not v or v.lower() == "not stated":
        return html.Td("Not stated", className="val-not-stated")
    if v == "Yes":
        return html.Td(v, className="val-yes")
    if v == "No":
        return html.Td(v, className="val-no")
    if v.startswith("Class"):
        return html.Td(v, className="val-class")
    return html.Td(v, className="val-name")


def build_results_table(results: list) -> html.Div:
    """
    Wide results table: one row per PDF, one column per parameter.
    """
    param_keys   = list(PARAM_LABELS.keys())
    header_cells = [html.Th("Source File")] + [
        html.Th(PARAM_LABELS[k]) for k in param_keys
    ]
    rows = []
    for r in results:
        src   = r.get("_source_file", "")
        cells = [html.Td(src, title=src)]
        for key in param_keys:
            cells.append(_value_td(r.get(key, "Not stated")))
        rows.append(html.Tr(cells))

    return html.Div(
        html.Table(
            [html.Thead(html.Tr(header_cells)), html.Tbody(rows)],
            className="results-table",
        ),
        className="results-wrapper",
    )


def build_totals_bar(results: list) -> html.Div:
    """Render the running-totals chips above the metadata table."""
    total_input    = sum(r.get("_meta", {}).get("input_tokens",      0) or 0 for r in results)
    total_output   = sum(r.get("_meta", {}).get("output_tokens",     0) or 0 for r in results)
    total_extract  = sum(r.get("_meta", {}).get("extraction_time_s", 0) or 0 for r in results)
    total_llm      = sum(r.get("_meta", {}).get("llm_time_s",        0) or 0 for r in results)

    chips = [
        ("PDFs Processed",  str(len(results))),
        ("Input Tokens",    f"{total_input:,}"),
        ("Output Tokens",   f"{total_output:,}"),
        ("Extraction Time", f"{total_extract:.1f} s"),
        ("LLM Time",        f"{total_llm:.1f} s"),
    ]
    return html.Div([
        html.Div([
            html.Span(label, className="chip-label"),
            html.Span(value, className="chip-value"),
        ], className="totals-chip")
        for label, value in chips
    ], className="totals-bar")


def build_metadata_table(results: list) -> html.Div:
    """Render the per-PDF metadata table (shown inside the collapsible section)."""
    header_cells = [html.Th(h) for h in [
        "Source File", "Timestamp", "LLM Provider", "LLM Model",
        "Pages", "OCR Used", "Extraction (s)", "LLM (s)",
        "Input Tokens", "Output Tokens",
    ]]
    rows = []
    for r in results:
        m   = r.get("_meta", {})
        src = r.get("_source_file", "")
        inp = m.get("input_tokens",  0)
        out = m.get("output_tokens", 0)
        cells = [
            html.Td(src, title=src),
            html.Td(m.get("timestamp", "—")),
            html.Td(m.get("llm_provider", "—")),
            html.Td(m.get("llm_model", "—")),
            html.Td(str(m.get("page_count", "—"))),
            html.Td(m.get("ocr_used", "—")),
            html.Td(f"{m.get('extraction_time_s', '—')} s", className="meta-value-time"),
            html.Td(f"{m.get('llm_time_s', '—')} s",        className="meta-value-time"),
            html.Td(f"{inp:,}" if isinstance(inp, int) else "—", className="meta-value-tokens"),
            html.Td(f"{out:,}" if isinstance(out, int) else "—", className="meta-value-tokens"),
        ]
        rows.append(html.Tr(cells))
    return html.Div(
        html.Table(
            [html.Thead(html.Tr(header_cells)), html.Tbody(rows)],
            className="meta-table",
        ),
        className="results-wrapper",
    )


# ─────────────────────────────────────────────────────────────────────────────
# LAYOUT
# ─────────────────────────────────────────────────────────────────────────────
app.layout = html.Div([

    html.Header([
        html.Span("oilrig", className="app-icon"),
        html.Div([
            html.Div("Well Report Analyzer", className="app-title"),
            html.Div("Multi-PDF Extraction + LLM Analysis Pipeline",
                     className="app-subtitle"),
        ])
    ], className="app-header"),

    html.Div([

        # Error banner — hidden by default
        html.Div(id="error-banner", style={"display": "none"}),

        # ── 01 Upload ─────────────────────────────────────────────────────
        html.Div([
            html.Div("01 - Upload PDFs", className="section-label"),
            dcc.Upload(
                id="upload-pdf",
                children=html.Div([
                    html.Span("PDF", className="upload-icon"),
                    html.Div(
                        "Drop one or more PDFs here, or click to browse",
                        className="upload-title",
                    ),
                    html.Div(
                        f"PDF only  |  max {MAX_UPLOAD_SIZE_MB} MB per file  |  multiple files supported",
                        className="upload-hint",
                    ),
                ]),
                className="upload-zone",
                accept=".pdf",
                max_size=MAX_UPLOAD_SIZE_MB * 1024 * 1024,
                multiple=True,
            ),
            # ── LLM Provider selector ────────────────────────────────────
            html.Div([
                html.Span(
                    "LLM Provider",
                    style={
                        "fontFamily": "'IBM Plex Mono', monospace",
                        "fontSize":   "10px",
                        "letterSpacing": "2px",
                        "textTransform": "uppercase",
                        "color":      "#8b949e",
                        "whiteSpace": "nowrap",
                        "marginRight": "12px",
                    },
                ),
                dcc.Dropdown(
                    id="provider-dropdown",
                    options=[
                        {"label": "Gemini",    "value": "gemini"},
                        {"label": "OpenAI",    "value": "openai"},
                        {"label": "Anthropic", "value": "anthropic"},
                    ],
                    value=LLM_PROVIDER,
                    clearable=False,
                    style={"width": "180px", "minWidth": "180px"},
                ),
            ], style={"display": "flex", "alignItems": "center", "marginTop": "18px",
                      "position": "relative", "zIndex": 9999}),
            html.Div(id="filename-display"),
            # ── Load from Cache ──────────────────────────────────────────
            html.Div([
                html.Button(
                    "⬇ Load from Cache",
                    id="btn-load-cache",
                    className="btn-secondary",
                    n_clicks=0,
                    title=(
                        f"Reads {CACHE_FILE} from disk and populates the results "
                        "table without re-uploading any PDFs"
                    ),
                ),
                html.Span(id="cache-load-status", className="cache-status"),
            ], className="cache-row"),
        ], className="section-card"),

        # ── 02 Pipeline status ────────────────────────────────────────────
        html.Div([
            html.Div("02 - Pipeline Status", className="section-label"),
            # `stepper-row` is a plain container; the .stepper flex class lives
            # on the html.Div returned by _build_stepper itself.  This way, when
            # set_progress(_build_stepper(N)) fires, it can safely replace the
            # children with a single Div without losing the flex layout.
            html.Div(
                id="stepper-row",
                children=_build_stepper(0),
            ),
        ], className="section-card"),

        # ── 03 Results (accumulated, never auto-cleared) ──────────────────
        dcc.Loading(
            id="loading-results",
            type="circle",
            color="#e6a817",
            children=html.Div(id="results-card", style={"display": "none"}),
        ),

        # ── 04 Performance & Metadata (collapsible) ───────────────────────
        html.Div([
            html.Div("04 - Performance & Metadata", className="section-label"),
            # Running-totals chips — always visible when card is shown
            html.Div(id="meta-totals-bar"),
            # Controls row: toggle + download buttons
            html.Div([
                html.Button(
                    "Show Metadata ▾",
                    id="btn-toggle-meta",
                    className="meta-toggle-btn",
                    n_clicks=0,
                ),
                html.Button(
                    "Download Metadata",
                    id="btn-download-meta",
                    className="btn-secondary",
                    n_clicks=0,
                ),
            ], className="meta-ctrl-row"),
            # Collapsible: metadata table
            html.Div(
                id="meta-section",
                style={"display": "none"},
                children=[
                    html.Div(className="meta-divider"),
                    html.Div(id="meta-table-content"),
                ],
            ),
        ], id="meta-card", style={"display": "none"}, className="section-card"),

        # Hidden state stores
        dcc.Store(id="pipeline-results",  data=[]),         # list of result dicts
        dcc.Store(id="pipeline-status",   data={"step": 0, "error": ""}),
        dcc.Store(id="selected-provider", data=LLM_PROVIDER),
        dcc.Download(id="download-csv"),
        dcc.Download(id="download-metadata-csv"),

    ], className="main-content"),
])
