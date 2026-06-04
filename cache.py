"""
cache.py — Persistent result cache for the Well Report Analyzer.

Stores extracted results in a local JSON file keyed by SHA-256 hash of each
PDF's raw bytes.  Written atomically so a crash mid-save never corrupts the file.
"""
import os
import json

_HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(_HERE, "well_cache.json")
VALIDATION_FILE = os.path.join(_HERE, "Validation.csv")


def _load_cache() -> dict:
    """Load well_cache.json from disk.  Returns an empty dict if missing or corrupt."""
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_cache(cache: dict):
    """Persist the cache dict to well_cache.json using an atomic write.

    Writing to a temp file first and then replacing means a mid-write crash
    can never leave the cache in a corrupt state.
    """
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CACHE_FILE)   # atomic on Windows and POSIX
