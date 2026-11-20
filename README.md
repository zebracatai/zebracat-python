# Zebracat Python SDK

The official Python client for the [Zebracat API](https://api.zebracat.ai/docs): make videos with AI from a brief,
a script or a link, then read, quote, generate and download them.

```bash
pip install zebracat
```

Python 3.9 or newer. One dependency: `httpx`.

## Quickstart

Create an API key at <https://zebracat.ai/api> and export it:

```bash
export ZEBRACAT_API_KEY=sk-...
```

```python
from zebracat import Zebracat

client = Zebracat()  # reads ZEBRACAT_API_KEY

# 1. Plan a video. Planning is free; nothing is charged until you generate.
video = client.create_video(
    brief="How bees make honey",
    auto_generate=False,
    options={"seconds": 30, "aspect": "vertical", "media_type": "ai_video"},
)

# 2. Wait for the plan, then look at the price.
video = client.wait_for_video(video["task_id"])          # returns at plan_ready
print(client.quote_video(video["task_id"]))

# 3. Generate it (this spends credit) and wait for the file.
client.generate_video(video["task_id"])
video = client.wait_for_video(video["task_id"])          # returns at completed
print(video["video_url"])
```

Pass `auto_generate=True` to plan and generate in one call; `wait_for_video` then returns once the video is done.

## Every operation is a method

Each operation of the [OpenAPI document](openapi/openapi.json) is a method named after its `operationId`:
`create_video`, `get_video`, `list_videos`, `quote_video`, `generate_video`, `clone_video`, `translate_video`,
`list_voices`, `list_templates`, `upload_file`, `create_webhook`, `get_account`, and the rest.

- Path parameters are positional: `client.get_video("6f1c…")`.
- Query parameters and body fields are keyword arguments: `client.create_video(brief="…", auto_generate=True)`.
- Leave an argument out to not send it; pass `None` to send JSON `null`. Fields the SDK does not know yet go in
  `extra_body={...}`.
- Answers are plain `dict`s, typed as `TypedDict`s in `zebracat.types` for your editor.
- Files (`upload_file`, `create_clone_voice`) take bytes, an open binary file, or `(filename, bytes, content_type)`.
- Operations the API marks deprecated still work and raise a `DeprecationWarning`.

## Authentication

```python
Zebracat(api_key="sk-...")                 # X-API-KEY, billed from the API balance

Zebracat(                                  # OAuth: Authorization: Bearer zoa_..., billed from plan credits
    access_token="zoa_...",
    refresh_token="zor_...",
    client_id="your-oauth-client-id",
    client_secret=None,                    # only for confidential clients
    on_token_refresh=lambda tokens: save(tokens),
)
```

With a refresh token, a `401` makes the client swap it for a new pair once (`POST /api/oauth/token`, the API rotates
both tokens) and repeat the call. `on_token_refresh` receives the new tokens so you can store them.

`ZEBRACAT_BASE_URL` (or `base_url=`) points the client at another server.

## Retries

A call is repeated up to `max_retries` times (default 3) when the API answers `429`, `500`, `502`, `503` or `504`,
or the connection drops:

- A `Retry-After` header is honoured. A wait longer than `max_retry_wait` (default 60 s, e.g. a daily cap) is raised
  as `RateLimitError` with `retry_after` instead of slept through.
- Without `Retry-After` the wait is 0.5 s, 1 s, 2 s… (at most 8 s) with jitter.
- A `429` is always retried: the API refused before doing anything.
- A `5xx` or a dropped connection is retried only when resending cannot do the work twice: reads, `PUT`, `DELETE`,
  and the paid POSTs whose server replays the first answer for a repeated `Idempotency-Key` (`create_video`,
  `clone_video`, `translate_video` and the v1 creators). Other POSTs raise at once, so a charge is never repeated.

Per call: `request_options={"max_retries": 0, "timeout": 120, "headers": {...}}`.

## Idempotency keys

Every POST that spends money (it can answer `402`, or it buys credit) sends an `Idempotency-Key`. The SDK makes one
per call and reuses it across that call's retries. Pass your own to make a retry from your side safe too:

```python
client.create_video(brief="…", auto_generate=True, idempotency_key=f"order-{order_id}")
```

The same key with a different body answers `422 idempotency_key_reused`.

## Waiting for a video

`wait_for_video(task_id, timeout=1800, poll_interval=None, on_update=None)` reads the video again after the
`poll_after_s` seconds the API gives (falling back to 10–30 s per phase on an API that does not send it). It returns
when nothing more happens on its own:

| phase | what you do next |
|---|---|
| `completed` | `video["video_url"]` is the file; `download_video(task_id)` returns the URL as well |
| `plan_ready` | show `quote_video(task_id)`, then `generate_video(task_id)` and wait again |
| `needs_input` | answer `video["question"]` with `answer_video_question(task_id, ...)` and wait again |

It raises `VideoFailedError` (`.video`, `.phase`, `.error`) when the video fails or is cancelled, and
`WaitTimeoutError` (`.video`) when `timeout` passes first; the video keeps going on our side.

## Errors

Every error from the API is a subclass of `zebracat.APIError` and carries the public error envelope:

```python
from zebracat import InsufficientFundsError, RateLimitError, APIError

try:
    client.generate_video(task_id)
except InsufficientFundsError as e:
    print("Add credit:", e.topup_url)
except RateLimitError as e:
    print("Try again in", e.retry_after, "s")
except APIError as e:
    print(e.status_code, e.code, e.hint, e.request_id)
```

| status | class |
|---|---|
| 400, 422 | `BadRequestError` (`field`, `allowed_values`, `errors`) |
| 401 | `AuthenticationError` |
| 402 | `InsufficientFundsError` (`topup_url`) |
| 403 | `PermissionDeniedError` (`upgrade_url`) |
| 404, 410 | `NotFoundError` |
| 409 | `ConflictError` |
| 429 | `RateLimitError` (`retry_after`) |
| 5xx | `InternalServerError` |
| no answer | `APIConnectionError`, `APITimeoutError` |

`request_id` is the id to quote to support: the API's `request_id` (also the `X-Request-ID` header). The SDK sends
an `X-Request-ID` of its own on every call, so even a connection error has one.

## Development

```bash
uv venv && uv pip install -e '.[test]'
python scripts/generate.py          # regenerate src/zebracat/_generated from openapi/openapi.json
python scripts/generate.py --check  # CI: fails when the generated code is out of date
pytest                              # runs against a local stub server, no network
```

`openapi/openapi.json` comes from the backend: `python manage.py spectacular --format openapi-json --file
openapi.json` (or download `https://api.zebracat.ai/docs/openapi.json`). Copy it here, regenerate, commit both.

## Releasing

Bump `src/zebracat/_version.py`, commit, tag `vX.Y.Z` and push the tag. `.github/workflows/release.yml` tests,
builds and publishes to PyPI with trusted publishing (GitHub OIDC; no token is stored anywhere).
