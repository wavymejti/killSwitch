---
name: echo
description: Agent testowy – odtwarza nagrany strumień bez zużywania limitów
provider: fake
provider_options:
  fixture: tests/fixtures/claude/simple_text.jsonl
  fixture_resume: tests/fixtures/claude/resume_turn2.jsonl
  replay_of: claude
  delay_ms: 30
---
Agent testowy. Rola nie jest używana – adapter fake odtwarza nagranie.
