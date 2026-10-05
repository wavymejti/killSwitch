# Claude Code CLI – rozpoznanie

Stan na 2026-10-05, macOS (Darwin 25.6), `claude --version` → **2.1.289 (Claude Code)**.

Źródła: `claude --help`, próby parsowania argumentów (`claude <flaga> --version`, które nie wywołują API), napisy z dystrybucji CLI oraz nagrania z `tests/fixtures/claude/` (opis formatu: `claude-stream-format.md`).

## Uwierzytelnienie

`claude auth status` wypisuje JSON, m.in. `"authMethod": "claude.ai"`, `"apiProvider": "firstParty"` i `"subscriptionType": "pro"`. W przyszłości to dobry sposób na sprawdzenie (np. w `konklawe doctor`), że CLI rozlicza się subskrypcją, bez dotykania plików z tokenami.

## Flagi istotne dla projektu

| Flaga | Opis | Uwagi dla adaptera |
|---|---|---|
| `-p`, `--print` | Tryb nieinteraktywny: odpowiedź i wyjście | W tym trybie pomijane jest okno zaufania katalogu, a błędne pliki ustawień są po cichu ignorowane |
| `--output-format <f>` | `text` (domyślnie), `json`, `stream-json` | Tylko z `-p` |
| `--verbose` | Nadpisuje ustawienie verbose | **Wymagane** przy `-p` + `stream-json`. Bez niej CLI kończy się błędem `When using --print, --output-format=stream-json requires --verbose` (napis z dystrybucji) |
| `--include-partial-messages` | Strumieniuje kawałki wiadomości (`stream_event`) | Tylko z `-p` i `stream-json` |
| `--input-format <f>` | `text` (domyślnie) albo `stream-json` | Nie używamy (model tur: jeden proces na turę) |
| `-r`, `--resume [id]` | Wznowienie rozmowy po ID sesji | Bez ID otwiera interaktywny wybór, więc zawsze podajemy ID. Sesja zostaje pod tym samym ID (nowe ID tylko z `--fork-session`) |
| `--model <m>` | Alias (`fable`, `opus`, `sonnet`, `haiku`) lub pełna nazwa | Przekazujemy bez zmian |
| `--allowedTools`, `--allowed-tools <tools...>` | Narzędzia dozwolone bez pytania | Lista „rozdzielona przecinkami lub spacjami”, opcja wieloargumentowa (zob. niżej) |
| `--disallowedTools`, `--disallowed-tools <tools...>` | Narzędzia zabronione | Jak wyżej |
| `--permission-mode <mode>` | Tryb uprawnień | Wartości z pomocy: `acceptEdits`, `auto`, `bypassPermissions`, `manual`, `dontAsk`, `plan`. **`default` też jest akceptowane** (ukryty alias, sprawdzone próbą), `bogus` jest odrzucane |
| `--permission-prompts <target>` | Kto odpowiada na pytania o zgodę w `-p`: `host` (domyślnie; host SDK albo `--permission-prompt-tool`) lub `none` (wszystko, co wymagałoby pytania, jest odrzucane) | Nie potrzebujemy: przy domyślnym `host` bez hosta SDK narzędzie spoza dozwolonych jest **od razu odrzucane** (nagranie `tool_denied`, bez zawieszenia) |
| `--append-system-prompt <text>` | Dopisuje tekst do domyślnego promptu systemowego | Tu trafia `role_prompt` |
| `--append-system-prompt-file <path>` | Wariant z plikiem | **Ukryty w `--help`**, ale istnieje (wymieniony w opisie `--bare` i na liście flag w dystrybucji). Furtka na limit długości linii poleceń w Windows |
| `--system-prompt <text>`, `--system-prompt-file <path>` | Zastępuje cały prompt systemowy | Nie używamy |
| `--system-prompt-snapshot on\|off` | Domyślnie `on`: prompt systemowy (z `--append-system-prompt`) jest zapisywany przy pierwszym żądaniu i używany bez zmian przy każdym wznowieniu, nawet gdy kolejne uruchomienie poda inny tekst | Zmiana roli agenta nie zadziała w już istniejącej sesji |
| `--mcp-config <configs...>` | Serwery MCP z plików JSON lub napisów JSON (rozdzielonych spacjami) | Etap 2 (tablica). Opcja wieloargumentowa |
| `--strict-mcp-config` | Tylko serwery z `--mcp-config` | Etap 2: odcina agentów od serwerów MCP użytkownika |

## Format list narzędzi

`--allowedTools` i `--disallowedTools` są opcjami wieloargumentowymi (`<tools...>`) i dodatkowo dzielą każdy argument po przecinkach lub spacjach. Działają oba zapisy (potwierdzone nagraniami `tool_use` i `tool_denied`):

```
--allowedTools Read Grep Glob
--allowedTools Read,Grep,Glob
```

**Pułapka:** opcja wieloargumentowa pochłania kolejne argumenty pozycyjne, dopóki nie trafi na następną flagę. Prompt podany jako argument zaraz po `--allowedTools Read` zostałby potraktowany jak nazwa narzędzia. Dlatego prompt podajemy przez stdin (zob. niżej), a gdyby kiedyś musiał być argumentem, trzeba go poprzedzić `--`.

Adapter przekazuje każde narzędzie jako osobny argument (`--allowedTools Read Grep Glob`). Tak przejdą też nazwy ze spacjami w nawiasach, np. `Bash(git log:*)`.

## Prompt przez stdin

**Potwierdzone:** wszystkie nagrania podają prompt przez stdin (`echo "…" | claude -p …`) i działają. Runner zawsze podaje prompt przez stdin, a argumentu pozycyjnego nie używa. Gdy brak promptu, CLI kończy się błędem `Input must be provided either through stdin or as a prompt argument when using --print`.

## Inne flagi warte uwagi (na później)

- `--tools <tools...>`: ogranicza zestaw dostępnych narzędzi wbudowanych (silniejsze niż `--allowedTools`, które tylko zwalnia z pytania o zgodę). Można przekazać przez `provider_options.extra_args`.
- `--setting-sources user,project,local`: które pliki ustawień wczytać. Bez tego agent dziedziczy ustawienia użytkownika (hooki, wtyczki, MCP, pamięć). Do rozważenia w etapie 2.
- `--session-id <uuid>`: z góry nadane ID sesji dostawcy.
- `--fork-session`: wznowienie pod nowym ID.
- `--no-session-persistence`: sesja nie jest zapisywana i nie da się jej wznowić. Nie używamy.
- `--bare`: tryb minimalny, ale uwierzytelnia **wyłącznie** przez `ANTHROPIC_API_KEY` lub `apiKeyHelper`. Wyklucza subskrypcję, więc nie używamy.
- `--fallback-model <models>`: zapasowy model przy przeciążeniu.
- `--effort <level>`: `low`, `medium`, `high`, `xhigh`, `max`.
- `--include-hook-events`, `--prompt-suggestions`: dodatkowe typy zdarzeń w strumieniu. Nie włączamy.
- `--max-budget-usd`: limit kosztu w `-p` (dotyczy API).

## Flagi zakazane

- `--dangerously-skip-permissions` oraz `--permission-mode bypassPermissions` (zasada 8).
- `--allow-dangerously-skip-permissions`: udostępnia bypass jako opcję. Też ją odrzucamy w walidacji `extra_args`.

## Środowisko

- `ANTHROPIC_API_KEY` i `ANTHROPIC_AUTH_TOKEN` nie są ustawione w środowisku użytkownika (sprawdzone 2026-10-05).
- Proces uruchomiony z wnętrza sesji Claude Code dziedziczy jej zmienne: `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`, `CLAUDE_CODE_SESSION_ID`, `CLAUDE_CODE_CHILD_SESSION`, `CLAUDE_CODE_MESSAGING_SOCKET`, `CLAUDE_CODE_MESSAGING_TOKEN`, `CLAUDE_CODE_SESSION_ATTENDED`, `CLAUDE_CODE_EXECPATH`, `CLAUDE_PID`, `CLAUDE_EFFORT`, `AI_AGENT`. Przy nagraniach je usunęliśmy, żeby dziecko nie uznało się za podsesję. Runner też je usuwa (domyślne `strip_env`, decyzja w `DECISIONS.md`).
- `system/init` w strumieniu zawiera `apiKeySource`. Wartość `none` potwierdza, że tura nie używa klucza API.
- Sesje dostawcy są przechowywane per katalog roboczy (`~/.claude/projects/<katalog>`), więc wznowienie uruchamiamy w tym samym `workdir` co pierwszą turę. Sesja konklawe i tak ma stały `workdir`.

## Sygnał wyczerpanego limitu

Opis w `claude-stream-format.md`, sekcja „rate_limit_event”.

## Uprawnienia w praktyce (nagrania)

- `--allowedTools` zwalnia z pytania o zgodę. Narzędzie, które jest dostępne, ale niedozwolone, model **widzi i próbuje wywołać**, a CLI je odrzuca (`system/permission_denied`, `tool_result` z `is_error`, `result.permission_denials`). Kod wyjścia to wtedy 0.
- `--disallowedTools` usuwa narzędzie z listy w `system/init`, więc model w ogóle go nie widzi.
- `Glob` i `Grep` są w tej wersji domyślnie ukryte i pojawiają się w `tools` dopiero po jawnym dopuszczeniu.
- **Auto-pamięć omija uprawnienia:** zapis do `~/.claude/projects/<katalog>/memory/` przeszedł bez `Write` na liście dozwolonych. Szczegóły w `claude-stream-format.md`, sekcja „Pułapki”.
