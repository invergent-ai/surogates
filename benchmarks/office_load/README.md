# Office load

Measures how many concurrent editing sessions one office document server holds while every save meets the bar.

`oload` drives runtime-office load clients over their HTTP API and plays the harness's part toward the document
server: it serves the input files, signs the editor configs, sends forcesave through `/command`, receives the
callbacks and downloads each saved file.

Never point it at a document server people use.

It answers the document server as the runtime does: `{"error": 0}` once a save is kept; 400, 403, 404 or 502
otherwise, after which the document server retries the callback. Every forcesave and every download gets a client of
its own, as the harness's do.

One difference makes its numbers slightly optimistic. Its `/files` and its callbacks answer as soon as their own work
is done: reading the input from memory, fetching the saved file. The runtime also reads and writes storage and the
database first. Open times, and how long the document server holds each callback, are a little shorter here than in
production.

## Settings

Everything site-specific comes from the environment, or from a git-ignored `.env` in this directory:

| Name | What |
| --- | --- |
| `OLOAD_DS_URL` | the document server as the pages reach it, in the cluster |
| `OLOAD_COMMAND_URL` | the document server as this host reaches it |
| `OLOAD_JWT_SECRET` | the document server's JWT secret |
| `OLOAD_CLIENT_TOKEN` | the load clients' bearer token |
| `OLOAD_LISTEN` | `host:port` the tool binds |
| `OLOAD_PUBLIC_BASE` | the base URL the document server reaches the tool at |
| `OLOAD_NAMESPACE` | the office namespace |
| `KUBECONFIG` | the cluster, for kubectl |

## Tests

```bash
uv venv -q --python 3.12 .venv && uv pip install -q -p .venv/bin/python -e '.[dev]'
.venv/bin/pytest -q -p no:cacheprovider
```
