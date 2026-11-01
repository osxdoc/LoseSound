# Contributing

Thanks for taking a look at LoseSound. This project is small on purpose: a stdlib-only Python
service, a plain HTML/CSS/JS UI and no build step.

## Development setup

```bash
git clone https://github.com/osxdoc/LoseSound.git
cd LoseSound
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
```

Run the service locally (it will create `./data/config.json`):

```bash
PYTHONPATH=src CONFIG_PATH=./data/config.json LISTEN_PORT=8092 \
AFTERTOUCH_URL=http://127.0.0.1:8091 SPEAKER_IP=127.0.0.1 \
python3 -m losesound
```

The tests use fake HTTP servers for the speaker, AfterTouch, radio-browser and the upstream stream;
no real speaker or Docker is needed.

## Pull requests

- Keep changes focused; describe the problem and the behaviour you changed.
- Add or update tests. New endpoints and mode logic should be covered by `tests/`.
- Run `ruff check .`, `ruff format --check .` and `pytest` before pushing.
- Update `CHANGELOG.md` under `Unreleased` for user-visible changes.
- Do not commit `.env`, station lists, stream tokens or speaker serial numbers.

## Coding guidelines

- Python 3.11+, standard library only for the runtime code. Dev-only tools live in
  `[project.optional-dependencies]`.
- Keep functions small and explicit; validate everything that comes from the network.
- Errors that users can fix should be `ApiError` with a short message and a stable `code`.
- Never log full stream URLs by default; tokens belong in query strings.
- The UI must escape every value that comes from the server (`esc()` in `app.js`).

## Reporting bugs

Please include:

- LoseSound version (`/api/health`), AfterTouch version (`/setup/version`)
- speaker model and firmware (`/info`)
- the relevant preset location and mode
- output of `python3 scripts/verify.py --json`
- relevant `docker compose logs preset-proxy` lines (URLs are redacted by default)

Security issues: see [SECURITY.md](SECURITY.md).
