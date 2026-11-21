# Open-note links: the `/open` page

With `OPEN_NOTE_URL` set, the tools give agents an http(s) link to each note
they name, because many places make only http(s) links clickable, not
`obsidian://` ones. With `OPEN_NOTE_VAULT` set too, this server serves the
page those links lead to. How the tools fill in the link:
[vault tools](architecture/vault-tools.md), "Open-note links".

## Setting it up

```
OPEN_NOTE_VAULT=MyVault
OPEN_NOTE_URL=https://notes.example.org/open?file={path}
```

`OPEN_NOTE_VAULT` is the vault's name in the Obsidian app: Obsidian finds the
vault by name, so every device that opens these links needs the vault under
that name. `OPEN_NOTE_URL` points at this server's `/open`. If your reverse
proxy forwards only some paths to the server, forward `/open` too.

## What the page does

- `/open?file=<path>` redirects to `obsidian://open?vault=<name>&file=<path>`,
  with an "Open in Obsidian" link as a fallback, then closes its own tab after
  3 seconds, so link tabs don't pile up. Firefox allows that for a tab opened
  from a link clicked in another app; a browser that refuses leaves the tab
  open.
- It takes the path as received, percent-encoded or partly raw. The server
  always percent-encodes it (`/` kept), but some clients decode part of a link
  before opening it (the Claude desktop app seems to turn `%28`/`%29` back
  into `(` and `)`), so a note under a `(private)` folder, or a heading with
  parentheses, arrives raw.
- Anything outside an allowlist of characters (`"`, `<`, `>`, `&`, a space,
  …) gets a usage line instead: the page writes the path into its HTML.
- It reads nothing from the vault or the database and needs no login, like
  the ledger entry page; its only script runs under a per-response nonce.
- Unset `OPEN_NOTE_VAULT`: `/open` answers 404, and the page behind
  `OPEN_NOTE_URL` is yours to serve.
