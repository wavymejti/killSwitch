# konklawe

Lokalne środowisko wieloagentowe: kilku agentów AI pracuje równolegle nad projektem, a człowiek obserwuje wszystko na żywo. Agenci to oficjalne CLI dostawców uruchamiane w trybie headless, rozliczane z subskrypcji, a nie przez API. Etap 1 (rdzeń) obsługuje Claude Code. Wizja całości jest w `docs/ARCHITECTURE.md`.

## Wymagania

- Python 3.12+ i [uv](https://docs.astral.sh/uv/)
- [Claude Code](https://claude.com/claude-code) zainstalowany i zalogowany subskrypcją (`claude auth status` powinno pokazać `"authMethod": "claude.ai"`)
- **Brak `ANTHROPIC_API_KEY`** w środowisku. Gdy ta zmienna jest ustawiona, Claude Code rozlicza się przez API. konklawe i tak usuwa ją ze środowiska agentów (`strip_env`), ale lepiej jej nie mieć.

Agent `echo` działa bez Claude Code i bez zużycia limitów: odtwarza nagrany strumień.

## Instalacja

```bash
uv sync
uv run konklawe --help
```

## Agenci

Agent to plik `agents/<nazwa>.md`: nagłówek YAML plus rola (prompt dopisywany do domyślnego promptu systemowego CLI). Nowy plik oznacza nowego agenta, bez zmian w kodzie.

```markdown
---
name: recenzent
description: Szuka błędów, ryzyk i słabych punktów
provider: claude
model: sonnet
tools: Read, Grep, Glob             # dozwolone bez pytania
disallowed_tools: Edit, Write, Bash # niedostępne
permission_mode: default            # opcjonalnie; bypassPermissions jest zabronione
provider_options: {}                # dla claude: executable, extra_args
---
Jesteś recenzentem...
```

W trybie headless agent nie może poprosić o zgodę, więc narzędzie spoza `tools` zostanie odrzucone.

## Komendy

```bash
uv run konklawe agents list                         # agenci; błędne pliki na czerwono ze ścieżką
uv run konklawe agents show architekt               # definicja i komenda, która zostałaby uruchomiona

uv run konklawe run echo "test"                     # jedna tura bez zużycia limitów
uv run konklawe run architekt "Opisz strukturę tego katalogu w 3 zdaniach"
uv run konklawe run architekt "A co z testami?" --session <id>   # kontynuacja sesji
uv run konklawe chat recenzent                      # każda linia to tura w tej samej sesji; /exit lub Ctrl+D
uv run konklawe run-many --task architekt="..." --task recenzent="..."   # równolegle

uv run konklawe sessions list                       # sesje z dziennika
uv run konklawe log <id> [--follow] [--verbose]     # odtworzenie sesji z dziennika
uv run konklawe record echo "test" --out /tmp/turn.jsonl          # surowy strumień i .meta.json
```

- Domyślny katalog roboczy to bieżący (`--workdir` go zmienia) i jest wypisywany na starcie.
- `--verbose` pokazuje też wyniki narzędzi, stderr i statusy.
- Ctrl+C przerywa tury i zabija całe drzewa procesów agentów. W `chat` Ctrl+C w trakcie odpowiedzi anuluje tylko bieżącą turę.
- Kwota w stopce (`≈ $X (API estimate, not a charge)`) to szacunek równoważnego kosztu API, a nie opłata.

## Konfiguracja

Opcjonalny `konklawe.toml` w katalogu, z którego uruchamiasz konklawe. Ścieżki względne liczą się od katalogu tego pliku.

```toml
agents_dir = "agents"
data_dir = ".konklawe"          # dziennik SQLite: .konklawe/konklawe.db
max_parallel_turns = 3
turn_timeout_s = 1800
kill_grace_s = 5
store_raw_lines = true
# strip_env = [...]             # domyślnie klucze ANTHROPIC_* i zmienne sesji Claude Code
```

## Testy

```bash
uv run pytest                    # bez sieci i bez zużycia limitów (adapter fake)
uv run pytest -m live            # prawdziwe Claude Code (haiku, 2 krótkie tury) – zużywa limity
uv run ruff check . && uv run ruff format --check .
```

## Dokumentacja

- `docs/ARCHITECTURE.md`: wizja systemu i etapy
- `docs/plan/01-core.md`: plan etapu 1
- `docs/DECISIONS.md`: decyzje podjęte w trakcie
- `docs/notes/`: rozpoznanie Claude Code CLI i formatu jego strumienia
