# konklawe

Lokalne środowisko wieloagentowe w stylu Spotify Xirp. Kilku agentów AI (Claude, GPT, Gemini) pracuje równolegle nad projektem programistycznym, komunikuje się ze sobą i wspólnie podejmuje decyzje. Człowiek obserwuje wszystko na żywo i w każdej chwili może wkroczyć. Nazwa „konklawe” jest robocza.

- Pełna wizja systemu: `docs/ARCHITECTURE.md`
- Bieżący etap: `docs/plan/01-core.md` (**pracujemy wyłącznie w jego zakresie**)
- Decyzje podjęte w trakcie: `docs/DECISIONS.md` (zakładasz i uzupełniasz)

## Zasady nienegocjowalne

1. **Bez API, na subskrypcjach.** Agenci to oficjalne CLI dostawców (`claude`, w późniejszych etapach `codex` i `agy`) uruchamiane jako podprocesy w trybie headless. Nigdy nie wołamy API modeli bezpośrednio. Nie odczytujemy, nie kopiujemy ani nie przekazujemy tokenów logowania z konfiguracji CLI. Nie rotujemy kont.
2. **Klucze API usuwamy ze środowiska podprocesów.** Gdy `ANTHROPIC_API_KEY` jest ustawiony, Claude Code rozlicza się przez API zamiast subskrypcji. Runner domyślnie usuwa takie zmienne ze środowiska dziecka (lista `strip_env` w konfiguracji). Analogiczne zmienne innych dostawców dojdą w etapie 5.
3. **Headless, nie TUI.** Czytamy ustrukturyzowane strumienie (`--output-format stream-json`). Nigdy nie zeskrobujemy interaktywnego terminala: żadnego `tmux send-keys` ani `capture-pane`.
4. **Agenci to dane, nie kod.** Nowy agent to nowy plik w `agents/`. Kod nie zawiera nazw konkretnych agentów ani ról.
5. **Reszta systemu nie zna dostawców.** Wszystko poza `src/konklawe/adapters/` operuje wyłącznie na wspólnym formacie zdarzeń (`events.py`).
6. **Najpierw dziennik, potem reszta.** Każde zdarzenie trafia do dziennika (SQLite) i dostaje numer kolejny, zanim zostanie rozesłane dalej. Jedyną drogą emisji zdarzeń jest funkcja `emit()`.
7. **Nie pal limitów w testach.** Testy domyślnie używają adaptera `fake`. Testy z prawdziwym CLI mają oznaczenie `@pytest.mark.live`, są domyślnie pomijane i uruchamiasz je tylko za wyraźną zgodą użytkownika. To samo dotyczy każdego ręcznego uruchomienia `claude`: najpierw pokaż komendy, potem czekaj na zgodę.
8. **Nigdy domyślnie `--dangerously-skip-permissions`** ani trybu `bypassPermissions`. Uprawnienia agentów wynikają z ich plików definicji.
9. **Parser nigdy nie wywraca tury.** Nieznane zdarzenie albo linia nie-JSON to zdarzenie typu `raw`, a nie wyjątek.

## Stos

- Python 3.12+, zarządzanie projektem przez `uv` (układ `src/`)
- CLI: Typer i Rich
- Modele danych: Pydantic v2; YAML: PyYAML
- Dziennik: SQLite w trybie WAL (biblioteka standardowa)
- Testy: pytest i pytest-asyncio; lint i formatowanie: ruff
- W późniejszych etapach: FastAPI z WebSocketami (dashboard) i oficjalne SDK MCP dla Pythona (tablica)

Nie dodawaj innych zależności bez pytania.

## Komendy

```bash
uv sync                                   # instalacja zależności
uv run pytest                             # testy (bez sieci, bez zużycia limitów)
uv run pytest -m live                     # testy z prawdziwym CLI – tylko za zgodą
uv run ruff check . && uv run ruff format .
uv run konklawe --help
```

## Konwencje

- Kod, identyfikatory, komentarze i komunikaty commitów po angielsku. Dokumentacja w `docs/` po polsku.
- Pełne typowanie. Cała praca z procesami na asyncio. Żadnych blokujących wywołań w pętli zdarzeń (blokujące I/O przez `asyncio.to_thread`).
- Czas zawsze w UTC (ISO 8601).
- Małe, spójne commity. Po każdym zadaniu z planu testy są zielone, a ruff czysty.
- Formaty strumieni CLI zmieniają się między wersjami. Przed napisaniem parsera nagraj prawdziwe wyjście do `tests/fixtures/` i testuj na nim. Flagi weryfikuj przez `claude --help`, nie z pamięci.

## Jak pracujemy

1. Przed rozpoczęciem etapu przeczytaj jego plan i przedstaw użytkownikowi swój plan działania oraz pytania.
2. Realizuj zadania w kolejności z planu. Po każdym zadaniu krótko raportuj, co zostało zrobione i jak to sprawdzić.
3. Gdy plan jest niejasny albo kłóci się z rzeczywistym zachowaniem CLI, zatrzymaj się i zapytaj, zamiast zgadywać.
4. Każdą decyzję projektową podjętą w trakcie zapisz w `docs/DECISIONS.md`: data, decyzja, powód.
