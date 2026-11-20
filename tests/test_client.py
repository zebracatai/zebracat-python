"""The hand-written layer against a local stub server: auth, retries, idempotency keys, typed errors, waiting."""

from __future__ import annotations

import json
import pathlib
import warnings

import pytest

import zebracat
from stub_server import Reply
from zebracat import (
    APIConnectionError,
    AuthenticationError,
    BadRequestError,
    ConflictError,
    InsufficientFundsError,
    InternalServerError,
    NotFoundError,
    RateLimitError,
    VideoFailedError,
    WaitTimeoutError,
    Zebracat,
    ZebracatError,
)

API = "/api/v1/public"
TASK = "6f1c0d2e9b8a4c7d8e9f0a1b2c3d4e5f"
ENVELOPE_402 = {
    "error": "Not enough API balance for this video.",
    "code": "insufficient_balance",
    "type": "insufficient_funds",
    "retryable": False,
    "hint": "Add credit, then generate again.",
    "topup_url": "https://zebracat.ai/api#billing",
    "request_id": "req-from-body",
}


def video(phase: str, **extra):
    base = {"task_id": TASK, "status": "processing", "phase": phase, "engine": "director_v2"}
    return {**base, **extra}


# ---------------------------------------------------------------------- auth


def test_api_key_header_user_agent_and_request_id(stub, client):
    stub.on("GET", f"{API}/account", Reply(200, {"email": "a@b.c"}))
    assert client.get_account() == {"email": "a@b.c"}
    (call,) = stub.calls()
    assert call.headers["x-api-key"] == "sk-test"
    assert "authorization" not in call.headers
    assert call.headers["user-agent"] == f"zebracat-python/{zebracat.__version__}"
    assert len(call.headers["x-request-id"]) == 32


def test_api_key_from_environment(stub, monkeypatch):
    monkeypatch.setenv("ZEBRACAT_API_KEY", "sk-env")
    stub.on("GET", f"{API}/account", Reply(200, {}))
    with Zebracat(base_url=stub.url) as c:
        c.get_account()
    assert stub.calls()[0].headers["x-api-key"] == "sk-env"


def test_no_credentials_is_refused(monkeypatch):
    monkeypatch.delenv("ZEBRACAT_API_KEY", raising=False)
    with pytest.raises(ZebracatError, match="No credentials"):
        Zebracat()
    with pytest.raises(ZebracatError, match="not both"):
        Zebracat(api_key="sk", access_token="zoa_x")


def test_oauth_bearer_refreshes_once_on_401_and_reports_new_tokens(stub, fake_time):
    stub.on("GET", f"{API}/account", Reply(401, {"detail": "expired"}), Reply(200, {"ok": True}))
    stub.on("POST", "/api/oauth/token", Reply(200, {"access_token": "zoa_new", "refresh_token": "zor_new",
                                                     "token_type": "Bearer", "expires_in": 3600}))
    saved = []
    c = Zebracat(access_token="zoa_old", refresh_token="zor_old", client_id="cli", base_url=stub.url,
                 on_token_refresh=saved.append)
    c._sleep = fake_time.sleep
    assert c.get_account() == {"ok": True}
    first, refresh, second = stub.requests
    assert first.headers["authorization"] == "Bearer zoa_old"
    assert refresh.path == "/api/oauth/token"
    form = {k: v[0] for k, v in __import__("urllib.parse").parse.parse_qs(refresh.body.decode()).items()}
    assert form == {"grant_type": "refresh_token", "refresh_token": "zor_old", "client_id": "cli"}
    assert second.headers["authorization"] == "Bearer zoa_new"
    assert c.refresh_token == "zor_new"
    assert saved and saved[0]["access_token"] == "zoa_new"


def test_oauth_refused_refresh_raises_authentication_error(stub):
    stub.on("GET", f"{API}/account", Reply(401, {"detail": "expired"}))
    stub.on("POST", "/api/oauth/token", Reply(400, {"error": "invalid_grant"}))
    c = Zebracat(access_token="zoa_old", refresh_token="zor_old", client_id="cli", base_url=stub.url)
    with pytest.raises(AuthenticationError, match="sign in again"):
        c.get_account()


def test_401_without_refresh_token_raises(stub, client):
    stub.on("GET", f"{API}/account", Reply(401, {"detail": "Invalid API key."}, {"X-Request-ID": "rid-401"}))
    with pytest.raises(AuthenticationError) as info:
        client.get_account()
    assert info.value.request_id == "rid-401"
    assert "Invalid API key." in str(info.value)


# ---------------------------------------------------------------------- retries


def test_429_honours_retry_after_then_succeeds(stub, client, fake_time):
    stub.on("GET", f"{API}/account", Reply(429, {"error": "slow down"}, {"Retry-After": "7"}), Reply(200, {"ok": 1}))
    assert client.get_account() == {"ok": 1}
    assert len(stub.calls()) == 2
    assert fake_time.sleeps == [7.0]


def test_retry_after_longer_than_max_wait_is_raised_not_slept(stub, client, fake_time):
    stub.on("GET", f"{API}/account", Reply(429, {"error": "daily cap", "code": "daily_cap"}, {"Retry-After": "3600"}))
    with pytest.raises(RateLimitError) as info:
        client.get_account()
    assert info.value.retry_after == 3600.0
    assert len(stub.calls()) == 1
    assert fake_time.sleeps == []


def test_5xx_on_a_read_is_retried_with_backoff_until_max_retries(stub, client, fake_time):
    stub.on("GET", f"{API}/account", Reply(503, {"error": "busy", "request_id": "rid-503"}))
    with pytest.raises(InternalServerError) as info:
        client.get_account()
    assert len(stub.calls()) == 1 + client.max_retries
    assert len(fake_time.sleeps) == client.max_retries
    assert all(0 < s <= 8 for s in fake_time.sleeps)
    assert fake_time.sleeps == sorted(fake_time.sleeps) or fake_time.sleeps[-1] > fake_time.sleeps[0]
    assert info.value.request_id == "rid-503"


def test_per_call_max_retries(stub, client):
    stub.on("GET", f"{API}/account", Reply(502, {"error": "bad gateway"}))
    with pytest.raises(InternalServerError):
        client.get_account(request_options={"max_retries": 0})
    assert len(stub.calls()) == 1


def test_paid_post_sends_one_idempotency_key_reused_across_retries(stub, client):
    stub.on("POST", f"{API}/videos", Reply(503, {"error": "busy"}), Reply(202, video("planning")))
    answer = client.create_video(brief="How bees make honey", auto_generate=False)
    assert answer["phase"] == "planning"
    first, second = stub.calls("POST", f"{API}/videos")
    key = first.headers["idempotency-key"]
    assert key.startswith("zc-") and second.headers["idempotency-key"] == key
    assert first.json() == {"brief": "How bees make honey", "auto_generate": False}
    assert first.headers["x-request-id"] == second.headers["x-request-id"]


def test_caller_idempotency_key_is_sent(stub, client):
    stub.on("POST", f"{API}/videos", Reply(202, video("planning")))
    client.create_video(brief="x", idempotency_key="order-42")
    assert stub.calls()[0].headers["idempotency-key"] == "order-42"


def test_paid_post_without_server_replay_is_not_retried_on_5xx(stub, client):
    stub.on("POST", f"{API}/generate_image", Reply(500, {"error": "boom"}))
    with pytest.raises(InternalServerError):
        client.generate_image(prompt="a cat")
    assert len(stub.calls()) == 1
    assert stub.calls()[0].headers["idempotency-key"]


def test_paid_post_without_server_replay_is_retried_on_429(stub, client):
    stub.on("POST", f"{API}/generate_image", Reply(429, {"error": "x"}, {"Retry-After": "1"}), Reply(200, {"ok": 1}))
    assert client.generate_image(prompt="a cat") == {"ok": 1}
    assert len(stub.calls()) == 2


def test_free_post_has_no_idempotency_key(stub, client):
    stub.on("POST", f"{API}/webhooks", Reply(201, {"id": 1}))
    client.create_webhook(url="https://example.com/hook", events=["video.completed"])
    assert "idempotency-key" not in stub.calls()[0].headers


def test_connection_refused_raises_connection_error_with_request_id(fake_time):
    c = Zebracat(api_key="sk", base_url="http://127.0.0.1:9", max_retries=1)
    c._sleep = fake_time.sleep
    with pytest.raises(APIConnectionError) as info:
        c.get_account()
    assert info.value.request_id and len(fake_time.sleeps) == 1


# ---------------------------------------------------------------------- errors


def test_402_is_typed_and_carries_the_envelope(stub, client):
    stub.on("POST", f"{API}/videos/{TASK}/generate", Reply(402, ENVELOPE_402, {"X-Request-ID": "req-header"}))
    with pytest.raises(InsufficientFundsError) as info:
        client.generate_video(TASK)
    err = info.value
    assert err.status_code == 402
    assert err.code == "insufficient_balance" and err.type == "insufficient_funds"
    assert err.topup_url == "https://zebracat.ai/api#billing"
    assert err.request_id == "req-from-body"  # the body wins over the header
    assert "req-from-body" in str(err) and "hint:" in str(err)


@pytest.mark.parametrize(
    "status, cls",
    [(400, BadRequestError), (422, BadRequestError), (404, NotFoundError), (410, NotFoundError),
     (409, ConflictError), (500, InternalServerError)],
)
def test_status_maps_to_error_class(stub, client, status, cls):
    stub.on("GET", f"{API}/videos/{TASK}", Reply(status, {"error": "nope", "field": "task_id"}, {"X-Request-ID": "h"}))
    with pytest.raises(cls) as info:
        client.get_video(TASK, request_options={"max_retries": 0})
    assert info.value.request_id == "h" and info.value.field == "task_id"


# ---------------------------------------------------------------------- wait_for_video


def test_wait_for_video_sleeps_poll_after_s_until_completed(stub, client, fake_time):
    stub.on(
        "GET", f"{API}/videos/{TASK}",
        Reply(200, video("planning", poll_after_s=10)),
        Reply(200, video("generating", poll_after_s=30)),
        Reply(200, video("rendering", poll_after_s=15)),
        Reply(200, video("completed", poll_after_s=None, video_url="https://cdn/x.mp4")),
    )
    seen = []
    done = client.wait_for_video(TASK, on_update=lambda v: seen.append(v["phase"]))
    assert done["video_url"] == "https://cdn/x.mp4"
    assert fake_time.sleeps == [10, 30, 15]
    assert seen == ["planning", "generating", "rendering", "completed"]


def test_wait_for_video_returns_at_plan_ready(stub, client, fake_time):
    stub.on("GET", f"{API}/videos/{TASK}", Reply(200, video("planning", poll_after_s=10)),
            Reply(200, video("plan_ready", poll_after_s=None)))
    assert client.wait_for_video(TASK)["phase"] == "plan_ready"


def test_wait_for_video_raises_on_failed(stub, client):
    stub.on("GET", f"{API}/videos/{TASK}",
            Reply(200, video("failed", poll_after_s=None, error={"code": "engine", "message": "render failed"})))
    with pytest.raises(VideoFailedError, match="render failed") as info:
        client.wait_for_video(TASK)
    assert info.value.phase == "failed"


def test_wait_for_video_times_out_without_oversleeping(stub, client, fake_time):
    stub.on("GET", f"{API}/videos/{TASK}", Reply(200, video("generating", poll_after_s=30)))
    with pytest.raises(WaitTimeoutError) as info:
        client.wait_for_video(TASK, timeout=70)
    assert fake_time.sleeps == [30, 30, 10]
    assert info.value.video["phase"] == "generating"


def test_wait_for_video_falls_back_per_phase_without_poll_after_s(stub, client, fake_time):
    stub.on("GET", f"{API}/videos/{TASK}", Reply(200, video("planning")), Reply(200, video("generating")),
            Reply(200, video("completed")))
    assert client.wait_for_video(TASK)["phase"] == "completed"
    assert fake_time.sleeps == [10.0, 30.0]


def test_wait_for_video_poll_interval_overrides(stub, client, fake_time):
    stub.on("GET", f"{API}/videos/{TASK}", Reply(200, video("generating", poll_after_s=30)),
            Reply(200, video("completed", poll_after_s=None)))
    client.wait_for_video(TASK, poll_interval=5)
    assert fake_time.sleeps == [5]


# ---------------------------------------------------------------------- shapes


def test_download_returns_the_redirect_location(stub, client):
    stub.on("GET", f"{API}/video/{TASK}/download", Reply(302, None, {"Location": "https://cdn/v.mp4"}))
    assert client.download_video(TASK) == "https://cdn/v.mp4"


def test_delete_answers_none(stub, client):
    stub.on("DELETE", f"{API}/webhooks/5", Reply(204))
    assert client.delete_webhook(5) is None


def test_multipart_upload_sends_the_file(stub, client):
    stub.on("POST", f"{API}/uploads", Reply(201, {"url": "https://cdn/u.png"}))
    client.upload_file(file=("cat.png", b"\x89PNGdata", "image/png"), kind="image")
    call = stub.calls()[0]
    assert call.headers["content-type"].startswith("multipart/form-data")
    assert b'filename="cat.png"' in call.body and b"\x89PNGdata" in call.body
    assert b'name="kind"' in call.body and b"image" in call.body


def test_left_out_arguments_are_not_sent_and_none_is_null(stub, client):
    stub.on("POST", f"{API}/videos", Reply(202, video("planning")))
    client.create_video(brief="x", voice_id=None, extra_body={"future_field": 1})
    assert stub.calls()[0].json() == {"brief": "x", "voice_id": None, "future_field": 1}


def test_path_parameters_are_escaped(stub, client):
    stub.on("GET", f"{API}/videos/a%2Fb", Reply(200, video("completed")))
    client.get_video("a/b")
    assert stub.calls()[0].path == f"{API}/videos/a%2Fb"


@pytest.mark.parametrize("bad", ["", ".", ".."])
def test_path_parameters_that_would_change_the_endpoint_are_refused(stub, client, bad):
    with pytest.raises(ZebracatError, match="Invalid task_id"):
        client.get_video(bad)
    assert stub.calls() == []


def test_deprecated_operation_warns(stub, client):
    stub.on("GET", f"{API}/video/status", Reply(200, {"status": "completed"}))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        client.get_video_task_status(task_id=TASK)
    assert any(issubclass(w.category, DeprecationWarning) for w in caught)
    assert stub.calls()[0].query == {"task_id": [TASK]}


def test_every_operation_in_the_spec_is_a_method():
    spec = json.loads((pathlib.Path(__file__).parent.parent / "openapi" / "openapi.json").read_text())
    ids = {op["operationId"] for item in spec["paths"].values() for m, op in item.items()
           if m in ("get", "post", "put", "patch", "delete")}
    missing = [i for i in ids if not callable(getattr(Zebracat, i, None))]
    assert not missing and len(ids) > 50
