# Well Report Analyzer

A web application that reads oil & gas well report PDFs and automatically extracts key drilling parameters using an AI language model. Upload one or several PDFs and get a structured table of results you can download as a CSV.

Built with Python and Dash. Supports OpenAI, Anthropic Claude, and Google Gemini as the AI backend.

---

## What it extracts

For every PDF you upload, the app pulls out these 15 parameters:

| # | Parameter | Example value |
|---|---|---|
| 1 | Wellbore Name | 15/9-F-11 T2 |
| 2 | Shallow Gas Hazard Classification | Class 0 / Class 1 / Class 2 |
| 3 | Pilot Hole Drilled | Yes / No |
| 4 | Shallow Gas Encountered | Yes / No |
| 5 | Gas Bubbles Detected with ROV | Yes / No |
| 6 | Conductor Cement Type | Class G, Norcem G, Tuned Light XL … |
| 7 | Conductor Gas-Tight Cement | Yes / No |
| 8 | Conductor Lead Slurry Density | 1.56 sg |
| 9 | Conductor Tail Slurry Density | 1.90 sg |
| 10 | Conductor Shoe Depth | 89.5 mBSF |
| 11 | Surface Casing Cement Type | Class G, Norcem G … |
| 12 | Surface Casing Gas-Tight Cement | Yes / No |
| 13 | Surface Casing Lead Slurry Density | 1.56 sg |
| 14 | Surface Casing Tail Slurry Density | 1.90 sg |
| 15 | Surface Casing Shoe Depth | 975 mBSF |

Values not found in the document are returned as **Not stated**.

---

## Before you begin — what you need

| Requirement | Notes |
|---|---|
| Python 3.9 or newer | Check with `python --version` in a terminal |
| Git | To clone the repository |
| An API key | Google Gemini is recommended — free tier, no credit card needed (see below) |
| Internet connection | The app calls the AI API when processing each PDF |

You do **not** need to install anything else to test with the sample PDFs included in this repository. OCR for scanned documents is optional and covered separately at the bottom of this file.

---

## Setup — step by step

### Step 1 — Clone the repository

Open a terminal (Command Prompt, PowerShell, or Terminal on Mac/Linux) and run:

```bash
git clone https://github.com/YOUR_USERNAME/well-report-analyzer.git
cd well-report-analyzer
```

After cloning, your folder looks like this:

```
well-report-analyzer/
├── app.py               ← entry point
├── cache.py             ← cache read/write logic
├── pdf_extract.py       ← PDF extraction and OCR fallback
├── llm_clients.py       ← LLM provider calls (OpenAI / Anthropic / Gemini)
├── layout.py            ← Dash app, UI layout, component builders
├── callbacks.py         ← pipeline orchestration and Dash callbacks
├── test_app.py          ← unit tests
├── requirements.txt     ← Python dependencies
├── .env.example         ← API key template (copy this to .env)
├── Well_Reports/        ← sample well reports to test with
├── .gitignore
└── README.md
```

### Step 2 — Create a virtual environment

A virtual environment keeps these dependencies isolated from the rest of Python on your machine. This is recommended but optional.

**On Windows (Anaconda Prompt):**
```bash
conda create -n wellreport python=3.11
conda activate wellreport
```

**On Windows (plain Python) / Mac / Linux:**
```bash
python -m venv venv

# Activate it:
# Windows:
venv\Scripts\activate
# Mac / Linux:
source venv/bin/activate
```

You should see `(wellreport)` or `(venv)` at the start of your terminal prompt when the environment is active.

### Step 3 — Install Python dependencies

```bash
pip install -r requirements.txt
```

This installs everything in one go. It may take a minute or two the first time.

To confirm it worked:
```bash
python -c "import dash, pdfplumber, anthropic, openai, google.generativeai; print('All good')"
```

You should see `All good`. If you see an import error, re-run the pip command above.

### Step 4 — Get an API key

The app needs a key from one AI provider. **Google Gemini is recommended** because it has a free tier that requires no credit card.

**Option A — Google Gemini (recommended for testing):**

1. Go to [https://aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey)
2. Sign in with a Google account
3. Click **Create API key**
4. Copy the key (it starts with `AIza…`)

**Option B — OpenAI:**

1. Go to [https://platform.openai.com/api-keys](https://platform.openai.com/api-keys)
2. Create an account and add a small credit balance
3. Click **Create new secret key** and copy it (starts with `sk-…`)

**Option C — Anthropic Claude:**

1. Go to [https://console.anthropic.com/](https://console.anthropic.com/)
2. Create an account and add credit
3. Go to **API Keys** and create a key (starts with `sk-ant-…`)

### Step 5 — Create your .env file

Copy the template:

```bash
# Windows:
copy .env.example .env

# Mac / Linux:
cp .env.example .env
```

Open the new `.env` file in any text editor (Notepad is fine) and paste in the key for whichever provider you chose. You only need to fill in the one you are using — leave the others blank.

```
OPENAI_API_KEY=sk-your-key-here

ANTHROPIC_API_KEY=sk-ant-your-key-here

GEMINI_API_KEY=AIza-your-key-here
```

Save the file. The `.env` file is listed in `.gitignore` so it will never be committed to Git — your key stays on your machine only.

### Step 6 — Set the provider in the app

Open `llm_clients.py` in any text editor. Near the top you will see:

```python
LLM_PROVIDER = "gemini"   # options: "openai", "anthropic", "gemini"
```

Make sure this matches the provider whose key you added in Step 5. Save the file.

> You can also switch provider from the UI dropdown while the app is running — no restart needed.

### Step 7 — Run the app

```bash
python app.py
```

You should see:

```
-------------------------------------------------------
  Well Report Analyzer
  LLM Provider : gemini
  Temp folder  : /path/to/well-report-analyzer/temp
-------------------------------------------------------
  Open http://127.0.0.1:8050 in your browser
-------------------------------------------------------
```

Open **http://127.0.0.1:8050** in any browser. The app is ready.

To stop it: press `Ctrl + C` in the terminal.

---

## Testing with the sample PDFs

Sample well reports are included in the `Well_Reports/` folder so you can test the app without needing your own documents.

1. Start the app (`python app.py`)
2. Open [http://127.0.0.1:8050](http://127.0.0.1:8050)
3. Drag one or more PDF files from the `Well_Reports/` folder into the upload zone, or click the zone to browse
4. Watch the pipeline stepper — **Upload → Extract → Analyze → Done** — as each file is processed
5. Results appear as a table once processing finishes. Each file is one row
6. Click **Download Results** to save the table as a CSV

You can upload multiple files at once or in separate batches — results accumulate in the table. Click **Reset & Clear** to start fresh.

---

## Validating your setup

After processing one or both of the sample PDFs from the `Well_Reports/` folder, a **Validate Sample Results** button appears below the results table. Click it to compare what the app extracted against a set of manually verified reference values stored in `Validation.csv`.

**What it checks**

For each sample well that has been processed, the validation table shows every parameter side-by-side — expected value on the left, extracted value on the right — with a green **PASS** or red **FAIL** badge. A summary line at the bottom counts how many of the 15 parameters matched.

**What a passing result looks like**

```
15/15 parameters matched for well 6608/11-3.  All correct!
15/15 parameters matched for well 6608/10-13. All correct!
```

**Scope**

- The validate button only appears after processing the two included sample PDFs — it will not appear for your own documents.
- If only one sample PDF has been processed, only that well's results are shown; the second is skipped with no error.
- Validation compares against `Validation.csv` in the project root. The expected values in that file are correct answers manually read from the original reports. Do not edit them unless you have re-verified the source document.
- A **Close Validation** button at the top of the validation card hides it without losing your results. The validation card also disappears when **Reset & Clear** is pressed.

---

## Running the tests

The test suite uses mocks, so it runs with no API key, no PDF, and no running server needed.

```bash
pytest test_app.py -v
```

Expected output:
```
======================== 121 passed in Xs ========================
```

A GitHub Actions workflow (`.github/workflows/ci.yml`) runs this suite automatically on every push and pull request, so a broken import or failing test is caught before it reaches the repository.

For a coverage report:
```bash
pytest test_app.py -v --cov=app --cov=cache --cov=pdf_extract --cov=llm_clients --cov=layout --cov=callbacks --cov-report=term-missing
```

---

## Optional: OCR for scanned PDFs

The sample PDFs and most modern well reports contain selectable (digital) text, so **you do not need OCR to test the app**. OCR is only needed if you want to process image-scanned documents where the text cannot be selected.

If you do need OCR, you must install two external programs — these are not Python packages:

### Tesseract OCR

| OS | Command / Link |
|---|---|
| Windows | [UB-Mannheim installer](https://github.com/UB-Mannheim/tesseract/wiki) — run the installer, default options are fine |
| macOS | `brew install tesseract` |
| Linux | `sudo apt install tesseract-ocr` |

After installing, add the Tesseract `bin` folder to your **system PATH** so the app can find it.

### Poppler

| OS | Command / Link |
|---|---|
| Windows | [Poppler for Windows](https://github.com/oschwartz10612/poppler-windows/releases) — download, extract, add the `Library\bin` folder to PATH |
| macOS | `brew install poppler` |
| Linux | `sudo apt install poppler-utils` |

**If you are on a Windows machine where you cannot edit system PATH** (e.g. a university or corporate laptop), add the two paths to your `.env` file instead:

```
TESSERACT_EXE=C:\path\to\Tesseract-OCR\tesseract.exe
POPPLER_BIN=C:\path\to\poppler\Library\bin
```

Replace the example paths with where **you** installed Tesseract and Poppler. The app reads these at startup and configures the tools automatically. Each variable is optional — if it is not set or the path does not exist, the block does nothing.

---

## Switching AI provider

The app supports three providers. You can switch at any time:

**Method A — Dropdown in the UI** (no restart needed):
Use the **LLM Provider** dropdown below the upload zone.

**Method B — Edit the default in `llm_clients.py`:**

```python
LLM_PROVIDER = "gemini"   # change to "openai" or "anthropic"
```

Make sure the matching key is in your `.env`:

| Provider | .env key | Free tier? | Get a key |
|---|---|---|---|
| Gemini | `GEMINI_API_KEY` | Yes | [aistudio.google.com](https://aistudio.google.com/app/apikey) |
| OpenAI | `OPENAI_API_KEY` | No (pay-per-use) | [platform.openai.com](https://platform.openai.com/api-keys) |
| Anthropic | `ANTHROPIC_API_KEY` | No (pay-per-use) | [console.anthropic.com](https://console.anthropic.com/) |

---

## Project structure

| File / Folder | What it is |
|---|---|
| `app.py` | Entry point — loads config, applies the httpx proxy patch, starts the server |
| `cache.py` | Cache read/write logic (`_load_cache`, `_save_cache`, `CACHE_FILE`) |
| `pdf_extract.py` | PDF text extraction and OCR fallback (`extract_text_from_pdf`) |
| `llm_clients.py` | LLM provider calls for OpenAI, Anthropic, and Gemini (`analyze_with_llm`) |
| `layout.py` | Dash app object, UI layout, component builders, and pipeline constants |
| `callbacks.py` | Pipeline orchestration (`process_single_pdf`) and all Dash callbacks |
| `test_app.py` | 121 unit tests covering every function; all mocked, no real API calls |
| `requirements.txt` | All pip dependencies, pinned to working versions |
| `.env.example` | Template showing which keys are needed — copy to `.env` and fill in |
| `.env` | Your real API keys — created by you locally, never committed to Git |
| `Well_Reports/` | Sample well reports included so you can test without your own files |
| `temp/` | Auto-created — uploaded PDFs are saved here temporarily during processing and deleted afterwards |
| `.cache/` | Auto-created — used by Dash's background task manager to stream live progress updates |
| `well_cache.json` | Auto-created — stores extracted results so the same PDF is never processed twice; excluded from Git |

---

## Troubleshooting

**`GEMINI_API_KEY is not set in your .env file`** (or same for OpenAI / Anthropic)
Your `.env` file is missing or the key is on the wrong line. Re-check Step 5. Make sure the file is named `.env` exactly (not `.env.txt` or `env`). On Windows, File Explorer may hide the extension — check in a text editor or terminal with `dir /a`.

**`ModuleNotFoundError`**
Run `pip install -r requirements.txt` again with your virtual environment active. The prompt should show `(wellreport)` or `(venv)` before you run pip.

**`ModuleNotFoundError: No module named 'diskcache'` or `'multiprocess'`**
These power the live progress stepper. Run `pip install diskcache multiprocess`.

**The stepper stays on step 1 / progress never updates**
The background task manager couldn't start a subprocess. Make sure `diskcache` and `multiprocess` are installed and the `.cache/` folder is writable. On some Windows machines antivirus software blocks subprocess creation — try temporarily whitelisting your project folder.

**`TesseractNotFoundError`**
Tesseract is not on your PATH. Either install it and add it to PATH (see the OCR section above), or set `TESSERACT_EXE` in your `.env` file to the full path of `tesseract.exe` (see the Windows note in the OCR section). This error only happens on scanned PDFs — the sample PDFs will work fine without Tesseract.

**`Unable to get page count. Is poppler installed and in PATH?`**
Same situation as above but for Poppler. Set `POPPLER_BIN` in your `.env` file to the full path of the Poppler `Library\bin` folder. Only affects scanned PDFs.

**LLM returns garbled or incomplete JSON**
This can happen with very long PDFs. Increase `max_tokens` in the `analyze_with_llm()` function inside `llm_clients.py` (currently `1024`).

**Port 8050 is already in use**
Another Dash app is already running. Stop it with `Ctrl + C`, or change the port in the last line of `app.py`:
```python
app.run(debug=False, host="0.0.0.0", port=8051)
```
Then open `http://127.0.0.1:8051` instead.

**Results from a previous session are still showing**
The app caches results in `well_cache.json` so the same PDF is never sent to the AI twice. If you want to clear all cached results, delete `well_cache.json` from the project folder and click **Reset & Clear** in the browser. The file is recreated automatically next time you process a PDF.

---

## How the pipeline works

1. **Upload** — You drop one or more PDFs in the browser. Filenames appear immediately as grey "queued" badges so you know the upload was received.
2. **Extract** — Each page is read with pdfplumber. If a page has selectable text, text and tables are extracted directly. If a page is a scanned image, Tesseract OCR is used as a fallback.
3. **Analyze** — The extracted text is sent to your chosen AI provider with a structured prompt. The model returns a JSON object with the 15 parameters.
4. **Done** — Results appear as a new row in the table. The pipeline stepper turns fully green. You can upload more PDFs to add more rows, or click **Download Results** to save a CSV.

Throughout steps 2–4 the stepper indicator updates live so you can see exactly which stage each file is at. Previously processed results are never removed — they accumulate until you click **Reset & Clear**.

**Result cache and cache-hit dialog**

Every result is stored in `well_cache.json` (keyed by the PDF's content hash). If you upload a PDF that has been processed before, a dialog appears immediately with two choices:

- **Load from Cache** — reuses the stored result instantly; no AI call, no wait.
- **Reprocess & Compare** — runs the full pipeline again and shows a side-by-side diff of the old and new values so you can see exactly what (if anything) changed.

This means the same PDF is never sent to the AI twice unless you explicitly ask for it.
