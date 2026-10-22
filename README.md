# minty-shared-py

Minty's shared Python for every backend: Flask (`Minty`) and the four Django APIs. This is where
the modernisation plan's `minty_shared` starts (`Minty/docs/modernisation/modernisation_plan.md`,
Part 2). Models, enums, the permission policy and entitlements are **not** here: they belong to
the planned `minty-db` package.

| Module | Needs | What |
|---|---|---|
| `minty_shared.logctx`, `minty_shared.log` | stdlib | the one log line, its trace/user/entity/step context, loguru's call shape over `logging` |
| `minty_shared.config` | stdlib | `APP_ENV`, flags, the `SECRET_KEY` placeholder refusal, `DATABASE_URL` (identifier-only `?schema=`), `S3_URL`, `SMTP_URL`, `PREVIEW_ORIGIN_REGEX`, and Django's `DATABASES`/`EMAIL_*` shapes |
| `minty_shared.money` | stdlib | `HKD 1,234.50`: the code, a non-breaking space, the currency's places; `format_price` drops a whole `.00`; no currency says so |
| `minty_shared.time` | stdlib + `tzdata` | `DEFAULT_ENTITY_TIMEZONE`, a stored instant on a company's calendar, `validate_timezone` |
| `minty_shared.tokens` | extra `jwt` | `decode_bearer` (60 s clock skew everywhere), `service_assertion`, `assertion_headers` |
| `minty_shared.django.middleware` | extra `django` | `RequestLoggingMiddleware` (settings `MINTY_HTTP_LOGGER`, optional `MINTY_LOGGABLE_PATH`) |
| `minty_shared.django.errors` | extra `django` | the envelope below: `register_error_handlers`, `ApiRefusal`, `validation_message`, `field_messages` |
| `minty_shared.django.client` | extra `django` | `forward`/`proxy`: call another Minty backend as the current caller |
| `minty_shared.testing` | extra `testing` | the Postgres-harness pytest plugin (`pytest_plugins = ["minty_shared.testing.pg"]`) and the source-walking guards |

## The error envelope

Every API answers a refusal the same way:

```
{"error": "<a sentence to show>", "fields": {"amount": "The amount is still empty. Add the amount to continue."}}
```

- `fields` appears only when input is refused. Any key a service routes on (Petty Cash's `to`,
  `report_id`) rides along.
- Schema refusals are **422**. A rule's own refusal keeps the status the rule names. 401 is
  `{"error": "Unauthorized"}`. 500 is one house sentence, with the detail in the log, never in the
  response.

## The log line

```
2026-10-22T08:42:15.123Z INFO [trace=a1b2c3d4] [user=<uuid>|entity=<uuid>|step=payment.approve] Bill approved bill=…
```

| Field | Set by | Absent |
|---|---|---|
| timestamp | the record, in UTC with milliseconds | — |
| `trace` | `start_trace(request.headers.get("X-Request-ID"))` at the start of a request, scheduler pass or command. A header of 8–32 lowercase hex digits is kept; anything else is replaced with 8 fresh digits, so a header can't forge a line | `-` |
| `user`, `entity` | `bind(user=…, entity=…)` in the auth layer, only **after** the request is authorised. Full ids | `-` |
| `step` | `with log_step("payment.approve"):` around a unit of work; the route name is the fallback (`set_default_step`) | `-` |

The values live in `ContextVar`s, so every line written during the request carries them,
including Django's and werkzeug's own. `ContextFilter` is attached to the handlers, not the
loggers, so propagated records get it too.

- **Across services:** send `outgoing_headers()` on every call to another Minty backend, and
  never to Xero, Stripe or anyone else. Each backend echoes `X-Request-ID` on its response.
- **Threads:** a new thread starts with an empty context. Use
  `threading.Thread(target=carry_context(work))` to keep the caller's trace.
- **Configuration:** `logging_config({...})` returns the `dictConfig` dict:

  ```python
  LOGGING = logging_config(
      {"billing-api": "core.log", "billing-api.http": "api.log"},
      log_dir=BASE_DIR / "logs",
      no_propagate=["billing-api.http"],
      level=LOG_LEVEL,
  )
  ```

## `minty_shared.log`

`get_logger("minty")` returns a `BraceLogger`: loguru's call shape (`logger.info("{} issued", n)`,
`logger.exception(...)`) over a standard `logging.Logger`. It is what Flask and the subscription
engine log through, so their messages stay byte-identical.

## Using it

Pin a tag in `requirements.txt` (tags are **immutable**: a change is a new tag):

```
minty-shared[jwt,django,testing] @ https://github.com/minty-oliveandvine/minty-shared-py/archive/refs/tags/v0.2.tar.gz
```

A Django API takes all three extras; Flask takes `[jwt]`.

The tag's **archive**, not `git+https://…@v0.1`: the `python:3.13-slim` images the Django APIs
build from have no git, so a `git+` pin installs in CI and fails on Render. The repo is public, so
the archive downloads with no key. A project built by hatchling (Minty) also needs
`[tool.hatch.metadata] allow-direct-references = true`. While developing against a
local checkout, run `python -m pip install -e C:\Github\minty-shared-py` in the consumer's venv.

## Developing

```
uv venv --seed --python 3.13 .venv
.venv/Scripts/python -m pip install -e ".[dev]"
.venv/Scripts/python -m pytest -q
.venv/Scripts/python -m ruff check . && .venv/Scripts/python -m ruff format --check .
.venv/Scripts/python -m pyright
```
