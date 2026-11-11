"""Unit tests for blast-radius scoping (scope.py). Pure filesystem, no net."""

from __future__ import annotations

from pathlib import Path

from pr_review_orch import config, scope


def _write(repo: Path, rel: str, content: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_find_dependents_matches_imports(tmp_path: Path) -> None:
    _write(tmp_path, "src/utils/math.ts", "export const add = () => 1\n")
    _write(tmp_path, "src/app/calc.ts", 'import { add } from "../utils/math";\n')
    _write(tmp_path, "src/app/unrelated.ts", 'import x from "lodash";\n')
    _write(tmp_path, "node_modules/pkg/index.ts", 'import "../utils/math";\n')

    deps = scope.find_dependents(str(tmp_path), ["src/utils/math.ts"])
    assert deps == ["src/app/calc.ts"]  # node_modules skipped, unrelated excluded


def test_find_dependents_index_resolution(tmp_path: Path) -> None:
    _write(tmp_path, "pkg/auth/index.ts", "export {};\n")
    _write(tmp_path, "app/main.ts", 'import auth from "pkg/auth";\n')

    deps = scope.find_dependents(str(tmp_path), ["pkg/auth/index.ts"])
    assert deps == ["app/main.ts"]


def test_find_dependents_multilanguage(tmp_path: Path) -> None:
    """FR-L6: go/rs/java/rb/php/cs dependents resolve to changed stems."""
    _write(tmp_path, "pkg/engine.go", "package engine\n")
    _write(tmp_path, "cmd/main.go", 'import (\n\t"github.com/me/app/pkg"\n)\n')
    _write(tmp_path, "src/engine.rs", "pub fn run() {}\n")
    _write(tmp_path, "src/main.rs", "use crate::engine;\nfn main() {}\n")
    _write(tmp_path, "com/app/Engine.java", "class Engine {}\n")
    _write(tmp_path, "com/app/Main.java", "import com.app.Engine;\n")
    _write(tmp_path, "lib/engine.rb", "class Engine; end\n")
    _write(tmp_path, "lib/app.rb", "require 'engine'\n")
    _write(tmp_path, "src/Engine.php", "class Engine {}\n")
    _write(tmp_path, "src/Boot.php", "use App\\Engine;\n")
    _write(tmp_path, "Core/Engine.cs", "class Engine {}\n")
    _write(tmp_path, "Core/Program.cs", "using Core.Engine;\n")

    changed = [
        "pkg/engine.go", "src/engine.rs", "com/app/Engine.java",
        "lib/engine.rb", "src/Engine.php", "Core/Engine.cs",
    ]
    deps = set(scope.find_dependents(str(tmp_path), changed))
    assert {
        "cmd/main.go", "src/main.rs", "com/app/Main.java",
        "lib/app.rb", "src/Boot.php", "Core/Program.cs",
    } <= deps


def test_context_includes_changed_and_related(tmp_path: Path) -> None:
    _write(tmp_path, "a.ts", "const a = 1;\n")
    _write(tmp_path, "b.ts", 'import "./a";\n')
    files = [{"filename": "a.ts", "status": "modified", "additions": 1,
              "deletions": 0, "changes": 1}]
    ctx = scope.build_pr_context(str(tmp_path), files, "diff --git a/a.ts b/a.ts\n+const a = 1;\n")

    assert ctx["changed_files"] == ["a.ts"]
    assert ctx["related_files"] == ["b.ts"]
    assert ctx["file_contents"]["a.ts"] == "const a = 1;\n"
    assert "diff --git" in ctx["diff_text"]
    assert ctx["truncated"] is False


def test_context_respects_char_budget(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(config, "SCOPE_MAX_CHARS", 100)
    _write(tmp_path, "big.ts", "x" * 500)
    files = [{"filename": "big.ts", "status": "modified", "additions": 500,
              "deletions": 0, "changes": 500}]
    ctx = scope.build_pr_context(str(tmp_path), files, "d" * 500)

    # diff takes half the budget; file content gets the remainder at most
    assert len(ctx["diff_text"]) == 50
    assert len(ctx["file_contents"].get("big.ts", "")) <= 50
    assert ctx["truncated"] is True


def test_context_respects_file_cap(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(config, "SCOPE_MAX_FILES", 1)
    for i in range(3):
        _write(tmp_path, f"f{i}.ts", f"const x{i} = 0;\n")
    files = [{"filename": f"f{i}.ts", "status": "modified", "additions": 1,
              "deletions": 0, "changes": 1} for i in range(3)]
    ctx = scope.build_pr_context(str(tmp_path), files, "")

    assert len(ctx["file_contents"]) == 1
    assert ctx["truncated"] is True


def test_removed_files_have_no_content(tmp_path: Path) -> None:
    files = [{"filename": "gone.ts", "status": "removed", "additions": 0,
              "deletions": 5, "changes": 5}]
    ctx = scope.build_pr_context(str(tmp_path), files, "")
    assert "gone.ts" not in ctx["file_contents"]


def test_fr_c2_agents_doc_collected(tmp_path: Path) -> None:
    _write(tmp_path, "AGENTS.md", "# rules\nenglish only\n")
    ctx = scope.build_pr_context(str(tmp_path), [], "")
    assert ctx["agents_doc"] == "# rules\nenglish only\n"


def test_fr_c2_agents_doc_prefers_agents_over_claude(tmp_path: Path) -> None:
    _write(tmp_path, "AGENTS.md", "agents\n")
    _write(tmp_path, "CLAUDE.md", "claude\n")
    ctx = scope.build_pr_context(str(tmp_path), [], "")
    assert ctx["agents_doc"] == "agents\n"


def test_fr_c2_agents_doc_absent(tmp_path: Path) -> None:
    ctx = scope.build_pr_context(str(tmp_path), [], "")
    assert ctx["agents_doc"] is None


def test_fr_c3_related_heads_excerpted(tmp_path: Path) -> None:
    _write(tmp_path, "a.ts", "export const a = 1;\n")
    _write(tmp_path, "b.ts", 'import "./a";\nexport const b = a;\n')
    files = [{"filename": "a.ts", "status": "modified", "additions": 1,
              "deletions": 0, "changes": 1}]
    ctx = scope.build_pr_context(str(tmp_path), files, "")
    assert ctx["related_heads"] == [
        {"path": "b.ts", "head": 'import "./a";\nexport const b = a;\n'}]


def test_fr_c3_related_head_capped(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(config, "RELATED_HEAD_CHARS", 10)
    _write(tmp_path, "a.ts", "export const a = 1;\n")
    _write(tmp_path, "b.ts", 'import "./a";\n' + "x" * 500)
    files = [{"filename": "a.ts", "status": "modified", "additions": 1,
              "deletions": 0, "changes": 1}]
    ctx = scope.build_pr_context(str(tmp_path), files, "")
    assert len(ctx["related_heads"][0]["head"]) == 10


def test_fr_c3_related_head_file_cap(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(config, "RELATED_HEAD_FILES", 1)
    _write(tmp_path, "a.ts", "export const a = 1;\n")
    _write(tmp_path, "b.ts", 'import "./a";\n')
    _write(tmp_path, "c.ts", 'import "./a";\n')
    files = [{"filename": "a.ts", "status": "modified", "additions": 1,
              "deletions": 0, "changes": 1}]
    ctx = scope.build_pr_context(str(tmp_path), files, "")
    assert len(ctx["related_files"]) == 2
    assert len(ctx["related_heads"]) == 1
