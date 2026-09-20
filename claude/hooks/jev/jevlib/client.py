"""Jev transport — key resolution, retrying POST, typed failures.

Request/response shape (faithful to the shadow scripts):
  POST {state, model, questions:{qid:{type,instructions,criteria}}}
  ->   {answers:{qid:{choice, probability, confidence, ...}}}
"""

import json
import os
import time
import urllib.error
import urllib.request

DEFAULT_API_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"
ENV_FILE = os.path.expanduser("~/.hermes/.env")
SECRET_FILE = os.path.expanduser("~/.secrets/typesafe-api-key")


class JevUnavailable(RuntimeError):
    """No API key resolvable — caller decides fail-open vs fail-closed."""


class JevAPIError(RuntimeError):
    """Request failed after retries; message never contains the key."""


class _Retryable(Exception):
    """Internal: transport returned a retryable (5xx) status."""

    def __init__(self, status):
        super().__init__(f"http {status}")
        self.status = status


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
        for attempt in range(1, attempts + 1):
            t0 = time.monotonic()
            try:
                if self._transport is not None:
                    status, payload = self._transport(body)
                    if status >= 400:
                        last_err = f"http {status}"
                        if status < 500:
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
                    with urllib.request.urlopen(req, timeout=self.timeout) as resp:
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
                if exc.code < 500:
                    break  # 4xx: retrying will not help
            except _Retryable as exc:
                last_err = str(exc)
            except JevAPIError:
                raise
            except Exception as exc:  # noqa: BLE001 — network/parse, retryable
                last_err = str(exc)[:120]
            if attempt < attempts:
                self._sleep(1.0 * attempt)
        raise JevAPIError(f"jev ask failed after {attempts} attempt(s): {last_err}")

    def choice(self, state, question_id, question):
        """Single-question convenience wrapper. Returns (answer_dict, meta)."""
        answers, meta = self.ask(state, {question_id: question})
        answer = answers.get(question_id)
        if not isinstance(answer, dict):
            raise JevAPIError(f"answer for {question_id!r} missing in response")
        return answer, meta
