# Format strumienia `stream-json` Claude Code

Opis na podstawie nagrań z `tests/fixtures/claude/` (Claude Code 2.1.289, model `claude-haiku-4-5-20251001`, 2026-10-05) i schematów z dystrybucji CLI. Komenda bazowa:

```
claude -p --output-format stream-json --verbose --include-partial-messages \
       --model haiku --append-system-prompt "..."      # prompt przez stdin
```

## Nagrania

| Plik | Co zawiera | Kod wyjścia |
|---|---|---|
| `simple_text.jsonl` | Odpowiedź tekstowa. Model sam wywołał `Write` do auto-pamięci (zob. „Pułapki”), więc są tu też `tool_use` i `tool_result` | 0 |
| `resume_turn2.jsonl` | Druga tura sesji z `simple_text` (`--resume`). Odpowiedź: `42`. ID sesji bez zmian | 0 |
| `tool_use.jsonl` | `Read` na `README.md` przy `--allowedTools Read Grep Glob`, potem streszczenie | 0 |
| `tool_denied.jsonl` | Próba `Write` przy `--allowedTools Read,Grep,Glob --disallowedTools Edit,Bash` → odmowa | 0 |
| `error.jsonl` | Nieistniejący model: syntetyczna wiadomość z błędem, `result` z `is_error: true` | **1** |

Każde nagranie ma obok plik `<nazwa>.meta.json` z komendą, stdin, katalogiem roboczym, kodem wyjścia, stderr i listą usuniętych zmiennych środowiska. Wartości pól `signature` w blokach `thinking` są zamienione na `REDACTED`, bo zawierają zakodowane ID organizacji (decyzja w `DECISIONS.md`). Reszta to bajt w bajt wyjście CLI.

## Ogólne zasady

- Jedna linia to jeden obiekt JSON w zapisie zwartym, z polem `type`. Prawie każdy obiekt ma `session_id` i `uuid`.
- Pierwsza linia to zawsze `system/init` (także przy błędzie modelu). Ostatnia to zawsze `result`.
- `session_id` jest ten sam we wszystkich liniach tury, a po `--resume` zostaje taki sam jak w poprzedniej turze.
- `parent_tool_use_id` jest różne od `null` tylko dla wiadomości z wnętrza subagenta (`Task`). W nagraniach zawsze `null`.
- Kolejność w jednym wywołaniu API: `stream_event/message_start` → dla każdego bloku: `content_block_start`, delty, **pełna wiadomość `assistant` z tym jednym blokiem**, `content_block_stop` → `message_delta` → `message_stop`. Wiadomość `assistant` przychodzi **przed** `content_block_stop` swojego bloku.
- Jedna tura może zawierać kilka wywołań API (np. `tool_use` → `tool_result` → odpowiedź). `result.num_turns` to liczba tych wywołań.

## Typy zdarzeń

### `system` / `init`

Pierwsza linia tury. Istotne pola:

| Pole | Przykład | Uwagi |
|---|---|---|
| `session_id` | `8cf3e9e9-…` | ID sesji dostawcy do `--resume` |
| `model` | `claude-haiku-4-5-20251001` | Pełna nazwa (alias rozwinięty). Przy błędnym modelu: podana nazwa bez zmian |
| `tools` | `["Task","Bash",…,"Read",…]` | Lista narzędzi. **Niedeterministyczna:** `Glob`/`Grep` pojawiają się tylko, gdy są jawnie dozwolone; narzędzia MCP tylko, gdy serwer zdążył się połączyć; `--disallowedTools` usuwa narzędzia z listy |
| `permissionMode` | `default` | |
| `apiKeySource` | `none` | `none` = brak klucza API, czyli rozliczenie subskrypcją. Przydatne do weryfikacji zasady 1 |
| `cwd` | ścieżka | Katalog roboczy |
| `claude_code_version` | `2.1.289` | |
| `mcp_servers` | `[{"name","status","source"}]` | `status` np. `pending` |
| inne | `agents`, `skills`, `slash_commands`, `plugins`, `memory_paths`, `messaging_socket_path`, `capabilities`, `output_style`, … | Ignorujemy |

→ `session_started {provider_session_id, model, tools}`.

### `system` / `status`

`{"type":"system","subtype":"status","status":"requesting",…}`. Pojawia się przed każdym wywołaniem API. → pomijamy.

### `system` / `thinking_tokens`

`{"subtype":"thinking_tokens","estimated_tokens":50,"estimated_tokens_delta":50}`. Szacunek liczby tokenów myślenia. → pomijamy.

### `system` / `permission_denied`

```json
{"type":"system","subtype":"permission_denied","tool_name":"Write","tool_use_id":"toolu_…",
 "message":"Claude requested permissions to write to …/notatka.txt, but you haven't granted it yet."}
```

Pojawia się tuż przed `tool_result` z błędem dla tego samego `tool_use_id`. Informacja jest zdublowana w `tool_result` i w `result.permission_denials`. → pomijamy (wystarczy `tool_result` z `is_error`).

### `stream_event`

Opakowanie surowych zdarzeń API Messages: `{"type":"stream_event","event":{…},"session_id",…}`.

| `event.type` | Treść | Mapowanie |
|---|---|---|
| `message_start` | `event.message.id` (np. `msg_011C…`), `model`, `usage`; dodatkowo `ttft_ms` na zewnątrz | Zapamiętaj bieżące `message_id`, nic nie emituj |
| `content_block_start` | `index`, `content_block`: `{"type":"text","text":""}`, `{"type":"thinking",…}`, `{"type":"tool_use","id","name","input":{}}` | Zapamiętaj typ bloku pod `index`, nic nie emituj |
| `content_block_delta` + `text_delta` | `index`, `delta.text` | → `text_delta {text, message_id}` |
| `content_block_delta` + `thinking_delta` | `delta.thinking` (w nagraniach **pusty**: `thinking_display: "updates"`), `delta.estimated_tokens` | Pomijamy |
| `content_block_delta` + `signature_delta` | `delta.signature` | Pomijamy |
| `content_block_delta` + `input_json_delta` | `delta.partial_json` (dziesiątki małych kawałków) | Pomijamy (pełne `input` przychodzi w `assistant`) |
| `content_block_stop` | `index` | Pomijamy |
| `message_delta` | `delta.stop_reason` (`tool_use`, `end_turn`), `usage` | Pomijamy |
| `message_stop` | — | Pomijamy |

Nieznany `event.type` lub nieznany typ delty → `raw`.

### `assistant`

Jedna wiadomość na **jeden blok** (nie na całą odpowiedź). `message.content` zawiera dokładnie jeden element, a `message.id` jest równe `id` z ostatniego `message_start`.

- blok `text` → `message {text, message_id, from_deltas}`,
- blok `tool_use` `{"id","name","input"}` → `tool_call {tool_id, name, input_summary}`; obok jest jeszcze `wire_tool_inputs` (to samo `input`),
- blok `thinking` (pusty tekst, `thinking_duration_ms`) → pomijamy.

**Deduplikacja (sprawdzona na wszystkich nagraniach):** sklejone `text_delta` dla pary (`message_id`, `index`) są identyczne z tekstem bloku w `assistant`. Wystarczy, że parser pamięta, czy dla bieżącego `message_id` przyszły delty tekstu do aktualnie otwartego bloku tekstowego. Jeśli tak, to `from_deltas=true`.

**Wiadomość z błędem API** (nagranie `error`): brak `stream_event`, a `assistant` ma dodatkowe pola na najwyższym poziomie:

```json
{"type":"assistant","message":{"model":"<synthetic>","content":[{"type":"text","text":"There's an issue with the selected model (…)…"}],…},
 "error":"model_not_found","is_api_error_message":true}
```

Wartości `error` według schematu CLI: `authentication_failed`, `oauth_org_not_allowed`, `account_on_hold`, `verification_required`, `billing_error`, **`rate_limit`**, `overloaded`, `invalid_request`, `model_not_found`, `server_error`, `unknown`, `max_output_tokens`, `cloud_credential_error`.

→ `error {message: text, fatal: false, code: error}` (o krytyczności tury rozstrzyga `result`). Gdy `error == "rate_limit"` → `rate_limited`.

### `user`

Wyniki narzędzi: `message.content` to lista bloków `tool_result`:

```json
{"type":"tool_result","tool_use_id":"toolu_…","content":"…","is_error":true}
```

- `content` bywa napisem albo listą bloków (`[{"type":"text","text":…}]`), więc parser obsługuje oba przypadki.
- `is_error` występuje tylko przy błędzie, a jego brak oznacza `false`.
- Obok jest `tool_use_result` (pełny, natywny wynik narzędzia, np. cała treść pliku dla `Read` albo napis `"Error: …"`) i przy odmowie `tool_result_meta: [{"id","non_execution_kind":"user-rejected"}]`. → nie używamy, tylko `message.content`.

→ `tool_result {tool_id, is_error, output_summary}`.

### `rate_limit_event`

Emitowane, gdy zmienia się informacja o limicie (w nagraniach raz na turę, po pierwszym wywołaniu API):

```json
{"type":"rate_limit_event","rate_limit_info":{"status":"allowed","resetsAt":1791241200,
 "rateLimitType":"five_hour","overageStatus":"rejected","overageDisabledReason":"org_level_disabled",
 "isUsingOverage":false,"unifiedWindows":{"five_hour":{"utilization":0.05,"resetsAt":1791241200},
 "seven_day":{"utilization":0.07,"resetsAt":1791399600}}}}
```

Schemat z dystrybucji: `status` ∈ `allowed | allowed_warning | rejected`; `resetsAt` w sekundach epoki; `rateLimitType` ∈ `five_hour | seven_day | seven_day_opus | seven_day_sonnet | seven_day_overage_included | overage`; `utilization` to ułamek okna.

- `status == "rejected"` → `rate_limited {message, resets_at}` (z `resetsAt` jako datetime UTC).
- `allowed` / `allowed_warning` → pomijamy.
- **Pułapka:** `overageStatus: "rejected"` pojawia się także przy zwykłym `allowed` (brak dopłat na koncie) i **nie oznacza** wyczerpania limitu. Patrzymy wyłącznie na `rate_limit_info.status`.

### `result`

Ostatnia linia tury.

| Pole | Przykład | Uwagi |
|---|---|---|
| `subtype` | `success` | **Także przy błędzie modelu** jest `success`. O błędzie mówi `is_error` |
| `is_error` | `false` / `true` | |
| `result` | tekst | Ostatnia odpowiedź (przy błędzie treść komunikatu) |
| `session_id` | | → `provider_session_id` |
| `num_turns` | `2` | Liczba wywołań API w turze |
| `duration_ms`, `duration_api_ms`, `ttft_ms` | | |
| `total_cost_usd` | `0.0233651` | → `cost_estimate_usd` (szacunek API, nie opłata) |
| `usage` | `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, … | → `usage` |
| `modelUsage` | per model | Ignorujemy |
| `permission_denials` | `[{"tool_name","tool_use_id","tool_input"}]` | |
| `terminal_reason` | `completed`, `api_error` | |
| `stop_reason` | `end_turn`, `stop_sequence` | |
| `api_error_status` | `null`, `404` | Kod HTTP błędu API |

→ `turn_completed {result_text, is_error, duration_ms, num_turns, usage, cost_estimate_usd, provider_session_id}`.

## Kod wyjścia i stderr

- Sukces: kod 0, stderr pusty.
- Odmowa uprawnień: kod 0 (tura się udała, tylko narzędzie zostało odrzucone).
- Błąd modelu: **kod 1**, mimo że strumień kończy się poprawnym `result`. stderr zawiera jedną linię: `[claude-code:unrecognized_model] {"model":"nie-ma-takiego-modelu","query_source":"sdk"}`.

Wniosek dla `finish()`: błąd krytyczny z ogonem stderr emitujemy tylko wtedy, gdy nie było `result`. Gdy `result` był, kod ≠ 0 nie dodaje drugiego błędu.

## Wyczerpany limit (nie nagrany)

Wyczerpania limitu nie da się wywołać na żądanie, więc opis pochodzi ze schematów CLI. Spodziewamy się dwóch sygnałów: `rate_limit_event` ze `status: "rejected"` i/lub syntetycznej wiadomości `assistant` z `error: "rate_limit"`. Najpewniej będzie im towarzyszyć `result` z `is_error: true` i `api_error_status: 429`. `detect_rate_limit` sprawdza oba sygnały strukturalne, a dopasowanie tekstu (np. „usage limit”, „limit reached”) zostaje jako zapas. Parser emituje `rate_limited` najwyżej raz na turę. **TODO:** potwierdzić na prawdziwym wyjściu, gdy limit się wyczerpie.

## Pułapki zaobserwowane w nagraniach

1. **Auto-pamięć.** Na prośbę „Zapamiętaj liczbę 42” model (bez zgody na `Write`) zapisał plik do `~/.claude/projects/<katalog>/memory/`. Zapisy do katalogu pamięci są dozwolone bez pytania. Konsekwencje:
   - agenci mogą zostawiać pliki w katalogu domowym użytkownika i między sesjami „pamiętać” rzeczy spoza historii rozmowy,
   - test wznowienia nie powinien prosić o „zapamiętanie”, bo poprawna odpowiedź może pochodzić z pamięci, a nie z `--resume`.

   Wyłączenie auto-pamięci dla agentów rozważamy w etapie 2 razem z izolacją ustawień.
2. **Agent nie wie, że działa bez człowieka.** Po odmowie model odpisał „Kliknij Allow…”. Rola agenta (albo prompt systemowy konklawe) powinna mówić, że pracuje w trybie headless.
3. **Lista narzędzi w `init` jest zmienna** (zob. wyżej). Nie porównujemy jej w testach dosłownie.
4. **Bardzo dużo drobnych zdarzeń.** `simple_text` ma 138 linii, z czego 63 to `input_json_delta`. Pomijanie technicznych `stream_event` jest konieczne, żeby nie zaśmiecać dziennika (zob. `DECISIONS.md`).
