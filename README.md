# Groceries for Home Assistant

A Home Assistant add-on that turns a pasted grocery list into a cart at a Kroger-family store
(Fred Meyer, Kroger, QFC, Ralphs, ...), using Kroger's public API.

## Install

In Home Assistant: **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, add this repository's URL,
then install **Groceries**. See [grocer/DOCS.md](grocer/DOCS.md) for setup.

Built for amd64.

## Development

```bash
cd grocer
python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest
GROCER_DATA_DIR=devdata KROGER_CLIENT_ID=... KROGER_CLIENT_SECRET=... .venv/bin/python -m grocer
```

Then open <http://127.0.0.1:8099>.

## License

MIT
