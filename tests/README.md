# Тесты

Тесты не ходят в сеть: Supabase подменяется поддельным клиентом
(`fake_supabase.py`), повторяющим форму API supabase-py. Гоняются в CI при
каждом пуше и пул-реквесте — см. `.github/workflows/tests.yml`, он же
источник истины для списка ниже (каждый файл — отдельный шаг).

```bash
pip install -r requirements.txt              # нужен полный набор: streamlit, matplotlib, python-docx, reportlab
python tests/test_import_adapters.py         # разбор выгрузок: форматы (xlsx, xls, csv), канонизация, починка битого xlsx
python tests/test_import_report.py           # диагностика импорта: что прочитано, что не понято
python tests/test_preprocess.py              # обработка: микротемы, заголовки инфоповодов, сборка таблиц
python tests/test_preprocess_split.py        # контракт распила preprocess.py: ре-экспорт, отсутствие циклов
python tests/test_loadtest_pipeline.py       # инструмент нагрузочного прогона: генератор синтетики, измеритель
python tests/test_message_kinds.py           # природа сообщения: отзыв, комментарий, репост, публикация
python tests/test_story_recovery.py          # восстановление сюжетов: наследование и кластеризация
python tests/test_project_settings.py        # пороги сборки инфоповодов в настройках проекта
python tests/test_reviews.py                 # отзывы о товаре: оценки, шаблон, претензии
python tests/test_audience_metrics.py        # аудитория считается по площадкам, а не по строкам
python tests/test_brand_metrics.py           # индексы бренда против примеров из Metric Calculation Guide
python tests/test_brand_metrics_periods.py   # индексы по периодам: динамика «все периоды», изменение к прошлому
python tests/test_sentiment_markup.py        # тональность без разметки: прочерк вместо ложного нуля во всём продукте
python tests/test_event_titles.py            # склейка похожих заголовков инфоповодов
python tests/test_event_enrichment.py        # связь сообщений с инфоповодами, агрегация
python tests/test_ai_summary.py              # тексты от ИИ (YandexGPT/GigaChat) — без сети
python tests/test_report_highlights.py       # общий top_report_tags/top_report_events для отчёта
python tests/test_period_comparison_daily.py # дневная/недельная/месячная динамика, подпись периода
python tests/test_granularity_ui.py          # гранулярность день/неделя/месяц - сквозной тест от экрана до данных
python tests/test_formatting.py              # короткие даты, подпись периода (общие хелперы)
python tests/test_report_export.py           # выгрузки саммари: PNG-инфографика, Word, PDF
python tests/test_summary_ui.py              # автотекст саммари без ИИ: метрики, динамика, без дублей
python tests/test_session_presence.py        # живые сессии: heartbeat, окно онлайна, очистка
python tests/test_ingest_queue.py            # логика очереди: захват, гонки, ретраи, источники
python tests/test_worker_e2e.py              # воркер целиком: очередь → файл → период проекта
python tests/test_section_boundary.py        # граница отказа раздела не роняет всю страницу
python tests/test_manual_conflict.py         # блокировка правок: конфликт двух редакторов
python tests/test_manual_moderation.py       # ручная модерация: правки, слияния, переносы, счётчики
python tests/test_observability.py           # доставка ошибок владельцу (Sentry/вебхук)
python tests/test_except_hygiene.py          # гигиена except: молчание должно быть обосновано
python tests/test_tag_tier_analytics.py      # аналитика по уровням тегов: own/subtree, покрытие
python tests/test_ui_smoke.py                # интерфейс: приложение стартует, разделы рисуются
python tests/test_roles.py                   # роли: что видит зритель/редактор/владелец, клиентский предпросмотр
```

Каждый тест печатает список проверок и завершается с ненулевым кодом при провале,
поэтому в CI каждый файл — отдельный шаг: видно, какой именно тест упал.

Стандарт репозитория — мутационная проверка при написании теста: внести в
проверяемый код правку, которая обязана всё сломать, и убедиться, что тест
краснеет именно на ней (иначе тест проходит и на сломанном коде). У части
файлов это описано прямо в docstring — раздел «Мутационные проверки».
