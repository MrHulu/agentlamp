# Collector Adapters

Provider adapters convert local provider signals into sanitized normalized events.

MVP order:

1. `manual`
2. `codex`
3. `claude`

Do not upload raw hook payloads, transcript paths, current working directories, prompts, file contents, shell commands, or provider credentials.

