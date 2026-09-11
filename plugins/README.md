# CraterView for Claude Code

Two plugins, for two ways of using [CraterView](https://craterview.ai) from an assistant.
This repository is a Claude Code marketplace as well as a client, so both install from it:

```
/plugin marketplace add craterviewai/craterview-python
/plugin install craterview@craterviewai          # the hosted tools
/plugin install craterview-api@craterviewai      # the API, from code
```

(`craterviewai/craterview-ts` carries the same marketplace; add whichever you already have.)

## `craterview` — the hosted tools

Installing it connects CraterView's MCP server, so the assistant can enhance, enlarge and
restore photographs in the conversation. The first use opens an authorization page that asks
for a code from your CraterView dashboard (**Connect an assistant** → **Get a code**). No
API key is ever on screen, and rotating your key from the dashboard disconnects the assistant.

The skill, `/craterview:enhance-image`, says which tool to reach for, which model suits a
soft scan and which a torn print, and what to do with a result whose links expire.

## `craterview-api` — the API, from code

No hosted tools. The skill, `/craterview-api:enhance-image`, is for an agent writing or
running code against the API: how to get a key and where to keep it, the Python and
TypeScript clients, the upload → submit → collect flow, retries that do not double-bill,
webhooks, and cost. `scripts/enhance.py` beside it runs one image from the command line.

## Elsewhere

Each `skills/*/SKILL.md` folder is a plain skill in the open format and works anywhere that
reads one: copy it into `~/.claude/skills/` for a personal install without the plugin, or zip
it and upload it as a custom skill to Claude.

The skills are generated into this repository with each client release, alongside the
client. Open an issue here for anything they get wrong.
