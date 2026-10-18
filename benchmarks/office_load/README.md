# Office load

Measures how many concurrent editing sessions one office document server holds while every save meets the bar.

`oload` drives runtime-office load clients over their HTTP API and plays the harness's part toward the document
server: it serves the input files, signs the editor configs, sends forcesave through `/command`, receives the
callbacks and downloads each saved file.

Never point it at a document server people use.

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
