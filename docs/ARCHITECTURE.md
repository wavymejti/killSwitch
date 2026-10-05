# Architektura systemu konklawe

> Dokument opisuje docelową wizję całego systemu. Implementujemy go etapami (sekcja 9), a bieżący etap ma własny, szczegółowy plan w `docs/plan/`. Informacje o CLI dostawców są aktualne na październik 2026. Przed implementacją każdego adaptera trzeba je zweryfikować (`--help` i nagrane strumienie).

## 1. Cel

Lokalna aplikacja, w której kilku agentów AI na różnych modelach (Claude, GPT, Gemini) pracuje równolegle nad projektem programistycznym, rozmawia ze sobą i wspólnie podejmuje decyzje. Użytkownik obserwuje wszystko na żywo w przeglądarce i w każdej chwili może wkroczyć.

Inspiracją jest Spotify Xirp, czyli warstwa orkiestracji i UI nad CLI agentów. Kluczowa różnica: Xirp to kokpit dla człowieka, który sam prowadzi wiele niezależnych sesji. konklawe to zespół agentów, który koordynuje się sam, a człowiek go nadzoruje.

## 2. Scenariusz docelowy

Użytkownik wybiera workflow „zespół z kierownikiem” i wpisuje zadanie „dodaj logowanie do aplikacji”. System uruchamia czterech agentów (kierownik, architekt, backend, recenzent), każdego w osobnym git worktree. Kierownik rozdziela pracę przez tablicę. Backend pisze kod i odsyła krótkie podsumowanie. Recenzent sprawdza zmiany i głosuje za poprawką, a kierownik podejmuje decyzję. W zakładce „Widok terminali” widać na żywo, co robi każdy agent. Gdy dyskusja przekroczy limit rund, sprawa trafia do użytkownika.

## 3. Zasady

Pełna lista zasad znajduje się w `CLAUDE.md`. Uzasadnienie najważniejszych:

- **Subskrypcje zamiast API.** Używamy wyłącznie oficjalnych programów CLI dostawców, zalogowanych subskrypcjami użytkownika. Powody są dwa: koszt oraz zgodność z warunkami dostawców. Anthropic dopuszcza użycie tokenów z planów Free, Pro i Max wyłącznie w Claude Code i Claude.ai, więc wyciąganie ich do własnego kodu jest zabronione.
- **Użytek osobisty.** Udostępnienie systemu innym osobom wymagałoby przejścia na klucze API. To osobna decyzja, poza zakresem projektu.
- **Limity subskrypcji to budżet systemu.** Obsługa wyczerpanych limitów jest funkcją pierwszej klasy, a nie wyjątkiem.

## 4. Pojęcia

| Pojęcie | Czym jest | Gdzie żyje |
|---|---|---|
| Agent | Rola zdefiniowana w pliku `agents/<nazwa>.md`: prompt roli, dostawca, model, uprawnienia | pliki |
| Dostawca (provider) | Konkretne CLI: `claude`, `codex`, `agy` albo `fake` (testowy) | adapter |
| Wykonawca | Dostawca przypisany w danej chwili do sesji. Może się zmienić w trakcie pracy (etap 5) | sesja |
| Projekt | Repozytorium docelowe, nad którym pracują agenci (etap 2) | konfiguracja |
| Sesja | Kontener pracy: katalog roboczy (worktree), ID sesji u dostawcy, historia tur, podsumowania | dziennik |
| Tura | Jedno wywołanie procesu CLI z jednym promptem | dziennik |
| Zdarzenie | Jednostka strumienia we wspólnym formacie, z numerem kolejnym | dziennik |
| Workflow | Plik `workflows/<nazwa>.yaml`: uczestnicy, topologia, reguła decyzji, limity (etap 4) | pliki |
| Tablica | Lokalny serwer MCP: wiadomości, propozycje, głosy, podsumowania (etap 2 i dalej) | serwer |

Dwa rozróżnienia są kluczowe i pochodzą z lekcji z Xirpa:

- **Sesja to nie to samo co agent ani wykonawca.** Agent to rola, wykonawca to program, który ją aktualnie odgrywa, a sesja to kontener ze stanem. Dzięki temu wykonawcę można podmienić bez utraty pracy.
- **Sesja i worktree mają niezależne cykle życia.** Zamknięcie sesji nie usuwa worktree, a usunięcie worktree nie zamyka sesji.

## 5. Komponenty

### 5.1 Definicje agentów

Każdy agent to plik Markdown z nagłówkiem YAML. Format jest w duchu zgodny z subagentami Claude Code: pola `name`, `description`, `tools` i `model` plus treść pliku jako prompt. Pozostałe pola są nasze.

```markdown
---
name: recenzent
description: Szuka błędów, ryzyk i słabych punktów w kodzie i propozycjach
provider: claude
model: sonnet
tools: Read, Grep, Glob
disallowed_tools: Edit, Write, Bash
provider_options: {}
---
Jesteś recenzentem. Twoim jedynym celem jest znajdowanie problemów...
```

- `name` to slug równy nazwie pliku.
- `tools` i `disallowed_tools` przyjmują listę YAML albo napis rozdzielony przecinkami.
- `provider_options` to natywne opcje danego dostawcy, walidowane przez jego adapter. To furtka na rzeczy, których wspólne pola nie wyrażają.
- Nieznane pola w nagłówku są błędem, żeby literówki nie przechodziły po cichu.
- W etapie 5 dojdzie pole `fallback_providers` (lista zapasowych dostawców).
- Rola agenta w workflow, np. kierownik czy pracownik, jest definiowana w pliku workflow, a nie w pliku agenta. Ten sam agent może grać różne role w różnych workflowach.

Uprawnienia nie przekładają się 1:1 między dostawcami. Claude ma tryby uprawnień i listy narzędzi, Codex ma tryby piaskownicy. Adapter mapuje wspólne pola najlepiej, jak się da, a resztę obsługuje przez `provider_options`.

### 5.2 Adaptery dostawców

Każdy dostawca ma adapter, który:

1. waliduje definicję agenta (`provider_options`, tryb uprawnień),
2. buduje komendę dla tury (argv, opcjonalny tekst na stdin, nadpisania zmiennych środowiska),
3. tworzy stanowy parser tury, który zamienia linie strumienia na zdarzenia we wspólnym formacie.

**Model tur:** każda tura to jedno wywołanie procesu, a kontynuacja rozmowy odbywa się przez wznowienie sesji dostawcy po jej ID. Claude potrafi więcej, bo z `--input-format stream-json` może utrzymywać stały proces, ale dla jednolitości wszystkich dostawców tego nie używamy.

| Dostawca | CLI | Tryb headless | Wznawianie | Uwagi |
|---|---|---|---|---|
| `claude` | Claude Code | `claude -p --output-format stream-json --verbose --include-partial-messages` | `--resume <id>` | `stream-json` w trybie `-p` wymaga `--verbose`. Delty tekstu tylko z `--include-partial-messages` |
| `codex` | Codex CLI | `codex exec --json "<prompt>"` | `codex exec resume <id>` (lub `--last`) | Korzysta z zapisanego logowania (plan ChatGPT). Piaskownica `--sandbox read-only` / `workspace-write`. Konfiguracja MCP do weryfikacji |
| `agy` | Antigravity CLI | `agy -p "<prompt>" --output-format stream-json` | do ustalenia | Od 18.06.2026 zastąpił Gemini CLI dla kont z subskrypcją Google. Zamknięty kod. JSON dostępny od ok. v1.1.8; wcześniejsze wersje gubiły stdout w podprocesie. Wymaga jednorazowego logowania interaktywnego. Obsługa MCP do sprawdzenia |
| `fake` | `python -m konklawe.fake_cli` | odtwarza nagrany strumień | — | Testy bez zużywania limitów. Parsuje wyjście parserem dostawcy wskazanego w `replay_of` |

### 5.3 Wspólny format zdarzeń

Każde zdarzenie ma pola: `seq`, `ts`, `session_id`, `turn_id`, `agent`, `provider`, `type`, `data` i opcjonalnie `raw`. Typy zdarzeń:

| Typ | Znaczenie |
|---|---|
| `status` | Zmiana stanu sesji lub tury (generuje runner/orkiestrator) |
| `session_started` | Dostawca podał ID swojej sesji, model, narzędzia |
| `text_delta` | Kawałek tekstu w trakcie generowania |
| `message` | Pełny blok tekstu (z flagą, czy wcześniej przyszły jego delty) |
| `tool_call` | Wywołanie narzędzia (nazwa, skrót argumentów) |
| `tool_result` | Wynik narzędzia (skrót, czy błąd) |
| `turn_completed` | Koniec tury: wynik, czas, zużycie, szacowany koszt |
| `rate_limited` | Wyczerpany limit subskrypcji |
| `error` | Błąd (krytyczny lub nie) |
| `stderr` | Linia ze stderr procesu |
| `process_exited` | Proces zakończył działanie (kod wyjścia, czas) |
| `raw` | Linia, której parser nie rozpoznał |

Szczegółowy kontrakt pola `data` dla każdego typu jest w `docs/plan/01-core.md`. W etapach 2 i 4 dojdą typy związane z tablicą: `board_message`, `proposal`, `vote`, `decision`, `summary` i `escalation`.

### 5.4 Runner i orkiestrator

- **Runner** wykonuje jedną turę. Uruchamia proces we własnej grupie procesów i równolegle czyta stdout oraz stderr. Pilnuje timeoutu, a przy anulowaniu zabija całe drzewo procesów. Każde zdarzenie emituje przez `emit()` (najpierw dziennik, potem szyna zdarzeń).
- **Orkiestrator** zarządza sesjami. Pilnuje, żeby jedna sesja miała naraz tylko jedną turę, a globalny semafor ogranicza liczbę równoległych tur. Przekazuje ID sesji dostawcy przy wznowieniu i porządnie sprząta przy wyłączeniu, tak żeby nie zostały osierocone procesy.
- **Szyna zdarzeń** (pub/sub w pamięci) rozsyła zdarzenia do odbiorców: renderera terminalowego, później dashboardu i tablicy. Wolny odbiorca nigdy nie blokuje runnera.

### 5.5 Dziennik zdarzeń

SQLite w trybie WAL, w katalogu danych (`.konklawe/konklawe.db`). Tabele `sessions`, `turns` i `events`; numer kolejny zdarzenia to `seq` (autoinkrementacja). Dziennik pełni trzy funkcje:

1. trwałość (sesje przetrwają restart aplikacji),
2. odtwarzanie: dashboard po odświeżeniu najpierw doczytuje historię, potem słucha na żywo,
3. dane testowe: nagrane strumienie zasilają adapter `fake`.

### 5.6 Projekty, worktree i podsumowania (etap 2)

- **Projekt** to zarejestrowane repozytorium docelowe.
- **Worktree na sesję:** `git worktree add <data_dir>/worktrees/<projekt>/<sesja> -b konklawe/<sesja>`. Worktree leży poza repozytorium docelowym, żeby uniknąć zagnieżdżenia.
- **Scalanie nie jest automatyczne.** Decyzję o scaleniu zmian podejmuje kierownik albo użytkownik, a samo scalenie wymaga potwierdzenia użytkownika.
- **Podsumowanie po każdej turze.** Agent kończy turę krótkim, ustrukturyzowanym podsumowaniem, które trafia do wspólnego stanu:

  ```json
  {"done": "...", "changed_files": ["..."], "next": "...", "open_questions": ["..."]}
  ```

  Mechanizm to narzędzie tablicy `submit_summary`. Z podsumowań korzystają kierownik, użytkownik i nowy wykonawca po zmianie dostawcy. Pełnej historii czatu nigdy nie przenosimy między dostawcami. Nośnikiem kontekstu są pliki w worktree, reguły projektu i podsumowania.

### 5.7 Tablica (MCP, etap 2 i 4)

Lokalny serwer MCP (oficjalne SDK dla Pythona) z bazą w tym samym SQLite, uruchamiany w tym samym procesie co orkiestrator. Ponieważ SDK MCP i FastAPI stoją na Starlette, w etapie 3 tablica i dashboard mogą działać w jednej aplikacji.

- **Tożsamość:** orkiestrator generuje każdej sesji osobny plik konfiguracji MCP (dla Claude: `--mcp-config`) z tożsamością agenta i losowym tokenem sesji w nagłówkach połączenia. Agent nigdy sam nie podaje, kim jest.
- **Narzędzia wspólne:** `send_message(to, content)`, `read_inbox()`, `submit_summary(...)`.
- **Narzędzia decyzyjne (etap 4):** `propose(title, body)`, `vote(proposal_id, choice, rationale)`.
- **Budzenie:** gdy przychodzi wiadomość do agenta, który akurat nie ma aktywnej tury, orkiestrator uruchamia mu nową turę z treścią nowych wiadomości w prompcie.
- **Routing:** wszystkie reguły „kto może pisać do kogo” są w jednej funkcji `can_send(from, to)`, sterowanej przez workflow.

### 5.8 Workflowy, topologie i kierownik (etap 4)

```yaml
name: zespol-z-kierownikiem
description: Kierownik deleguje, pracownicy raportują tylko do niego
topology: hub            # hub | mesh
manager: kierownik       # wymagane dla hub
participants: [architekt, backend, recenzent]
decision:
  rule: manager          # manager | majority | unanimous
  max_rounds: 5
on_limit: switch         # wait | switch
```

- **Mesh:** każdy pisze do każdego, a decyzje zapadają według reguły (domyślnie większość).
- **Hub:** pracownicy piszą wyłącznie do kierownika, a kierownik do wszystkich.
- **Narzędzia kierownika:** `assign_task(agent, task)`, `ask(agent, question)`, `broadcast(content)`, `decide(proposal_id, decision, rationale)`, `escalate(reason)`.
- **Narzędzia pracownika:** `report(summary, details_path)`, `ask_manager(question)`.
- **Kierownik nie edytuje plików** (zablokowane Edit, Write, Bash), więc musi delegować.
- **Raporty są krótkie:** kilka zdań plus ścieżka do pliku ze szczegółami, żeby nie zapchać kontekstu kierownika.
- **Limit rund:** po przekroczeniu `max_rounds` sprawa trafia do użytkownika (`escalation`).
- **Różnorodność:** agentów celowo różnicujemy rolą i modelem. Instancje tego samego modelu z tym samym kontekstem mają tendencję do zgadzania się ze sobą.
- **Później:** narzędzie `spawn_agent(name)` z twardym limitem liczby instancji (poza obecnym zakresem).

### 5.9 Reguły projektu (etap 2)

Jedno źródło reguł projektu, z którego aplikacja generuje treść dla natywnych plików każdego CLI: `CLAUDE.md` dla Claude Code, `AGENTS.md` dla Codexa, a dla `agy` do ustalenia. Treść trafia do worktree jako blok między znacznikami `<!-- konklawe:begin -->` i `<!-- konklawe:end -->`, bez nadpisywania treści użytkownika. Szczegóły, w tym to, czy te zmiany są commitowane, ustalamy w etapie 2.

### 5.10 Limity i zmiana wykonawcy (etap 5)

- Zdarzenie `rate_limited` zmienia status sesji na `limited`.
- **Polityka `wait`:** wstrzymanie sesji do czasu odnowienia limitu, jeśli jest znany, albo do decyzji użytkownika.
- **Polityka `switch`:** zmiana wykonawcy na kolejnego z `fallback_providers`. Nowy wykonawca dostaje nową sesję u swojego dostawcy w tym samym worktree, a jego prompt składa się z roli, ostatnich podsumowań i bieżącego zadania. Historii czatu nie przenosimy.

### 5.11 Dashboard (etap 3)

FastAPI z WebSocketem, statyczny frontend HTML/JS bez kroku budowania. Opcjonalnie xterm.js, jeśli zechcemy bardziej „terminalowy” wygląd.

**Zakładki:**

- **Agenci:** lista agentów i sesji z ich statusem.
- **Widok terminali:** panele na żywo. Wyrenderowane zdarzenia, nie surowy JSON: narzędzia przygaszone i zwijane, wiadomości wyróżnione kolorem, decyzje wyróżnione.
- **Tablica:** chronologia wiadomości, propozycji i głosów, czytana jak czat.

**Protokół WebSocket:**

- klient → `{"type": "subscribe", "after_seq": N}`; serwer wysyła zaległe zdarzenia z dziennika, potem zdarzenia na żywo
- serwer → zdarzenia w paczkach co ok. 50 ms: `{"type": "events", "events": [...]}`
- klient → `{"type": "send", "session_id": "...", "text": "..."}`, `{"type": "cancel", "session_id": "..."}`

**Przejmij sesję (etap 5):** dashboard pokazuje komendę do wznowienia sesji w prawdziwym terminalu (dla Claude: `claude -r <id>` w katalogu sesji) i oznacza sesję jako przejętą, żeby orkiestrator jej w tym czasie nie używał.

## 6. Docelowa struktura repozytorium

```
konklawe/
  CLAUDE.md
  README.md
  konklawe.toml              # opcjonalna konfiguracja
  agents/                    # definicje agentów (*.md)
  workflows/                 # definicje workflowów (*.yaml, etap 4)
  docs/
    ARCHITECTURE.md
    DECISIONS.md
    plan/                    # plany etapów
    notes/                   # notatki z rozpoznania CLI
  src/konklawe/
    cli.py  config.py  agents.py  events.py  store.py  bus.py
    runner.py  orchestrator.py  render.py  fake_cli.py
    adapters/                # base, claude, fake; później codex, agy
    board/                   # tablica MCP (etap 2)
    web/                     # dashboard (etap 3)
  tests/
    fixtures/                # nagrane strumienie per dostawca
```

## 7. Czego świadomie nie robimy

- Nie forkujemy Xirpa (zamknięty kod) ani innych narzędzi. Podglądamy tylko pomysły (np. Claude Squad, Vibe Kanban), bez kopiowania kodu.
- Nie używamy LangGrapha. Możemy wrócić do niego później jako warstwy workflowów.
- Nie budujemy na eksperymentalnych Agent Teams z Claude Code.
- Nie zeskrobujemy interaktywnych terminali.
- Nie mamy kontekstu organizacji w stylu Portalu (ewentualnie później jako kolejny serwer MCP).
- Nie scalamy zmian automatycznie.

## 8. Ryzyka

| Ryzyko | Jak ograniczamy |
|---|---|
| Zmiany formatów CLI i regulaminów dostawców | Izolacja w adapterach, testy na nagranych strumieniach, parser tolerancyjny na nieznane zdarzenia |
| Wyczerpywanie limitów | Adapter `fake` w testach, tani model do nagrań, polityki `wait` i `switch` |
| Agenci w pętli dyskusji | `max_rounds` i eskalacja do użytkownika |
| Konflikty przy scalaniu worktree | Brak automatycznego scalania, decyzja kierownika lub użytkownika |
| Agenci wykonują komendy w systemie | Minimalne uprawnienia z plików agentów, nigdy tryb bypass |
| Osierocone procesy | Grupy procesów, zabijanie całego drzewa, sprzątanie przy wyłączeniu |

## 9. Etapy

1. **Core** (plan: `docs/plan/01-core.md`): agenci z plików, adaptery `claude` i `fake`, runner, orkiestrator, dziennik, szyna zdarzeń, CLI z renderowaniem na żywo i odtwarzaniem.
2. **Rozmowa:** projekty i worktree, tablica MCP z wiadomościami, budzenie agentów, mesh z 2–3 agentami Claude, podsumowania tur, jedno źródło reguł.
3. **Podgląd:** FastAPI i WebSocket, dashboard z zakładkami Agenci, Widok terminali i Tablica, odtwarzanie, wysyłanie wiadomości i anulowanie z UI.
4. **Decyzje:** propozycje i głosy, pliki workflowów, topologia hub i kierownik, limity rund, eskalacja.
5. **Wielu dostawców:** adaptery `codex` i `agy`, obsługa limitów, zmiana wykonawcy, przejmowanie sesji.

## 10. Otwarte kwestie

- Projekt testowy, na którym agenci będą pracować od etapu 2.
- Domyślna reguła decyzji: większość czy kierownik po wysłuchaniu reszty.
- System docelowy: Linux, macOS czy Windows (natywnie albo przez WSL).
- Ostateczna nazwa aplikacji.
