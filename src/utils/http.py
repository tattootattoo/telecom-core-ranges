"""
Shared HTTP helper: retries transient failures (connection errors, timeouts, 429, 5xx)
with exponential backoff + jitter, so one slow or overloaded moment on RIPEstat or
PeeringDB doesn't need a full 30-day cache-expiry cycle to be retried. Non-retryable
4xx errors (404, 400, etc.) fail immediately instead of wasting the retry budget on
something that won't succeed no matter how many times it's tried.
"""
import random
import time

import requests


class FetchError(Exception):
    """Raised when a request still fails after every retry attempt is exhausted."""


def get_with_retry(session, url, params=None, timeout=20, max_retries=3, backoff_base=1.5):
    """
    GETs a URL, retrying on connection errors, timeouts, HTTP 429, and HTTP 5xx.
    Returns the successful `requests.Response`. Raises FetchError if every attempt
    fails, or immediately re-raises requests.HTTPError for a non-retryable 4xx.
    """
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            resp = session.get(url, params=params, timeout=timeout)
            if resp.status_code == 429 or resp.status_code >= 500:
                last_exc = Exception(f"HTTP {resp.status_code}")
            else:
                resp.raise_for_status()  # a non-retryable 4xx raises straight out below
                return resp
        except requests.HTTPError:
            raise
        except Exception as e:
            last_exc = e

        if attempt < max_retries:
            time.sleep((backoff_base ** attempt) + random.uniform(0, 0.5))

    raise FetchError(f"failed after {max_retries + 1} attempts: {last_exc}")
