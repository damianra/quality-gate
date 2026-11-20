"""FR-O34 prompt-redaction corpus tests.

Every shape rule must fire on a matching token and the entropy-gated
assignment rule must flag real credentials while skipping
placeholders and low-entropy strings — the corpus is the last control
before text leaves the host.
"""

from __future__ import annotations

import pytest

from pr_review_orch.secrets import _entropy, redact


@pytest.mark.parametrize("kind,snippet", [
    ("aws-access-key", "key = AKIAIOSFODNN7EXAMPLE"),
    ("github-token",
     'tok = "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"'),
    ("github-token",
     'tok = "github_pat_11AAA0AAAA0abcdefghijklmnopqrstuvwxyz0123456789"'),
    ("anthropic-key",
     'key = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"'),
    ("openai-key",
     'key = "sk-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcd"'),
    ("slack-token",
     'tok = "xoxb-123456789012-abcdefghijkl"'),
    ("google-api-key",
     'key = "AIzaSyDaGmWKa4JsXZ-HjGw8rN2vT4bU6cV3eX5"'),
    ("jwt", "tok = eyJhbGciOiJIUzI1NiJ9."
            "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"),
    ("private-key",
     "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n"
     "-----END RSA PRIVATE KEY-----"),
])
def test_shape_rules_redact(kind: str, snippet: str) -> None:
    out, n = redact(f"prefix {snippet} suffix")
    assert n == 1
    assert f"[REDACTED:{kind}]" in out
    # the matched text is gone, not just wrapped
    for tok in ("AKIAIOSFODNN7EXAMPLE", "ghp_", "sk-ant-", "xoxb-",
                "AIza", "eyJhbGciOiJIUzI1NiJ9", "MIIabc"):
        assert tok not in out


def test_generic_assignment_redacts_value_keeps_shape() -> None:
    out, n = redact('api_key = "xJ9fK2mQ7vB4nR8tY3wE6uI1oP5aS"')
    assert n == 1
    assert 'api_key = "[REDACTED:generic-secret]"' in out
    assert "xJ9fK2mQ7vB4nR8tY3wE6uI1oP5aS" not in out


@pytest.mark.parametrize("name", [
    "password", "token", "api_key", "secret", "client_secret",
    "access_key", "auth_token", "credentials",
    # compound identifiers — the credential word sits inside
    "db_password", "admin_token", "accessToken", "AWS_SECRET_KEY",
    "db_pwd",
])
def test_generic_assignment_name_coverage(name: str) -> None:
    out, n = redact(f'{name} = "xJ9fK2mQ7vB4nR8tY3wE6uI1oP5aS"')
    assert n == 1, name
    assert "[REDACTED:generic-secret]" in out


@pytest.mark.parametrize("snippet", [
    'password = "hunter2"',            # too short + low entropy
    'password = "changeme123"',        # placeholder-ish + below floor
    'token = "your_token_here"',       # placeholder prefix + below floor
    'secret = "example_secret_value"', # placeholder prefix
    'api_key = os.environ["API_KEY"]', # not a literal
    'path = "/etc/secrets"',           # low entropy, wrong name anyway
    "const maxRetries = 5",
    # keyword-as-prefix or keyword-inside-non-credential names —
    # the trailing word boundary must reject these
    'passwordless = "xJ9fK2mQ7vB4nR8tY3wE6uI1oP5aS"',
    'tokenizer = "xJ9fK2mQ7vB4nR8tY3wE6uI1oP5aS"',
    'token_expiry = "xJ9fK2mQ7vB4nR8tY3wE6uI1oP5aS"',
])
def test_clean_strings_untouched(snippet: str) -> None:
    out, n = redact(snippet)
    assert n == 0
    assert out == snippet


def test_multiple_secrets_counted() -> None:
    text = ('a = "AKIAIOSFODNN7EXAMPLE"\n'
            'b = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123"\n'
            'password = "xJ9fK2mQ7vB4nR8tY3wE6uI1oP5aS"')
    out, n = redact(text)
    assert n == 3
    assert "[REDACTED:aws-access-key]" in out
    assert "[REDACTED:anthropic-key]" in out
    assert "[REDACTED:generic-secret]" in out
    assert "EXAMPLE" not in out


def test_empty_and_none_safe() -> None:
    assert redact("") == ("", 0)


@pytest.mark.parametrize("snippet,kind", [
    ('password = "AKIAIOSFODNN7EXAMPLE"', "aws-access-key"),
    ('api_key = "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"',
     "github-token"),
    ('token = "xoxb-123456789012-abcdefghijkl"', "slack-token"),
])
def test_shaped_secret_in_assignment_counts_once(
    snippet: str, kind: str,
) -> None:
    # The credential-ish name would also fire the generic rule —
    # the shaped label must win and the count stay at 1.
    out, n = redact(snippet)
    assert n == 1
    assert f"[REDACTED:{kind}]" in out


def test_redact_is_idempotent() -> None:
    text = 'password = "xJ9fK2mQ7vB4nR8tY3wE6uI1oP5aS"'
    once, n = redact(text)
    twice, n2 = redact(once)
    assert n == 1 and n2 == 0
    assert twice == once


def test_entropy_gate_calibration() -> None:
    assert _entropy("xJ9fK2mQ7vB4nR8tY3wE6uI1oP5aS") > 3.3
    assert _entropy("changeme123") < 3.3
    assert _entropy("access_denied") < 3.3
