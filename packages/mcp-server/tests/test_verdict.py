"""Verdict comment format + idempotency marker tests."""

from quality_gate_mcp.tools import (
    MARKER_RE,
    build_verdict_comment,
    find_verdict_comment,
    pr_review_status,
)

HEAD = "a1b2c3d4e5f67890"


def test_verdict_comment_contract():
    body = build_verdict_comment(
        approve=True,
        checks=[
            {"name": "Sin secretos", "passed": True, "detail": "clean"},
            {"name": "Scope", "passed": False, "detail": "2 files outside"},
        ],
        result="2 findings",
        note="automated review",
        head_sha=HEAD,
    )
    assert body.startswith("## Validación funcional")
    assert "✅ Aprobado" in body
    assert "- ✅ **Sin secretos** — clean" in body
    assert "- ❌ **Scope** — 2 files outside" in body
    assert "**Result:** 2 findings" in body
    assert "**Note:** automated review" in body
    assert f"<!-- pr-review head:{HEAD[:7]} -->" in body


def test_verdict_comment_rejected():
    body = build_verdict_comment(
        approve=False, checks=[], result="", note="", head_sha=HEAD
    )
    assert "❌ Cambios requeridos" in body
    assert "**Result:**" not in body
    assert "**Note:**" not in body


def test_marker_regex_matches_sha7():
    body = build_verdict_comment(True, [], "", "", HEAD)
    m = MARKER_RE.search(body)
    assert m and m.group("sha") == HEAD[:7]


def test_find_verdict_comment_picks_latest():
    comments = [
        {"id": 1, "body": "random comment"},
        {"id": 2, "body": "old <!-- pr-review head:1111111 -->"},
        {"id": 3, "body": "unrelated"},
        {"id": 4, "body": "new <!-- pr-review head:2222222 -->"},
    ]
    found = find_verdict_comment(comments)
    assert found and found["id"] == 4


def test_pr_review_status_pending_vs_reviewed():
    reviewed = [{"body": f"## Validación funcional\n<!-- pr-review head:{HEAD[:7]} -->"}]
    stale = [{"body": "## Validación funcional\n<!-- pr-review head:0000000 -->"}]
    assert pr_review_status(reviewed, HEAD) == "reviewed"
    assert pr_review_status(stale, HEAD) == "pending"
    assert pr_review_status([], HEAD) == "pending"
