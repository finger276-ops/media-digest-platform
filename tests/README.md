# Тесты

Тесты не ходят в сеть: Supabase подменяется поддельным клиентом
(`fake_supabase.py`), повторяющим форму API supabase-py. Гоняются в CI при
каждом пуше и пул-реквесте — см. `.github/workflows/tests.yml`.

```bash
pip install -r requirements.txt        # нужен полный набор: streamlit, matplotlib, python-docx, reportlab
python tests/test_import_adapters.py   # разбор выгрузок: форматы, канонизация, починка битого xlsx
python tests/test_preprocess.py        # обработка: микротемы, заголовки инфоповодов, сборка таблиц
python tests/test_brand_metrics.py     # индексы бренда против примеров из Metric Calculation Guide
python tests/test_event_titles.py      # склейка похожих заголовков инфоповодов
python tests/test_event_enrichment.py  # связь сообщений с инфоповодами, агрегация
python tests/test_ai_summary.py        # тексты от ИИ (YandexGPT/GigaChat) — без сети
python tests/test_report_export.py     # выгрузки саммари: PNG-инфографика, Word, PDF
python tests/test_session_presence.py  # живые сессии: heartbeat, окно онлайна, очистка
python tests/test_ingest_queue.py      # логика очереди: захват, гонки, ретраи, источники
python tests/test_worker_e2e.py        # воркер целиком: очередь → файл → период проекта
python tests/test_ui_smoke.py          # интерфейс: приложение стартует, разделы рисуются
```

Каждый тест печатает список проверок и завершается с ненулевым кодом при провале,
поэтому в CI каждый файл — отдельный шаг: видно, какой именно тест упал.
