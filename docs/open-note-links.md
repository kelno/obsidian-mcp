# Open-note links: the page behind `OPEN_NOTE_URL`

With `OPEN_NOTE_URL` set, the tools give agents an http(s) link to each note
they name, because many places make only http(s) links clickable, not
`obsidian://` ones. The server only builds the link; you serve the page behind
it. That page answers `/open?file=<path>` by opening `obsidian://open` for that
note. How the tools fill in the link: [vault tools](architecture/vault-tools.md),
"Open-note links".

## What the page must do

- Redirect to `obsidian://open?vault=<your vault's name>&file=<the file
  argument as given>`. Obsidian finds the vault by its name in the app, so
  every device that opens these links needs the vault under that name.
- Accept the path both percent-encoded and partly raw. The server always
  percent-encodes it (`/` kept), but some clients decode part of a link before
  opening it (the Claude desktop app seems to turn `%28`/`%29` back into `(`
  and `)`), so a note under a `(private)` folder, or a heading with
  parentheses, arrives raw.
- Never let `"`, `<`, `>` or `&` through: the page writes the path into its
  HTML. An allowlist of characters is the simple way.

The page reads nothing from the vault or the server, so it needs no login.

## Example: nginx

In the `server` block that serves this app (HTTPS), plus a `map` at the `http`
level. Replace `MyVault` with the vault's name, percent-encoded.

```nginx
# The note path: a percent-encoded path, plus punctuation a client may leave
# raw, such as the ( ) of a "(private)" folder; never " < > or &, which would
# let it inject markup into the page
map $arg_file $open_note_file {
    "~^[A-Za-z0-9%._~/!'()*+,:;=@-]+$" $arg_file;
    default "";
}

server {
    # ... this app's server block ...

    location = /open {
        if ($open_note_file = "") {
            return 400 "Usage: /open?file=<percent-encoded vault path, without .md>\n";
        }
        default_type text/html;
        add_header Content-Security-Policy "default-src 'none'" always;
        add_header Cache-Control "no-store" always;
        add_header Referrer-Policy "no-referrer" always;
        return 200 '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width"><meta http-equiv="refresh" content="0;url=obsidian://open?vault=MyVault&amp;file=$open_note_file"><title>Open in Obsidian</title><p><a href="obsidian://open?vault=MyVault&amp;file=$open_note_file">Open in Obsidian</a></p>\n';
    }
}
```

Then set `OPEN_NOTE_URL=https://notes.example.org/open?file={path}`.
