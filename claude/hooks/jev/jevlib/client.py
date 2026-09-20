"""Jev transport — key resolution, retrying POST, typed failures.

Request/response shape (faithful to the shadow scripts):
  POST {state, model, questions:{qid:{type,instructions,criteria}}}
  ->   {answers:{qid:{choice, probability, confidence, ...}}}
"""

import json
import os
import ssl
import time
import urllib.error
import urllib.request

DEFAULT_API_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"
ENV_FILE = os.path.expanduser("~/.hermes/.env")
SECRET_FILE = os.path.expanduser("~/.secrets/typesafe-api-key")

# Jev documents four error statuses: 401, 422, 429, 529. Of the 4xx, only 429
# (rate limit) clears on its own — 401/422 are permanent and retrying wastes a
# request. 429 is *expected* under the published limits (250k tokens/s,
# 1200 req/min), and a shadow replay batches thousands of calls, so treating it
# as permanent turns a routine throttle into an aborted run.
RETRYABLE_4XX = frozenset({429})
MAX_RETRY_AFTER = 30.0  # cap server-suggested waits so a gate cannot hang


class JevUnavailable(RuntimeError):
    """No API key resolvable — caller decides fail-open vs fail-closed."""


class JevAPIError(RuntimeError):
    """Request failed after retries; message never contains the key."""


class _Retryable(Exception):
    """Internal: transport returned a retryable status (5xx, or 429)."""

    def __init__(self, status, retry_after=None):
        super().__init__(f"http {status}")
        self.status = status
        self.retry_after = retry_after


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect — the request carries a bearer token.

    urllib re-sends headers on a redirect, so a 30x pointing off-host would
    hand the API key to whatever answered. Returning None here makes urllib
    raise the original HTTPError instead of following it; a 3xx is not
    retryable, so the call fails fast and loudly rather than leaking.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = None


def _opener():
    global _OPENER
    if _OPENER is None:
        _OPENER = urllib.request.build_opener(
            _NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context())
        )
    return _OPENER


def _is_retryable(status):
    return status >= 500 or status in RETRYABLE_4XX


def _retry_after_seconds(headers):
    """Parse a Retry-After header (delta-seconds form only); None when absent."""
    if not headers:
        return None
    raw = headers.get("Retry-After")
    if raw is None:
        return None
    try:
        secs = float(str(raw).strip())
    except (TypeError, ValueError):
        return None  # HTTP-date form: fall back to our own backoff
    if secs < 0:
        return None
    return min(secs, MAX_RETRY_AFTER)


def resolve_key(env=None, env_file=None, secret_file=None):
    """Order: env var -> env-var-named file -> secret file -> hermes env file."""
    env = os.environ if env is None else env
    env_file = ENV_FILE if env_file is None else env_file
    secret_file = SECRET_FILE if secret_file is None else secret_file
    key = (env.get("TYPESAFE_API_KEY") or "").strip()
    if key:
        return key
    path = (env.get("TYPESAFE_API_KEY_FILE") or "").strip()
    if path and os.path.exists(path):
        with open(path) as fh:
            got = fh.read().strip()
        if got:
            return got
    if secret_file and os.path.exists(secret_file):
        with open(secret_file) as fh:
            got = fh.read().strip()
        if got:
            return got
    if env_file and os.path.exists(env_file):
        with open(env_file) as fh:
            for line in fh:
                if line.startswith("TYPESAFE_API_KEY="):
                    got = line.split("=", 1)[1].strip()
                    if got:
                        return got
    return None


class JevClient:
    def __init__(
        self,
        key=None,
        api_url=DEFAULT_API_URL,
        model=DEFAULT_MODEL,
        timeout=20.0,
        retries=2,
        sleeper=time.sleep,
        transport=None,
    ):
        self._key_override = key
        self.api_url = api_url
        self.model = model
        self.timeout = timeout
        self.retries = retries  # total attempts = retries (>=1)
        self._sleep = sleeper
        self._transport = transport  # tests: callable(body_bytes) -> (status, payload)

    @property
    def key(self):
        if self._key_override is not None:
            return self._key_override
        return resolve_key()

    def ask(self, state, questions):
        """Returns (answers_dict, meta). Raises JevUnavailable/JevAPIError."""
        key = self.key
        if not key:
            raise JevUnavailable("TYPESAFE_API_KEY not found (env or key files)")
        body = json.dumps({"state": state, "model": self.model, "questions": questions}).encode()
        attempts = max(1, self.retries)
        last_err = None
        wait_override = None
        for attempt in range(1, attempts + 1):
            t0 = time.monotonic()
            try:
                if self._transport is not None:
                    status, payload = self._transport(body)
                    if status >= 400:
                        last_err = f"http {status}"
                        if not _is_retryable(status):
                            break
                        raise _Retryable(status)
                    data = json.loads(payload.decode())
                    answers = data.get("answers")
                    if not isinstance(answers, dict):
                        raise ValueError("response missing answers object")
                    return answers, {
                        "latency_ms": int((time.monotonic() - t0) * 1000),
                        "model": self.model,
                        "attempts": attempt,
                        "error": None,
                    }
                else:
                    req = urllib.request.Request(
                        self.api_url,
                        data=body,
                        method="POST",
                        headers={
                            "Authorization": f"Bearer {key}",
                            "Content-Type": "application/json",
                        },
                    )
                    with _opener().open(req, timeout=self.timeout) as resp:
                        status, payload = resp.status, resp.read()
                data = json.loads(payload.decode())
                answers = data.get("answers")
                if not isinstance(answers, dict):
                    raise ValueError("response missing answers object")
                return answers, {
                    "latency_ms": int((time.monotonic() - t0) * 1000),
                    "model": self.model,
                    "attempts": attempt,
                    "error": None,
                }
            except urllib.error.HTTPError as exc:
                last_err = f"http {exc.code}"
                if not _is_retryable(exc.code):
                    break  # 401/422 and friends: retrying will not help
                wait_override = _retry_after_seconds(getattr(exc, "headers", None))
            except _Retryable as exc:
                last_err = str(exc)
                wait_override = exc.retry_after
            except JevAPIError:
                raise
            except Exception as exc:  # noqa: BLE001 — network/parse, retryable
                last_err = str(exc)[:120]
            if attempt < attempts:
                self._sleep(wait_override if wait_override is not None else 1.0 * attempt)
                wait_override = None
        raise JevAPIError(f"jev ask failed after {attempts} attempt(s): {last_err}")

    def choice(self, state, question_id, question):
        """Single-question convenience wrapper. Returns (answer_dict, meta)."""
        answers, meta = self.ask(state, {question_id: question})
        answer = answers.get(question_id)
        if not isinstance(answer, dict):
            raise JevAPIError(f"answer for {question_id!r} missing in response")
        return answer, meta
