# Etap 1: Core

**Prompt startowy dla Claude Code:**

> Przeczytaj CLAUDE.md, docs/ARCHITECTURE.md i docs/plan/01-core.md. Realizujemy etap 1 (core). Najpierw przedstaw plan działania i pytania, potem zacznij od zadania 0.

## Cel etapu

Działający rdzeń systemu, na razie bez interfejsu webowego:

- agenci wczytywani z plików,
- uruchamiani jako procesy `claude -p` w trybie headless,
- ich strumienie tłumaczone na wspólny format zdarzeń, zapisywane w dzienniku SQLite i wyświetlane na żywo w terminalu,
- kilku agentów pracuje równolegle,
- sesje można kontynuować (wznowienie po ID),
- historię każdej sesji można odtworzyć z dziennika.

Efekt widoczny dla użytkownika: `konklawe run-many` uruchamia dwóch agentów naraz i widać na żywo, co każdy z nich „mówi” i robi.

## Zakres

**W zakresie:**

- szkielet projektu
- konfiguracja
- definicje agentów
- wspólny format zdarzeń
- dziennik
- szyna zdarzeń
- adaptery `claude` i `fake`
- runner
- orkiestrator
- renderer terminalowy
- CLI
- testy

**Poza zakresem.** Nie implementuj tych rzeczy, ale nie zamykaj im drogi:

- tablica MCP i komunikacja między agentami (etap 2)
- projekty i git worktree (etap 2); w etapie 1 sesja pracuje w podanym katalogu
- podsumowania tur (etap 2)
- dashboard i FastAPI (etap 3)
- workflowy, głosowanie, kierownik (etap 4)
- adaptery `codex` i `agy`, zmiana wykonawcy, przejmowanie sesji (etap 5)

## Struktura po etapie 1

```
konklawe/
  CLAUDE.md
  README.md
  pyproject.toml
  .gitignore
  agents/
    architekt.md  recenzent.md  echo.md
  docs/
    ARCHITECTURE.md  DECISIONS.md
    plan/01-core.md
    notes/claude-cli.md  notes/claude-stream-format.md
  src/konklawe/
    __init__.py
    cli.py           # komendy Typer
    config.py        # Settings + konklawe.toml
    agents.py        # wczytywanie i walidacja definicji agentów
    events.py        # wspólny format zdarzeń
    store.py         # dziennik SQLite
    bus.py           # szyna zdarzeń + emit()
    runner.py        # wykonanie jednej tury (podproces)
    orchestrator.py  # sesje, równoległość, sprzątanie
    render.py        # renderowanie zdarzeń w terminalu (Rich)
    fake_cli.py      # udawane CLI odtwarzające nagrania
    adapters/
      __init__.py    # rejestr adapterów
      base.py
      claude.py
      fake.py
  tests/
    fixtures/claude/*.jsonl
    test_agents.py  test_config.py  test_store.py  test_bus.py
    test_claude_adapter.py  test_runner.py  test_orchestrator.py
    test_render.py  test_live.py
```

## Zadania

### Zadanie 0: Rozpoznanie Claude Code CLI (przed pisaniem kodu)

Cel: oprzeć parser na rzeczywistym wyjściu, a nie na założeniach.

1. Uruchom `claude --version` i `claude --help`. W `docs/notes/claude-cli.md` zapisz wersję i opis flag istotnych dla projektu:
   - `-p`, `--output-format`, `--input-format`, `--verbose`
   - `--include-partial-messages`, `--resume`, `--model`
   - `--allowedTools`, `--disallowedTools`, `--permission-mode`
   - `--append-system-prompt` (oraz ewentualny wariant z plikiem), `--mcp-config`

   Zapisz też, w jakim formacie przekazuje się listy narzędzi (jeden argument z przecinkami czy kilka argumentów).
2. Sprawdź, czy prompt można podać przez stdin (`echo "..." | claude -p ...`). Jeśli tak, runner podaje prompt przez stdin. Linia poleceń na Windowsie ma limit ok. 32 tys. znaków, a prompty i role bywają długie.
3. Upewnij się, że `ANTHROPIC_API_KEY` nie jest ustawiony w środowisku (`env | grep ANTHROPIC`).
4. **Najpierw pokaż użytkownikowi listę komend i poczekaj na zgodę, bo to zużywa limity subskrypcji.** Używaj najtańszego modelu (np. `--model haiku`) i krótkich promptów. Nagraj surowe strumienie do `tests/fixtures/claude/`:
   - `simple_text.jsonl`: krótka odpowiedź tekstowa z `--include-partial-messages`
   - `tool_use.jsonl`: prompt wymuszający odczyt pliku (np. „przeczytaj README.md i streść w jednym zdaniu”) przy dozwolonym `Read`
   - `resume_turn2.jsonl`: druga tura z `--resume <id>` z poprzedniego nagrania, z pytaniem odwołującym się do pierwszej tury
   - `tool_denied.jsonl`: prompt próbujący zapisać plik przy zabronionym `Write`
   - `error.jsonl`: przypadek błędu, np. nieistniejący model

   Przy każdym nagraniu zapisz plik `<nazwa>.meta.json` z kodem wyjścia, treścią stderr i dokładną komendą.
5. W `docs/notes/claude-stream-format.md` opisz zaobserwowane typy zdarzeń i ich pola. To dokumentacja dla parsera.
6. Spróbuj ustalić, jak wygląda sygnał wyczerpanego limitu subskrypcji (z dokumentacji lub wyjścia). Jeśli się nie da, zostaw TODO; heurystyka powstanie w zadaniu 7.

**Gotowe gdy:** fixtures istnieją, a notatki opisują format i flagi.

### Zadanie 1: Szkielet projektu

- `uv init --package konklawe` (układ `src/`), Python ≥ 3.12.
- Zależności: `typer`, `rich`, `pydantic>=2`, `pyyaml`. Dev: `pytest`, `pytest-asyncio`, `ruff`.
- `pyproject.toml`:
  - punkt wejścia `konklawe = "konklawe.cli:app"`
  - ruff: `line-length = 100`, reguły `E, F, I, UP, B, ASYNC`
  - pytest: `asyncio_mode = "auto"`, marker `live`, domyślnie `addopts = "-m 'not live'"`
- `.gitignore`: `.konklawe/`, `.venv/`, `__pycache__/`, `*.pyc`.
- Utwórz pusty `docs/DECISIONS.md` z nagłówkiem tabeli: data | decyzja | powód.

**Gotowe gdy:** działa `uv run konklawe --help`, `uv run pytest` przechodzi, a ruff jest czysty.

### Zadanie 2: Konfiguracja (`config.py`)

Model `Settings` (Pydantic), wczytywany z opcjonalnego pliku `konklawe.toml` w bieżącym katalogu (`tomllib`), z wartościami domyślnymi:

| Pole | Domyślnie | Opis |
|---|---|---|
| `agents_dir` | `"agents"` | katalog z definicjami agentów |
| `data_dir` | `".konklawe"` | baza danych i pliki robocze |
| `max_parallel_turns` | `3` | globalny limit równoległych tur |
| `turn_timeout_s` | `1800` | maksymalny czas jednej tury |
| `kill_grace_s` | `5` | czas między łagodnym a twardym zabiciem procesu |
| `strip_env` | `["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"]` | zmienne usuwane ze środowiska procesów agentów |
| `store_raw_lines` | `true` | czy zapisywać w dzienniku surowe linie strumienia |
| `stdout_line_limit` | `16 * 1024 * 1024` | limit długości linii przy czytaniu strumienia |

Nieznany klucz w TOML zgłaszaj jako błąd z nazwą klucza. Ścieżki względne rozwiązuj względem katalogu z plikiem konfiguracji (albo bieżącego, gdy pliku nie ma).

**Testy:** wartości domyślne, nadpisanie z pliku, nieznany klucz.

### Zadanie 3: Definicje agentów (`agents.py`)

Format pliku:

```markdown
---
name: recenzent
description: Szuka błędów i ryzyk
provider: claude
model: sonnet
tools: Read, Grep, Glob
disallowed_tools: Edit, Write, Bash
permission_mode: default
provider_options: {}
---
Treść pliku to rola agenta, dopisywana do domyślnego promptu systemowego CLI.
```

Model `AgentSpec` (Pydantic, `extra="forbid"`):

| Pole | Typ | Zasady |
|---|---|---|
| `name` | `str` | slug `^[a-z0-9][a-z0-9-]{0,39}$`, równy nazwie pliku bez `.md` |
| `description` | `str` | wymagany, niepusty |
| `provider` | `str` | musi istnieć w rejestrze adapterów |
| `model` | `str \| None` | przekazywany dostawcy bez zmian |
| `tools` | `list[str]` | lista YAML albo napis rozdzielony przecinkami |
| `disallowed_tools` | `list[str]` | jak wyżej |
| `permission_mode` | `str \| None` | walidowany przez adapter |
| `provider_options` | `dict[str, Any]` | walidowane przez adapter |
| `role_prompt` | `str` | treść pod nagłówkiem, niepusta po `strip()` |
| `source_path` | `Path` | ustawiane przez loader |

Funkcje:

- `load_agent(path) -> AgentSpec`
- `load_agents(dir) -> dict[str, AgentSpec]`: wczytuje wszystkie pliki `*.md` i zbiera błędy ze wszystkich plików naraz. Rzuca `AgentLoadError` z listą par (ścieżka, komunikat). Zduplikowana nazwa jest błędem.
- Parsowanie: plik musi zaczynać się od linii `---`, a nagłówek kończy się na następnej linii `---`. YAML wczytuj przez `yaml.safe_load`.
- Po walidacji modelu wywołaj `adapter.validate_agent(spec)` i dołącz jego błędy do tej samej listy.

**Testy:** poprawny plik, brak nagłówka, nieznane pole, nazwa niezgodna z plikiem, `tools` jako napis i jako lista, nieznany dostawca, kilka błędnych plików zgłoszonych naraz.

### Zadanie 4: Wspólny format zdarzeń (`events.py`)

```python
class EventType(StrEnum):
    STATUS = "status"
    SESSION_STARTED = "session_started"
    TEXT_DELTA = "text_delta"
    MESSAGE = "message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    TURN_COMPLETED = "turn_completed"
    RATE_LIMITED = "rate_limited"
    ERROR = "error"
    STDERR = "stderr"
    PROCESS_EXITED = "process_exited"
    RAW = "raw"

class ParsedEvent(BaseModel):   # zwracane przez parser adaptera
    type: EventType
    data: dict[str, Any]
    raw: str | None = None

class AgentEvent(BaseModel):    # zapisywane w dzienniku i rozsyłane dalej
    seq: int | None = None      # nadaje dziennik
    ts: datetime                # UTC
    session_id: str
    turn_id: str | None
    agent: str
    provider: str
    type: EventType
    data: dict[str, Any]
    raw: str | None = None
```

Kontrakt pola `data`:

| Typ | `data` |
|---|---|
| `status` | `{state, reason?}`; `state` ∈ `starting, running, idle, done, failed, cancelled, limited` |
| `session_started` | `{provider_session_id, model?, tools?: list[str]}` |
| `text_delta` | `{text, message_id?}` |
| `message` | `{text, message_id?, from_deltas: bool}`; `from_deltas=true`, gdy wcześniej przyszły delty tej wiadomości (renderer jej wtedy nie powtarza) |
| `tool_call` | `{tool_id, name, input_summary}`; skrót ≤ 300 znaków, np. ścieżka pliku albo komenda |
| `tool_result` | `{tool_id, is_error, output_summary}`; skrót ≤ 2000 znaków |
| `turn_completed` | `{result_text?, is_error, duration_ms?, num_turns?, usage?: dict, cost_estimate_usd?: float, provider_session_id?}` |
| `rate_limited` | `{message, resets_at?: datetime}` |
| `error` | `{message, fatal: bool}` |
| `stderr` | `{line}` |
| `process_exited` | `{exit_code, signal?, duration_ms}` |
| `raw` | `{note?}`; nierozpoznana linia jest w polu `raw` |

`cost_estimate_usd` przy subskrypcji jest szacunkiem równoważnego kosztu API, a nie realną opłatą. Tak też ma być wyświetlany.

### Zadanie 5: Dziennik (`store.py`)

SQLite w `{data_dir}/konklawe.db` z `PRAGMA journal_mode=WAL` i `PRAGMA foreign_keys=ON`.

```sql
CREATE TABLE schema_version (version INTEGER NOT NULL);

CREATE TABLE sessions (
  id                  TEXT PRIMARY KEY,
  agent               TEXT NOT NULL,
  provider            TEXT NOT NULL,      -- bieżący wykonawca
  provider_session_id TEXT,
  workdir             TEXT NOT NULL,
  status              TEXT NOT NULL,
  created_at          TEXT NOT NULL,
  updated_at          TEXT NOT NULL
);

CREATE TABLE turns (
  id           TEXT PRIMARY KEY,
  session_id   TEXT NOT NULL REFERENCES sessions(id),
  idx          INTEGER NOT NULL,
  provider     TEXT NOT NULL,             -- wykonawca tej konkretnej tury
  prompt       TEXT NOT NULL,
  status       TEXT NOT NULL,
  started_at   TEXT NOT NULL,
  finished_at  TEXT,
  result_text  TEXT,
  is_error     INTEGER,
  usage_json   TEXT,
  UNIQUE (session_id, idx)
);

CREATE TABLE events (
  seq        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         TEXT NOT NULL,
  session_id TEXT NOT NULL REFERENCES sessions(id),
  turn_id    TEXT REFERENCES turns(id),
  agent      TEXT NOT NULL,
  provider   TEXT NOT NULL,
  type       TEXT NOT NULL,
  data_json  TEXT NOT NULL,
  raw        TEXT
);
CREATE INDEX idx_events_session_seq ON events(session_id, seq);
```

Klasa `EventStore` z metodami async:

- `create_session`, `update_session`, `get_session`, `list_sessions`
- `create_turn`, `finish_turn`
- `append_event(event) -> AgentEvent`: zwraca zdarzenie z nadanym `seq`
- `iter_events(session_id=None, after_seq=0, limit=None)`
- `migrate()`: prosta migracja oparta na `schema_version`

Implementacja zapisu: jedno połączenie do zapisu, serializowane blokadą i wywoływane przez `asyncio.to_thread`, albo kolejka z jednym taskiem-zapisującym. Wybierz prostsze rozwiązanie i uzasadnij wybór w `DECISIONS.md`. Identyfikatory sesji i tur: `uuid4().hex[:12]`.

**Testy:** rosnące `seq`, odczyt od `after_seq`, równoległe `append_event` z wielu tasków bez utraty zdarzeń, ponowne otwarcie bazy i odczyt.

### Zadanie 6: Szyna zdarzeń (`bus.py`)

- `EventBus` z `subscribe(session_id: str | None = None)`, który zwraca asynchroniczny iterator, oraz `publish(event)`.
- Każdy subskrybent ma `asyncio.Queue(maxsize=1000)`. Przy przepełnieniu odrzucaj najstarsze zdarzenia i wstaw jedno ostrzeżenie dla tego subskrybenta. **Runner nigdy nie czeka na wolnego odbiorcę.**
- `async def emit(store, bus, event) -> AgentEvent`: najpierw `store.append_event` (nadaje `seq`), potem `bus.publish`. To jedyna droga emisji zdarzeń w systemie.

**Testy:** wielu subskrybentów, filtr po sesji, przepełnienie nie blokuje publikującego.

### Zadanie 7: Adaptery (`adapters/`)

**Interfejs (`base.py`):**

```python
@dataclass
class CommandSpec:
    argv: list[str]
    stdin_text: str | None = None
    env_overrides: dict[str, str] = field(default_factory=dict)

@dataclass
class TurnRequest:
    agent: AgentSpec
    prompt: str
    workdir: Path
    resume_id: str | None          # provider_session_id z poprzedniej tury

class TurnParser(ABC):             # stanowy, jeden na turę
    @abstractmethod
    def feed(self, line: str) -> list[ParsedEvent]: ...
    @abstractmethod
    def finish(self, exit_code: int | None, stderr_tail: list[str]) -> list[ParsedEvent]: ...

class ProviderAdapter(ABC):
    name: ClassVar[str]
    @abstractmethod
    def validate_agent(self, spec: AgentSpec) -> list[str]: ...   # lista błędów
    @abstractmethod
    def build_command(self, req: TurnRequest) -> CommandSpec: ...
    @abstractmethod
    def new_parser(self) -> TurnParser: ...
```

Rejestr w `adapters/__init__.py` udostępnia `get_adapter(name)` i `available_providers()`.

**`claude.py`.** Docelowy kształt komendy (potwierdź w zadaniu 0):

```
claude -p --output-format stream-json --verbose --include-partial-messages
       [--resume <resume_id>] [--model <model>]
       [--allowedTools ...] [--disallowedTools ...] [--permission-mode <mode>]
       --append-system-prompt <role_prompt>
       [extra_args z provider_options]
       [prompt jako argument, jeśli nie da się przez stdin]
```

- `provider_options` dla Claude: `executable` (domyślnie `"claude"`) i `extra_args: list[str]`. Każdy inny klucz to błąd walidacji.
- Błąd walidacji: `extra_args` zawierające `--dangerously-skip-permissions` albo `bypassPermissions`, oraz `permission_mode: bypassPermissions`.
- Mapowanie zdarzeń (dopasuj do fixtures z zadania 0):

| Zdarzenie Claude | Wspólny format |
|---|---|
| `system` z podtypem `init` | `session_started` (ID sesji, model, narzędzia) |
| `stream_event` z deltą tekstu | `text_delta` |
| `assistant`, blok `text` | `message` (`from_deltas` zależnie od tego, czy wcześniej były delty tej wiadomości) |
| `assistant`, blok `tool_use` | `tool_call` |
| `user`, blok `tool_result` | `tool_result` |
| `result` | `turn_completed` (wraz z `provider_session_id`; `total_cost_usd` trafia do `cost_estimate_usd`) |
| sygnał wyczerpanego limitu | `rate_limited` |
| inny lub nieznany typ, linia nie-JSON | `raw` (bez wyjątku) |

- Pozostałe `stream_event` (start i koniec bloków itp.) pomijaj albo zapisuj jako `raw`. Zdecyduj na podstawie fixtures, żeby nie zaśmiecać dziennika, i zapisz decyzję w `DECISIONS.md`.
- Wykrywanie limitu: jedna funkcja `detect_rate_limit(...)` z wzorcami w jednym miejscu, testy na sztucznych przykładach i TODO do potwierdzenia na prawdziwym wyjściu.
- `finish()`: gdy nie było zdarzenia `result`, a kod wyjścia ≠ 0, emituj `error` (krytyczny) z ostatnimi liniami stderr.

**Testy:** parser na każdym fixture (porównanie z oczekiwaną listą zdarzeń), budowanie komendy dla różnych `AgentSpec`, walidacja `provider_options`, deduplikacja delt i pełnych wiadomości.

**`fake_cli.py` i `fake.py`:**

- `python -m konklawe.fake_cli` przyjmuje argumenty:
  - `--fixture PATH`
  - `--delay-ms N`
  - `--exit-code N`
  - `--stderr TEXT`
  - `--hang` (wiesza się, do testów timeoutu)
  - `--report-env NAME...` (wypisuje linię JSON z informacją, czy dane zmienne są ustawione, do testu `strip_env`)

  Wypisuje linie nagrania na stdout z opóźnieniem i robi flush po każdej linii.
- `FakeAdapter.build_command` zwraca `[sys.executable, "-m", "konklawe.fake_cli", ...]`. Opcje z `provider_options`:
  - `fixture` (wymagane; ścieżka rozwiązywana do absolutnej względem katalogu głównego konklawe)
  - `replay_of` (domyślnie `"claude"`)
  - `delay_ms` (domyślnie 20)
  - `exit_code`
  - `hang`
  - `fixture_resume` (opcjonalne nagranie dla tur ze wznowieniem)
- `new_parser()` zwraca parser adaptera wskazanego w `replay_of`. Testujesz w ten sposób prawdziwy parser i prawdziwą ścieżkę podprocesu bez zużywania limitów.

### Zadanie 8: Runner (`runner.py`)

`async def run_turn(session, agent, prompt, *, adapter, store, bus, settings) -> TurnOutcome`

1. Utwórz turę w dzienniku i emituj `status: starting`.
2. Zbuduj komendę przez `adapter.build_command(...)`. Środowisko dziecka to `os.environ` bez kluczy z `strip_env`, plus `env_overrides`.
3. Uruchom proces: `asyncio.create_subprocess_exec(*argv, cwd=workdir, stdin=PIPE lub DEVNULL, stdout=PIPE, stderr=PIPE, limit=settings.stdout_line_limit, ...)` we własnej grupie procesów:
   - POSIX: `start_new_session=True`
   - Windows: `creationflags=subprocess.CREATE_NEW_PROCESS_GROUP`
4. Jeśli jest `stdin_text`, zapisz go i zamknij stdin.
5. Równolegle (`asyncio.TaskGroup`) czytaj oba strumienie:
   - stdout linia po linii → `parser.feed` → `emit` każdego zdarzenia; pierwsze zdarzenie z treścią → `status: running`,
   - stderr linia po linii → `emit(stderr)`; trzymaj ostatnie 50 linii.

   Oba strumienie muszą być czytane jednocześnie, inaczej proces zablokuje się na pełnym buforze.
6. Czekaj na zakończenie z limitem `turn_timeout_s`. Przy timeoucie albo anulowaniu wywołaj `terminate_tree()`:
   - POSIX: `os.killpg(pgid, SIGTERM)`, po `kill_grace_s` `SIGKILL`
   - Windows: `taskkill /T /F /PID <pid>`

   Następnie emituj `status: cancelled` albo `failed`.
7. Emituj zdarzenia z `parser.finish(exit_code, stderr_tail)`, a potem `process_exited`.
8. Zaktualizuj turę (status, `result_text`, `is_error`, `usage`) i sesję (`provider_session_id` z `session_started` lub `turn_completed`; status `idle`, `failed` albo `limited`).
9. Każdy nieprzewidziany wyjątek kończy się emisją `error` (krytyczny) i statusem tury `failed`. Blok `try/finally` gwarantuje, że nie zostanie proces-sierota.

Dekodowanie linii: UTF-8 z `errors="replace"`, bez końcówek `\r\n`, puste linie pomijane. Przy `LimitOverrunError` emituj niekrytyczny `error`, odrzuć resztę tej linii i czytaj dalej.

`TurnOutcome`: `turn_id, status, result_text, is_error, provider_session_id, duration_ms`.

**Testy (na adapterze `fake`):**

- poprawna tura
- kod błędu ze stderr
- timeout (`--hang`) zabija proces
- anulowanie w trakcie tury
- linia dłuższa niż 64 KiB
- zmienne ze `strip_env` nie trafiają do procesu dziecka (`--report-env`)

### Zadanie 9: Orkiestrator (`orchestrator.py`)

Klasa `Orchestrator(settings, store, bus, agents)`:

- `new_session(agent_name, workdir) -> Session`: zapis w dzienniku ze statusem `idle`. `workdir` musi istnieć i jest zapisywany jako ścieżka absolutna.
- `send(session_id, prompt) -> TurnOutcome`: uruchamia turę i przy wznowieniu przekazuje `provider_session_id` sesji. Blokada per sesja (jedna tura naraz) i globalny semafor `max_parallel_turns`.
- `cancel(session_id)` oraz `shutdown()`: anuluje wszystkie tury i czeka na sprzątanie.
- Sesja zapisuje `provider` jako bieżącego wykonawcę. To przygotowanie pod zmianę wykonawcy w etapie 5, samej zmiany nie implementuj.

**Testy:**

- dwie sesje pracują równolegle
- semafor ogranicza równoległość
- druga tura tej samej sesji czeka na pierwszą
- `resume_id` jest przekazany w drugiej turze
- `shutdown()` nie zostawia procesów

### Zadanie 10: Renderowanie i CLI (`render.py`, `cli.py`)

**Renderer** (Rich) jest wspólny dla podglądu na żywo i odtwarzania z dziennika:

- Każdy agent ma stały kolor i prefiks `[nazwa]`.
- `text_delta` wypisuj strumieniowo. Gdy pracuje kilku agentów naraz, buforuj tekst osobno dla każdego agenta i wypisuj pełne linie z prefiksem, żeby znaki się nie mieszały.
- `message`: przy `from_deltas=true` nie powtarzaj, przy `false` wypisz.
- `tool_call`: przygaszone `▸ Nazwa skrót_argumentów`.
- `tool_result`: błąd na czerwono jako `✗ ...`; udane wyniki domyślnie pomijaj (pokazuje je `--verbose`).
- `stderr`: przygaszone, tylko z `--verbose`.
- `rate_limited` i `error`: wyraźnie wyróżnione.
- `turn_completed`: stopka z czasem, liczbą kroków, zużyciem tokenów i `≈ $X (szacunek API, nie opłata)`.

**Komendy (Typer):**

| Komenda | Opis |
|---|---|
| `konklawe agents list` | tabela: nazwa, dostawca, model, opis; błędy plików na czerwono |
| `konklawe agents show <nazwa>` | pełna definicja plus komenda, która zostałaby uruchomiona (dry-run) |
| `konklawe run <agent> <prompt> [--workdir DIR] [--session ID] [--verbose]` | jedna tura; bez `--session` tworzy nową sesję; na końcu wypisuje ID sesji |
| `konklawe chat <agent> [--workdir DIR] [--session ID]` | pętla: każda linia to nowa tura w tej samej sesji; wyjście przez `/exit` albo Ctrl+D |
| `konklawe run-many --task <agent>=<prompt> ... [--workdir DIR]` | kilku agentów równolegle, każdy w osobnej sesji |
| `konklawe sessions list` | ID, agent, dostawca, status, liczba tur, ostatnia aktywność |
| `konklawe log <session_id> [--follow] [--verbose]` | odtworzenie z dziennika tym samym rendererem; `--follow` dociąga nowe zdarzenia (odpytywanie co 300 ms po `seq`) |
| `konklawe record <agent> <prompt> --out FILE` | zapis surowego stdout do pliku (nagrywanie fixtures); stderr, kod wyjścia i komenda trafiają do `FILE.meta.json` |

Domyślny `--workdir` to bieżący katalog. Zawsze wypisuj go na początku działania. Ctrl+C wywołuje `orchestrator.shutdown()`.

### Zadanie 11: Przykładowi agenci

W repozytorium są już `agents/architekt.md` i `agents/recenzent.md` (Claude, tylko odczyt) oraz `agents/echo.md` (`fake`). Dopasuj ścieżki w `echo.md` do nazw fixtures z zadania 0. `konklawe agents list` ma pokazać wszystkich trzech bez błędów.

### Zadanie 12: Testy końcowe i dokumentacja

- `uv run pytest` przechodzi bez sieci, a ruff jest czysty.
- Jeden test `@pytest.mark.live` w `test_live.py`: prawdziwa tura `claude` (model `haiku`, krótki prompt) plus druga tura z wznowieniem, w której agent odwołuje się do pierwszej. Uruchom go raz, za zgodą użytkownika.
- `README.md` opisuje instalację, wymagania (zainstalowany i zalogowany Claude Code, brak `ANTHROPIC_API_KEY`) i przykłady komend.
- `docs/DECISIONS.md` jest uzupełniony.

## Definicja ukończenia (sprawdza użytkownik)

1. `uv run konklawe agents list` pokazuje trzech agentów, a zepsuty plik agenta daje czytelny błąd ze ścieżką.
2. `uv run konklawe run architekt "Opisz strukturę tego katalogu w 3 zdaniach"`: tekst pojawia się na żywo, wywołania narzędzi są widoczne, a na końcu jest stopka i ID sesji.
3. `uv run konklawe chat recenzent`: w drugiej wiadomości agent pamięta pierwszą.
4. `uv run konklawe run-many --task architekt="..." --task recenzent="..."`: obaj agenci pracują równolegle, a wyjście jest czytelne.
5. `uv run konklawe log <id>` odtwarza sesję tak samo, jak wyglądała na żywo.
6. Ctrl+C w trakcie `run-many` kończy wszystko czysto, a `ps` nie pokazuje osieroconych procesów `claude`.
7. `uv run konklawe run echo "test"` działa bez zużycia limitów.
8. Dodanie nowego pliku w `agents/` daje nowego agenta bez zmian w kodzie.
9. Testy przechodzą bez dostępu do sieci.

## Pułapki (przeczytaj przed implementacją)

- **`ANTHROPIC_API_KEY` w środowisku** oznacza rozliczenie przez API zamiast subskrypcji. Dlatego `strip_env` usuwa go ze środowiska dziecka.
- **`stream-json` wymaga `--verbose`.** W trybie `-p` bez tej flagi CLI zgłasza błąd.
- **Limit linii w asyncio.** Domyślny limit `asyncio.StreamReader` to 64 KiB, a linie `stream-json` bywają dłuższe (np. wyniki narzędzi). Ustaw parametr `limit`.
- **Dwa strumienie naraz.** Czytaj stdout i stderr równolegle.
- **Podwójny tekst.** Z `--include-partial-messages` przychodzą i delty, i pełne wiadomości. Bez deduplikacji tekst wyświetli się dwa razy.
- **Brak pytań o zgodę.** W trybie headless agent nie może poprosić o zgodę, więc narzędzie spoza dozwolonych zostanie odrzucone. Uprawnienia definiuj w pliku agenta.
- **Całe drzewo procesów.** Zabijaj całą grupę procesów, a nie tylko bezpośrednie dziecko, bo CLI uruchamia własne podprocesy (komendy, serwery MCP).
- **Windows.** Linia poleceń ma limit długości, a grupy procesów działają inaczej.
- **Koszt to szacunek.** `total_cost_usd` przy subskrypcji jest szacunkiem, a nie opłatą.
- **Formaty się zmieniają.** Format strumienia zmienia się z wersjami CLI, dlatego parser na nieznanym zdarzeniu emituje `raw`, a nie rzuca wyjątku.
