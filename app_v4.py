"""
app.py — Well Report Analyzer
A single-file Dash application that extracts and analyzes drilling/well report PDFs.

Changes in this version:
  - Wellbore Name added as an extracted parameter
  - Multiple PDFs can be uploaded and processed in one batch
  - Results accumulate across batches — user decides when to clear via Reset
  - Pipeline stepper now updates in real time AND finishes fully green
  - Metadata & Performance section (section 04):
      * Collapsible metadata table — one row per PDF showing timestamp,
        LLM provider, model name, page count, OCR flag, extraction time,
        LLM time, input tokens, output tokens
      * Running-totals bar always visible: PDFs processed, total tokens, total times
      * "Download Results" exports only the drilling parameters (clean deliverable)
      * "Download Metadata" exports the performance/provenance audit log separately
  - Token usage captured from all three providers (OpenAI, Anthropic, Gemini)
    via their respective response.usage objects; falls back to 0 gracefully
  - Per-phase wall-clock timing (extraction vs LLM) recorded separately

Run with:
    python app.py

Requires a .env file with your API key (copy from .env.example).
"""

import os
import base64
import json
import time
import hashlib
import traceback
from datetime import datetime
from pathlib import Path

import dash
from dash import dcc, html, Input, Output, State, DiskcacheManager
import dash_bootstrap_components as dbc
import diskcache

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION  — edit these to match your setup
# ─────────────────────────────────────────────────────────────────────────────
LLM_PROVIDER       = "gemini"   # options: "openai", "anthropic", "gemini"
TEMP_FOLDER        = "./temp"   # temporary folder for uploaded PDFs
MAX_UPLOAD_SIZE_MB = 50         # reject files larger than this
CACHE_FILE         = "./well_cache.json"  # persistent results cache (local only, not committed to Git)

# ── Load .env file ────────────────────────────────────────────────────────────
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
    _orig_client       = httpx.Client.__init__
    _orig_async_client = httpx.AsyncClient.__init__
    def _client_init(self, *a, **kw):       kw.pop("proxies", None); _orig_client(self, *a, **kw)
    def _async_client_init(self, *a, **kw): kw.pop("proxies", None); _orig_async_client(self, *a, **kw)
    httpx.Client.__init__      = _client_init
    httpx.AsyncClient.__init__ = _async_client_init
except Exception:
    pass

# ── Ensure temp folder exists ─────────────────────────────────────────────────
os.makedirs(TEMP_FOLDER, exist_ok=True)

# ── Windows PATH fix for Tesseract and Poppler ────────────────────────────────
# These paths are specific to one development machine.  Each block checks
# whether its path actually exists before doing anything, so on every other
# machine (different Windows user, Mac, Linux) both blocks are silently skipped
# and cause no side-effects whatsoever.
#
# If YOU are on Windows and can't edit your system PATH (e.g. locked-down
# university or corporate laptop), update these two paths to match your own
# Tesseract and Poppler install locations:
_TESSERACT_EXE = (
    r"C:\Users\2930332\AppData\Local\Programs\Tesseract-OCR\tesseract.exe"
)
if os.path.exists(_TESSERACT_EXE):
    try:
        import pytesseract
        pytesseract.pytesseract.tesseract_cmd = _TESSERACT_EXE
    except ImportError:
        pass  # pytesseract not installed — OCR fallback simply won't be available.

_POPPLER_BIN = (
    r"C:\Users\2930332\AppData\Local\Programs\poppler\poppler-26.02.0\Library\bin"
)
if os.path.exists(_POPPLER_BIN):
    os.environ["PATH"] += ";" + _POPPLER_BIN

# ── Long-callback (background) manager ────────────────────────────────────────
# Backs the `run_pipeline` long_callback so it can run in a subprocess and push
# live step-progress updates to the stepper via `set_progress`.  The cache
# folder is created automatically.  Diskcache + multiprocess are required.
CACHE_FOLDER = "./.cache"
os.makedirs(CACHE_FOLDER, exist_ok=True)
long_callback_manager = DiskcacheManager(diskcache.Cache(CACHE_FOLDER))


# ─────────────────────────────────────────────────────────────────────────────
# PARAMETER DEFINITIONS
# Keys must match the JSON keys the LLM prompt instructs the model to return.
# Order here controls the display order in the results table.
# ─────────────────────────────────────────────────────────────────────────────
PARAM_LABELS = {
    "wellbore_name":                       "Wellbore Name",
    "shallow_gas_hazard_classification":   "Shallow Gas Hazard Classification",
    "pilot_hole_drilled":                  "Pilot Hole Drilled",
    "shallow_gas_encountered":             "Shallow Gas Encountered",
    "gas_bubbles_detected_with_ROV":       "Gas Bubbles Detected with ROV",
    "conductor_cement_type":               "Conductor Cement Type",
    "conductor_gas_tight_cement":          "Conductor Gas-Tight Cement",
    "conductor_lead_slurry_density":       "Conductor Lead Slurry Density",
    "conductor_tail_slurry_density":       "Conductor Tail Slurry Density",
    "conductor_shoe_depth":                "Conductor Shoe Depth",
    "surface_casing_cement_type":          "Surface Casing Cement Type",
    "surface_casing_gas_tight_cement":     "Surface Casing Gas-Tight Cement",
    "surface_casing_lead_slurry_density":  "Surface Casing Lead Slurry Density",
    "surface_casing_tail_slurry_density":  "Surface Casing Tail Slurry Density",
    "surface_casing_shoe_depth":           "Surface Casing Shoe Depth",
}


# ─────────────────────────────────────────────────────────────────────────────
# PDF EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────
OCR_THRESHOLD = 20   # pages with fewer selectable chars than this use OCR

# ── Side-effect stores for metadata (avoids changing public return signatures) ─
# These are written by extract_text_from_pdf / analyze_with_llm and read by
# process_single_pdf immediately after each call.  Because the pipeline runs
# single-threaded inside one long_callback subprocess, there is no race risk.
_extract_meta: dict = {"page_count": 0, "ocr_used": False}
_llm_call_meta: dict = {"input_tokens": 0, "output_tokens": 0, "model_name": ""}


# ─────────────────────────────────────────────────────────────────────────────
# RESULT CACHE
# A JSON file keyed by SHA256 hash of each PDF's raw bytes.  Using the hash
# (not the filename) means renamed copies of the same PDF are still recognised.
# The file lives in the project folder but is excluded from Git via .gitignore
# so confidential extracted data never leaves the local machine.
# Written atomically so a crash mid-save never corrupts the file.
# ─────────────────────────────────────────────────────────────────────────────

def _load_cache() -> dict:
    """Load well_cache.json from disk.  Returns an empty dict if missing or corrupt."""
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_cache(cache: dict):
    """
    Persist the cache dict to well_cache.json using an atomic write.
    Writing to a temp file first and then replacing means a mid-write crash
    can never leave the cache in a corrupt state.
    """
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CACHE_FILE)   # atomic on Windows and POSIX


def _format_table(table: list) -> str:
    """Convert a pdfplumber list-of-lists table into pipe-delimited text."""
    rows = []
    for row in table:
        cells = [str(c).strip() if c is not None else "" for c in row]
        rows.append(" | ".join(cells))
    return "\n".join(rows)


def extract_text_from_pdf(pdf_path: str) -> str:
    """
    Extract all text and table content from a PDF file.

    CASE A (digital pages)  : pdfplumber extracts text + tables directly.
    CASE B (scanned pages)  : pdf2image converts to image, pytesseract OCRs it.

    Returns one combined plain-text string for the entire document.
    Side-effect: populates the module-level _extract_meta dict with
    page_count and ocr_used so process_single_pdf can embed this in metadata.
    """
    global _extract_meta
    try:
        import pdfplumber
    except ImportError:
        raise RuntimeError("pdfplumber is required.  pip install pdfplumber")

    try:
        import pdf2image
        import pytesseract
        ocr_available = True
    except ImportError:
        ocr_available = False

    all_text  = []
    ocr_used  = False

    with pdfplumber.open(pdf_path) as pdf:
        page_count = len(pdf.pages)
        for page_num, page in enumerate(pdf.pages, start=1):
            page_parts = []

            try:
                probe = page.extract_text() or ""
            except Exception:
                probe = ""

            if len(probe.strip()) >= OCR_THRESHOLD:
                # CASE A: digital / selectable text
                try:
                    raw_text = page.extract_text() or ""
                    if raw_text.strip():
                        page_parts.append(raw_text)
                except Exception as e:
                    page_parts.append(f"[TEXT ERROR page {page_num}: {e}]")

                try:
                    tables = page.extract_tables()
                    for t_idx, tbl in enumerate(tables or [], start=1):
                        formatted = _format_table(tbl)
                        page_parts.append(
                            f"\n[TABLE {t_idx} - Page {page_num}]\n"
                            f"{'-'*50}\n{formatted}\n{'-'*50}\n"
                        )
                except Exception as e:
                    page_parts.append(f"[TABLE ERROR page {page_num}: {e}]")

            else:
                # CASE B: scanned — OCR fallback
                ocr_used = True
                if not ocr_available:
                    page_parts.append(
                        f"[Page {page_num}: scanned - OCR unavailable "
                        "(install pdf2image + pytesseract)]"
                    )
                else:
                    try:
                        images = pdf2image.convert_from_path(
                            pdf_path, dpi=300,
                            first_page=page_num, last_page=page_num
                        )
                        if images:
                            ocr_text = pytesseract.image_to_string(
                                images[0], config="--psm 3"
                            )
                            if ocr_text.strip():
                                page_parts.append(ocr_text)
                    except Exception as e:
                        page_parts.append(f"[OCR ERROR page {page_num}: {e}]")

            header = f"\n{'='*60}\nPAGE {page_num}\n{'='*60}\n"
            all_text.append(header + "\n".join(page_parts))

    # ── Populate side-effect metadata ────────────────────────────────────────
    _extract_meta = {"page_count": page_count, "ocr_used": ocr_used}

    return "\n".join(all_text)


# ─────────────────────────────────────────────────────────────────────────────
# LLM ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────
ANALYSIS_PROMPT = """You are an expert in oil & gas drilling operations and document analysis.
You will be given raw text extracted from a drilling or well report PDF.
Your job is to carefully read the text and extract the parameters listed below.
The text may be unstructured, partially OCR'd, or contain table fragments — reason through ambiguous content carefully.

REFERENCE DATA TO LOCATE FIRST (used later for BSF depth calculations):

Before extracting the per-parameter answers, scan the document for these two reference values
on the well-data summary page, header, or cover sheet:

  A) AIR GAP — the height of the rotary kelly bushing above mean sea level.
     Look for labels: "KB ELEVATION (to MSL)", "RKB elevation", "Air gap",
     "Derrick floor elevation", "KB-MSL". Typically a small number, e.g. 23 m, 25 m, 30 m.

  B) WATER DEPTH — the distance from mean sea level down to the seabed.
     Look for labels: "WATER DEPTH (MSL)", "Water depth", "Sea depth", "MSL to seabed".

Hold these two values in mind — they are needed to compute Below-Seabed-Floor (BSF) depths.
If EITHER value is not stated in the document, BSF cannot be computed and any BSF answer
below MUST be returned as "Not stated".

PARAMETERS TO EXTRACT:

1. Wellbore Name
   - Return the wellbore or well name/designation as it appears in the document
   - Look for labels such as: Well Name, Wellbore, Well Designation, Field/Well, or the report header
   - If not found, return: "Not stated"

2. Shallow Gas Hazard Classification
   - Preferred values: Class 0, Class 1, Class 2
   - If the document uses the explicit Class 0/1/2 terminology, return that value verbatim.
   - If the document does NOT use this classification but describes the hazard verbally,
     INFER the class using the mapping below and append " (inferred)" to your answer:
       * "no warning" / "no shallow gas hazard" / "no anomaly"            -> Class 0 (inferred)
       * "weak warning" / "low risk" / "minor anomaly"                    -> Class 1 (inferred)
       * "moderate warning" / "significant warning" / "possible gas"      -> Class 2 (inferred)
       * "strong warning" / "high risk" / "confirmed shallow gas"         -> Class 2 (inferred)
   - If the document is entirely silent on shallow gas hazard, return: "Not stated"

3. Pilot Hole Drilled
   - Return exactly one of: Yes, No
   - If not found, return: "Not stated"

4. Shallow Gas Encountered
   - Return exactly one of: Yes, No
   - If not found, return: "Not stated"

5. Gas Bubbles Detected with ROV
   - Return exactly one of: Yes, No
   - If not found, return: "Not stated"

6. Conductor Cement Type
   - Return one or more of: Class G, Norcem G, Dyckerhoff G, Tuned Light XL, Tuned Light XLE, X-lite, DWFS, Class C
     (comma-separated if multiple).
   - Match case-insensitively and normalize synonyms:
       "API Class G", "API CLASS G", "Class G cement"  ->  Class G
       "Norcem Class G"                                 ->  Norcem G
       "Dyckerhoff Class G"                             ->  Dyckerhoff G
   - The conductor is typically the 30" or 36" casing string. In older reports it may be
     identified only by diameter rather than by the label "conductor".
   - If not found, return: "Not stated"

7. Conductor Gas-Tight Cement
   - Was the cement used for the conductor (30" / 36") casing designed to be gas-tight?
   - Return "Yes" if the cementing description for THIS casing mentions ANY of the following
     (case-insensitive):
       * Additive products: "Gascon", "Gascon 469", "Gascon-469", "GASCON469", "GASCON-469"
       * Product code: "EDP-C469-91"
       * Descriptors: "gas-tight cement", "gastight cement", "gas-stop cement",
         "gasstop cement", "gas-block cement", "gasblock cement",
         "gas-migration cement", "gas migration additive", "gas migration control",
         or any explicit statement that the cement was designed to prevent gas migration.
   - Return "No" if the cementing job for the conductor IS described (slurry composition,
     additives, or cement design stated) but none of the above appear.
   - Return "Not stated" ONLY if the conductor cementing job is not described at all.
   - IMPORTANT: judge this strictly from additives/descriptors mentioned for the CONDUCTOR
     itself. Do not infer "Yes" because Gascon was used on a deeper string.

8. Conductor Lead Slurry Density
   - Return the LEAD slurry density used to cement the conductor (30" / 36") casing,
     formatted as a number followed by " sg" (e.g. "1.56 sg").
   - If only ONE slurry density is given for the conductor (a single-stage cement job
     with no lead/tail separation), put that single value here.
   - If not found, return: "Not stated"

9. Conductor Tail Slurry Density
   - Return the TAIL slurry density used to cement the conductor (30" / 36") casing,
     formatted as a number followed by " sg" (e.g. "1.90 sg").
   - If the conductor was cemented with a SINGLE slurry (no separate lead and tail),
     return: "Not applicable"
   - If the document mentions a tail slurry but does not state its density,
     return: "Not stated"

10. Conductor Shoe Depth
    - Return the depth at which the conductor (30" / 36") casing SHOE was set,
      expressed as Below-Seabed-Floor (mBSF) where possible.
    - STEP 1 — Locate the shoe depth in mRKB. Look in: casing design summary,
      casing schematic, "shoe set at" statements, completion log, daily reports.
      If multiple depths are mentioned, use the FINAL setting depth.
    - STEP 2 — If BOTH the air gap AND the water depth (located in the REFERENCE
      DATA section at the top of this prompt) are stated in the document, compute:
        BSF = conductor_shoe_depth_mRKB - (water_depth + air_gap)
      and return the result as a number followed by " mBSF" (e.g. "89.5 mBSF").
      Use the values exactly as stated — do NOT round or invent defaults.
    - STEP 3 — Fallback: if EITHER the air gap OR the water depth is missing
      from the document (so BSF cannot be computed), return the mRKB value as
      found, formatted as a number followed by " mRKB" (e.g. "464.5 mRKB").
    - If the shoe depth itself cannot be located in the document at all,
      return: "Not stated"

11. Surface Casing Cement Type
    - Return one or more of: Class G, Norcem G, Dyckerhoff G, Tuned Light XL, Tuned Light XLE, X-lite, DWFS, Class C
      (comma-separated if multiple).
    - Match case-insensitively and normalize synonyms exactly as for the conductor (item 6).
    - The surface casing is typically the 20" or 13 3/8" casing string. In older reports
      it may be identified only by diameter rather than by the label "surface casing".
    - If not found, return: "Not stated"

12. Surface Casing Gas-Tight Cement
    - Was the cement used for the surface (20" / 13 3/8") casing designed to be gas-tight?
    - Same matching rules as item 7, but applied to the surface casing's cementing description.
    - Return "Yes" / "No" / "Not stated" using the same logic as item 7.
    - IMPORTANT: judge this strictly from additives/descriptors mentioned for the SURFACE
      CASING itself. Do not infer from other casing strings.

13. Surface Casing Lead Slurry Density
    - Return the LEAD slurry density used to cement the surface (20" / 13 3/8") casing,
      formatted as a number followed by " sg" (e.g. "1.56 sg").
    - If only ONE slurry density is given for the surface casing, put that single value here.
    - If not found, return: "Not stated"

14. Surface Casing Tail Slurry Density
    - Return the TAIL slurry density used to cement the surface (20" / 13 3/8") casing,
      formatted as a number followed by " sg" (e.g. "1.90 sg").
    - If the surface casing was cemented with a SINGLE slurry, return: "Not applicable"
    - If a tail slurry was used but its density is not stated, return: "Not stated"

15. Surface Casing Shoe Depth
    - Return the depth at which the surface (20" / 13 3/8") casing SHOE was set,
      expressed as Below-Seabed-Floor (mBSF) where possible.
    - STEP 1 — Locate the shoe depth in mRKB using the same lookup locations as item 10.
      If multiple depths are mentioned, use the FINAL setting depth.
    - STEP 2 — If BOTH the air gap AND the water depth (located in the REFERENCE DATA
      section at the top of this prompt) are stated, compute:
        BSF = surface_casing_shoe_depth_mRKB - (water_depth + air_gap)
      using the SAME air_gap and water_depth values used for item 10. Return the result
      as a number followed by " mBSF" (e.g. "975 mBSF").
    - STEP 3 — Fallback: if EITHER the air gap OR the water depth is missing from the
      document, return the mRKB value as found, formatted as a number followed by
      " mRKB" (e.g. "1350 mRKB").
    - If the shoe depth itself cannot be located at all, return: "Not stated"

INSTRUCTIONS:
- Do NOT guess or fabricate values. Only extract what is explicitly stated, clearly implied,
  or inferable from the explicit mappings above.
- For wellbore name, look in the document title, header, cover page, or first few pages.
- For cement types, gas-tight indicators, and slurry densities, look in: cementing program,
  cement design, casing program, well construction summary, daily drilling reports.
- For shallow gas, look in: hazard assessment, pre-drill site survey, geological hazards,
  shallow gas warning sections.
- For BSF calculations: use the air gap and water depth located in the reference data section
  above. Both shoe-depth answers (items 10 and 15) MUST use the same reference values.
  If those reference values are missing, fall back to mRKB rather than returning "Not stated".
- Return ONLY a valid JSON object with NO extra text, explanation, or markdown.
- Use exactly these keys, in this order:

{{
  "wellbore_name": "",
  "shallow_gas_hazard_classification": "",
  "pilot_hole_drilled": "",
  "shallow_gas_encountered": "",
  "gas_bubbles_detected_with_ROV": "",
  "conductor_cement_type": "",
  "conductor_gas_tight_cement": "",
  "conductor_lead_slurry_density": "",
  "conductor_tail_slurry_density": "",
  "conductor_shoe_depth": "",
  "surface_casing_cement_type": "",
  "surface_casing_gas_tight_cement": "",
  "surface_casing_lead_slurry_density": "",
  "surface_casing_tail_slurry_density": "",
  "surface_casing_shoe_depth": ""
}}

RAW EXTRACTED TEXT:
{raw_extracted_text}"""


def _parse_llm_json(response_text: str) -> dict:
    """Strip markdown fences if present, then parse the JSON."""
    text = response_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text  = "\n".join(ln for ln in lines if not ln.strip().startswith("```"))
    return json.loads(text.strip())


def analyze_with_llm(raw_text: str, provider: str = None) -> dict:
    """
    Send raw_text to the configured LLM and return the extracted parameter dict.
    `provider` defaults to LLM_PROVIDER but can be overridden at call time so
    the UI dropdown takes effect without restarting the app.

    Side-effect: populates _llm_call_meta with input_tokens, output_tokens,
    and model_name so process_single_pdf can embed these in the metadata record.
    Token extraction is wrapped in try/except — if the provider changes its
    response shape, counts fall back to 0 rather than crashing the pipeline.
    """
    if provider is None:
        provider = LLM_PROVIDER
    # Reload .env so API keys edited on disk are picked up without a restart.
    try:
        from dotenv import load_dotenv as _ld
        _ld(override=True)
    except ImportError:
        pass
    global _llm_call_meta
    prompt = ANALYSIS_PROMPT.format(raw_extracted_text=raw_text)

    if provider == "openai":
        try:
            import openai
        except ImportError:
            raise RuntimeError("openai not installed.  pip install openai")
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set in your .env file.")
        client   = openai.OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model="gpt-5.4-mini",
            messages=[{"role": "user", "content": prompt}],
        )
        try:
            _llm_call_meta = {
                "input_tokens":  response.usage.prompt_tokens,
                "output_tokens": response.usage.completion_tokens,
                "model_name":    response.model,
            }
        except Exception:
            _llm_call_meta = {"input_tokens": 0, "output_tokens": 0, "model_name": "gpt-5.4-mini"}
        return _parse_llm_json(response.choices[0].message.content)

    elif provider == "anthropic":
        try:
            import anthropic
        except ImportError:
            raise RuntimeError("anthropic not installed.  pip install anthropic")
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set in your .env file.")
        client  = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-opus-4-5", max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        try:
            _llm_call_meta = {
                "input_tokens":  message.usage.input_tokens,
                "output_tokens": message.usage.output_tokens,
                "model_name":    message.model,
            }
        except Exception:
            _llm_call_meta = {"input_tokens": 0, "output_tokens": 0, "model_name": "claude-opus-4-5"}
        return _parse_llm_json(message.content[0].text)

    elif provider == "gemini":
        try:
            import google.generativeai as genai
        except ImportError:
            raise RuntimeError(
                "google-generativeai not installed.  pip install google-generativeai"
            )
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not set in your .env file.")
        genai.configure(api_key=api_key)
        model    = genai.GenerativeModel("gemini-2.5-flash")
        response = model.generate_content(prompt)
        try:
            _llm_call_meta = {
                "input_tokens":  response.usage_metadata.prompt_token_count,
                "output_tokens": response.usage_metadata.candidates_token_count,
                "model_name":    "gemini-2.5-flash",
            }
        except Exception:
            _llm_call_meta = {"input_tokens": 0, "output_tokens": 0, "model_name": "gemini-2.5-flash"}
        return _parse_llm_json(response.text)

    else:
        raise RuntimeError(
            f"Unknown LLM provider '{provider}'. "
            "Choose from: openai, anthropic, gemini"
        )


# ─────────────────────────────────────────────────────────────────────────────
# PIPELINE HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def _cleanup(path: str):
    """Silently remove a temp file — never raises."""
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
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
    except Exception as e:
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
    except Exception as e:
        return None, f"'{filename}': could not save temp file — {e}"

    # ── Live: Upload finished, Extract is now ACTIVE ────────────────────────
    if progress_cb is not None:
        try:
            progress_cb(_build_stepper(2))
        except Exception:
            pass

    try:
        t_extract_start = time.time()
        raw_text = extract_text_from_pdf(temp_path)
        t_extract = round(time.time() - t_extract_start, 2)
        extract_snapshot = _extract_meta.copy()   # read side-effect
    except Exception as e:
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
        except Exception:
            pass

    try:
        t_llm_start = time.time()
        result = analyze_with_llm(raw_text, provider=provider)
        t_llm = round(time.time() - t_llm_start, 2)
        llm_snapshot = _llm_call_meta.copy()      # read side-effect
    except Exception as e:
        _cleanup(temp_path)
        return None, f"'{filename}': LLM analysis failed — {e}"

    _cleanup(temp_path)
    result["_source_file"] = filename
    result["_meta"] = {
        "timestamp":         datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "llm_provider":      provider,
        "llm_model":         llm_snapshot.get("model_name", ""),
        "page_count":        extract_snapshot.get("page_count", 0),
        "ocr_used":          "Yes" if extract_snapshot.get("ocr_used") else "No",
        "extraction_time_s": t_extract,
        "llm_time_s":        t_llm,
        "input_tokens":      llm_snapshot.get("input_tokens", 0),
        "output_tokens":     llm_snapshot.get("output_tokens", 0),
    }

    # ── Write to cache ───────────────────────────────────────────────────────
    # Reload before writing so parallel batches don't overwrite each other's
    # entries — re-reading first keeps as many prior entries as possible.
    try:
        fresh_cache = _load_cache()
        fresh_cache[cache_key] = result.copy()
        _save_cache(fresh_cache)
    except Exception:
        pass   # cache write failure is non-fatal — result still returned

    return result, ""


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


# ─────────────────────────────────────────────────────────────────────────────
# CALLBACKS
# ─────────────────────────────────────────────────────────────────────────────

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
    except Exception:
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
    except Exception:
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
