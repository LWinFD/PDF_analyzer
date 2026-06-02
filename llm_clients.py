"""
llm_clients.py — LLM provider clients for the Well Report Analyzer.

Supports OpenAI, Anthropic Claude, and Google Gemini.  Each provider is
imported lazily (inside the function) so missing SDKs don't prevent the app
from starting with a different provider configured.
"""
import os
import json

LLM_PROVIDER = "gemini"   # options: "openai", "anthropic", "gemini"

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


def analyze_with_llm(raw_text: str, provider: str = None) -> tuple:
    """
    Send raw_text to the configured LLM and return (result, meta).
    result is the extracted parameter dict; meta has input_tokens, output_tokens,
    and model_name.  `provider` defaults to LLM_PROVIDER but can be overridden
    at call time so the UI dropdown takes effect without restarting the app.
    Token extraction is wrapped in try/except — if the provider changes its
    response shape, counts fall back to 0 rather than crashing the pipeline.
    """
    if provider is None:
        provider = LLM_PROVIDER
    prompt = ANALYSIS_PROMPT.format(raw_extracted_text=raw_text)

    if provider == "openai":
        try:
            import openai
        except ImportError as e:
            raise RuntimeError("openai not installed.  pip install openai") from e
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set in your .env file.")
        client = openai.OpenAI(api_key=api_key)
        try:
            response = client.chat.completions.create(
                model="gpt-5.4-mini",
                messages=[{"role": "user", "content": prompt}],
            )
        except openai.OpenAIError as e:
            raise RuntimeError(f"OpenAI API error: {e}") from e
        try:
            meta = {
                "input_tokens":  response.usage.prompt_tokens,
                "output_tokens": response.usage.completion_tokens,
                "model_name":    response.model,
            }
        except Exception:
            meta = {"input_tokens": 0, "output_tokens": 0, "model_name": "gpt-5.4-mini"}
        return _parse_llm_json(response.choices[0].message.content), meta

    elif provider == "anthropic":
        try:
            import anthropic
        except ImportError as e:
            raise RuntimeError("anthropic not installed.  pip install anthropic") from e
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set in your .env file.")
        client = anthropic.Anthropic(api_key=api_key)
        try:
            message = client.messages.create(
                model="claude-opus-4-7", max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.APIError as e:
            raise RuntimeError(f"Anthropic API error: {e}") from e
        try:
            meta = {
                "input_tokens":  message.usage.input_tokens,
                "output_tokens": message.usage.output_tokens,
                "model_name":    message.model,
            }
        except Exception:
            meta = {"input_tokens": 0, "output_tokens": 0, "model_name": "claude-opus-4-5"}
        return _parse_llm_json(message.content[0].text), meta

    elif provider == "gemini":
        try:
            import google.generativeai as genai
        except ImportError as e:
            raise RuntimeError(
                "google-generativeai not installed.  pip install google-generativeai"
            ) from e
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not set in your .env file.")
        from google.api_core import exceptions as google_exceptions
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-2.5-flash")
        try:
            response = model.generate_content(prompt)
        except google_exceptions.GoogleAPIError as e:
            raise RuntimeError(f"Gemini API error: {e}") from e
        try:
            meta = {
                "input_tokens":  response.usage_metadata.prompt_token_count,
                "output_tokens": response.usage_metadata.candidates_token_count,
                "model_name":    "gemini-2.5-flash",
            }
        except Exception:
            meta = {"input_tokens": 0, "output_tokens": 0, "model_name": "gemini-2.5-flash"}
        return _parse_llm_json(response.text), meta

    else:
        raise RuntimeError(
            f"Unknown LLM provider '{provider}'. "
            "Choose from: openai, anthropic, gemini"
        )
