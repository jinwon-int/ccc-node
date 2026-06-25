# Security Policy

## Supported scope

This repository is being prepared for possible public source visibility. Public
visibility, release publication, package publication, deployment, provider or
Telegram sends, database mutation, credential movement, and history rewrite are
separate approval-gated actions.

## Reporting a vulnerability

Do not open a public issue with secrets, tokens, private URLs, personal data, or
exploit details. Open a minimal maintainer-contact issue that says you need a
private security route, or contact the repository owner through an already
established private channel. Share sensitive details only after a private route
is confirmed.

## Secret handling

- Do not commit real API keys, bot tokens, cookies, sessions, private keys,
  production logs, private host paths, or raw runtime data.
- Example files must use placeholders only.
- Runtime credentials belong in local environment variables or operator-owned
  secret stores outside this repository.
