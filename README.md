# chatgpt-history

Download your ChatGPT conversation history as JSON.

ChatGPT has no public API for conversation history, so this tool uses the same
private backend API as the chatgpt.com web app, authenticated with your browser
session cookie.

> **Caveats**
> - The backend API is undocumented and may change or break at any time.
> - Automated access to it is not officially supported by OpenAI; use it for
>   your own data only, at a reasonable pace.
> - The official alternative is *Settings → Data controls → Export data* in
>   ChatGPT, which emails you a zip containing `conversations.json`.

## Setup

Requires Python 3.10+.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.json config.json
chmod 600 config.json
```

[`curl_cffi`](https://github.com/lexiforest/curl_cffi) impersonates a real
browser's TLS fingerprint, which is needed to get past Cloudflare.

### Authentication

1. Log in to <https://chatgpt.com> in your browser.
2. Open DevTools (F12) → *Application* → *Cookies* → `https://chatgpt.com`.
3. Copy the value of `__Secure-next-auth.session-token` into `session_token`
   in `config.json`.

If the session token is split into `.0` / `.1` chunks, or if authentication
fails, copy instead the whole `Cookie` request header of any chatgpt.com
request (DevTools → *Network*) into `cookie`.

The session cookie is exchanged for a short-lived access token at each run,
and lasts about 3 months. **It grants full access to your account: never share
or commit `config.json`** (it is git-ignored).

| Key             | Description                                                        |
|-----------------|--------------------------------------------------------------------|
| `session_token` | Value of the `__Secure-next-auth.session-token` cookie             |
| `cookie`        | Full `Cookie` header (takes precedence over `session_token`)       |
| `access_token`  | Bearer token used directly, if no cookie is given (expires fast)   |
| `account_id`    | Workspace id, for Team accounts (`chatgpt-account-id` header)      |
| `user_agent`    | Override the impersonated browser's User-Agent                     |
| `delay`         | Seconds to wait between requests (default `1.0`)                   |

## Usage

```sh
.venv/bin/python chatgpt_export.py check                  # verify authentication
.venv/bin/python chatgpt_export.py list                   # list conversations (id, date, title)
.venv/bin/python chatgpt_export.py get <conversation-id>  # print one conversation as JSON

.venv/bin/python chatgpt_export.py export                 # download everything
.venv/bin/python chatgpt_export.py export -n 10           # the 10 most recently updated
.venv/bin/python chatgpt_export.py export -t rabbit       # title contains "rabbit" (or exact id)
.venv/bin/python chatgpt_export.py export -s "some text"  # title or messages contain "some text"
```

`export` options:

| Option                  | Description                                                           |
|-------------------------|-----------------------------------------------------------------------|
| `-n N`, `--last N`      | The N most recently updated conversations (`0`, the default, = all)   |
| `-t STR`, `--title STR` | Conversations whose title contains STR (case-insensitive), or an id   |
| `-s STR`, `--contains STR` | Conversations whose title or messages contain STR (case-insensitive) |
| `--full`                | Re-download conversations even if unchanged                           |
| `--archived`            | Also include archived conversations (full export only)                |
| `--single-file`         | Also write all exported conversations to `conversations.json`         |
| `-o DIR`, `--output DIR`| Output directory (default `export/`)                                  |

`-n`, `-t` and `-s` are mutually exclusive.

Exports are incremental: a conversation whose update time has not changed
since the last run is not downloaded again.

`-t` and `-s` use the web app's search endpoint, which is fast but fuzzy; `-s`
then keeps only conversations that really contain the string.

A full export pages through the whole conversation list (about 7 s per 100
conversations) and then downloads each conversation, so it can take a while
for a large history.

## Output

```
export/
├── index.json                  # id, title, update_time of every exported conversation
├── conversations/
│   └── <conversation-id>.json  # raw API response, one file per conversation
└── conversations.json          # all conversations in one array (--single-file)
```

Each conversation is the raw backend JSON. Messages are stored in `mapping`, a
tree of nodes (`parent` / `children`) that keeps every branch created by edits
and regenerations. To get the conversation as displayed in the web app, start
from `current_node` and follow `parent` links up to the root, then reverse:

```python
import json

conv = json.load(open("export/conversations/<conversation-id>.json"))
nodes, node_id = [], conv["current_node"]
while node_id:
    node = conv["mapping"][node_id]
    nodes.append(node)
    node_id = node.get("parent")

for node in reversed(nodes):
    msg = node.get("message")
    if msg and msg["author"]["role"] in ("user", "assistant"):
        text = "\n".join(p for p in msg["content"].get("parts", []) if isinstance(p, str))
        if text.strip():
            print(f"## {msg['author']['role']}\n{text}\n")
```

## Limitations

- Conversations inside ChatGPT *Projects* may not be returned by the
  conversation list.
- Attached files and generated images are referenced but not downloaded.
