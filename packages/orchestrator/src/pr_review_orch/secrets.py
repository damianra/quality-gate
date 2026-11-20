"""FR-O34 prompt-bound secret redaction — vendored regex corpus.

``redact()`` scrubs strings bound for an LLM provider before the
request is built. It is deliberately independent of the gitleaks
scanner pass (FR-O29): gitleaks findings carry one-way sha256
fingerprints only — never the matched text — so prompt redaction
needs its own detector at prompt-build time. The corpus is a small
auditable set of key-shape rules plus one entropy-gated assignment
rule for unshaped credentials; it is not a second gitleaks.

Two rule kinds:

- ``shape`` — high-signal token formats (AWS ``AKIA…``, GitHub
  ``ghp_…``, ``sk-ant-…``, Slack ``xox…-``, PEM blocks, JWTs).
  The whole match is replaced.
- ``generic-secret`` — ``<name> = "<value>"`` assignments where the
  name looks credential-ish and the value clears a length + Shannon
  entropy floor (``MIN_ENTROPY``) and is not an obvious placeholder.
  Only the quoted literal is replaced, preserving the code shape.

Replacements render as ``[REDACTED:<kind>]`` — the secret text is
never logged, stored or returned.
"""

from __future__ import annotations

import math
import re

#: Shannon entropy floor (bits/char) for the generic assignment rule.
#: Calibrated on common shapes: real keys/tokens score ~3.4+;
#: placeholders and natural-language strings score below ~3.3
#: (``changeme123`` 3.28, ``access_denied`` 2.87, ``/etc/secrets`` 2.52).
MIN_ENTROPY = 3.3

#: Minimum literal length the generic rule considers.
MIN_VALUE_LEN = 12

#: `<name> = "<value>"` / `<name>: "<value>"` in any text —
#: covers python/js/go assignments, JSON keys, YAML scalars and
#: env-style lines. Single and double quotes both accepted.
#: No leading word boundary on the name so compound identifiers
#: (``db_password``, ``accessToken``, ``AWS_SECRET_KEY``) still
#: match; the trailing ``s?\b`` keeps ``passwordless``/``tokenizer``
#: clean.
_ASSIGN_RE = re.compile(
    r"(password|passwd|pwd|secret|secret_?key|token|api_?key|"
    r"access_?key|auth_?token|private_?key|client_?secret|"
    r"credential)s?\b\s*[:=]\s*([\"'])((?:(?!\2).){%d,}?)\2"
    % MIN_VALUE_LEN,
    re.IGNORECASE,
)

#: Values that pass the entropy floor but are obviously not real
#: secrets — documentation placeholders, self-describing fakes and
#: our own ``[REDACTED:…]`` markers (a shaped secret inside a
#: credential-ish assignment must not re-fire the generic rule —
#: keeps the count at 1 and the pass idempotent).
_PLACEHOLDER_RE = re.compile(
    r"(?i)^(?:change|example|test|your|fake|dummy|sample|placeholder|"
    r"redacted|xxx+|<|\$\{|\*\*|\[\s*redacted)"
)

#: High-signal token shapes; the whole match is replaced. Ordered —
#: more specific families first so labels stay accurate (``sk-ant-``
#: would also match the generic ``sk-`` shape).
_SHAPE_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("private-key", re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |PGP |DSA |ENCRYPTED )?"
        r"PRIVATE KEY(?: BLOCK)?-----.*?-----END "
        r"(?:RSA |EC |OPENSSH |PGP |DSA |ENCRYPTED )?"
        r"PRIVATE KEY(?: BLOCK)?-----", re.DOTALL)),
    ("github-token", re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|"
        r"github_pat_[A-Za-z0-9_]{22,})\b")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("openai-key", re.compile(
        r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}\b")),
    ("aws-access-key", re.compile(
        r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA)[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("jwt", re.compile(
        r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."
        r"[A-Za-z0-9_-]{10,}\b")),
]


def _entropy(text: str) -> float:
    """Shannon entropy in bits per character."""
    if not text:
        return 0.0
    counts: dict[str, int] = {}
    for ch in text:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(text)
    return -sum((v / n) * math.log2(v / n) for v in counts.values())


def _redact_assignments(text: str) -> tuple[str, int]:
    """Apply the entropy-gated assignment rule; return (text, hits)."""
    hits = 0

    def repl(m: re.Match[str]) -> str:
        nonlocal hits
        value = m.group(3)
        if _PLACEHOLDER_RE.match(value) or _entropy(value) < MIN_ENTROPY:
            return m.group(0)
        hits += 1
        return m.group(0)[: m.start(3) - m.start(0)] \
            + "[REDACTED:generic-secret]" + m.group(2)

    return _ASSIGN_RE.sub(repl, text), hits


def redact(text: str) -> tuple[str, int]:
    """Replace detected secrets with ``[REDACTED:<kind>]`` markers.

    Returns ``(scrubbed_text, replacement_count)`` — the count feeds
    the ``llm_calls`` audit trail so a review run can prove the pass
    executed (a zero count is still evidence). Never returns the
    matched text.
    """
    if not text:
        return text, 0
    hits = 0
    for kind, rx in _SHAPE_RULES:
        def repl(m: re.Match[str], kind: str = kind) -> str:
            nonlocal hits
            hits += 1
            return f"[REDACTED:{kind}]"
        text = rx.sub(repl, text)
    text, n = _redact_assignments(text)
    return text, hits + n
