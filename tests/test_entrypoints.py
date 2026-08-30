"""
Entry-point wiring.

These are cheap structural checks for defects that were invisible in source
review and only showed up as "the app dies with no explanation":

  * launcher.py must call main.run(), not main.main().  run() installs the
    crash handlers; calling main() directly skipped them, which is why no
    released build ever produced a crash.log.
  * Crash logging must not sit behind `if __name__ == "__main__"`, because the
    frozen binary imports main as a module and that block never executes.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _module_level_calls(source: str) -> list[str]:
    """Names of functions called at module scope (not inside any def/class)."""
    tree = ast.parse(source)
    names: list[str] = []
    for node in tree.body:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                fn = sub.func
                if isinstance(fn, ast.Name):
                    names.append(fn.id)
                elif isinstance(fn, ast.Attribute):
                    names.append(fn.attr)
    return names


def test_launcher_invokes_run_not_main():
    source = (REPO / "launcher.py").read_text(encoding="utf-8-sig")
    called = _module_level_calls(source)
    assert "run" in called, "launcher.py must call main.run() so crash handlers install"
    assert "main" not in called, (
        "launcher.py calls main() directly — that bypasses the crash handlers "
        "installed by run(), which is how released builds ended up with no crash.log"
    )


def test_main_exposes_run():
    source = (REPO / "main.py").read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    top_level = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert "run" in top_level
    assert "_write_crash_log" in top_level
    assert "_install_thread_excepthook" in top_level


def test_crash_logging_is_not_gated_behind_dunder_main():
    """
    The original bug: everything below `if __name__ == "__main__"` is dead code
    in the frozen app, because launcher.py imports main as a module.
    """
    tree = ast.parse((REPO / "main.py").read_text(encoding="utf-8-sig"))
    guarded_src: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.If):
            test = node.test
            if (isinstance(test, ast.Compare)
                    and isinstance(test.left, ast.Name)
                    and test.left.id == "__name__"):
                for sub in node.body:
                    guarded_src.append(ast.dump(sub))
    joined = " ".join(guarded_src)
    assert "_write_crash_log" not in joined, (
        "crash logging is inside the __main__ guard and will never run in the frozen build"
    )


def test_run_installs_logging_before_calling_main():
    """Startup failures must be captured too, so logging comes first."""
    source = (REPO / "main.py").read_text(encoding="utf-8-sig")
    body = source[source.index("def run() -> int:"):]
    assert body.index("logging_setup") < body.index("return main()")
