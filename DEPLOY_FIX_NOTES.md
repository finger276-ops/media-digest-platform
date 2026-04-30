# Fix: universal topics platform import error

This patch includes a complete `src/` set. The previous universal topics patch only included `app.py`, `preprocess.py`, `settings.py`, and `import_adapters.py`, while the updated `app.py` imports `load_all_manual_tables` from `manual_db.py` introduced in the speed optimization patch. If `manual_db.py` was not deployed together with the new app, Streamlit could fail during import.

Replace the whole `src/` folder with the files in this archive, or at minimum update:

- `src/app.py`
- `src/preprocess.py`
- `src/settings.py`
- `src/import_adapters.py`
- `src/manual_db.py`
- `src/persistent_store.py`
- `src/io_utils.py`

Then reboot the Streamlit app.
