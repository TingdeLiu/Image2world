"""Model weights have to be releasable, and every model has to be registered.

`server.py` loads each model into a module global and keeps it forever, which is
fine until a subprocess needs the GPU.  `unload_models()` is what frees them, and
it can only free what `MODEL_GLOBALS` lists -- so the risk is not that unloading
breaks, it is that someone adds a ninth model and forgets to add it to the tuple.
The first test reads `server.py` as source rather than importing it, so it runs
without torch, CUDA, or any of the checkpoints installed.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

from backend.reconstruction_jobs import ReconstructionJobManager


SERVER_PATH = Path(__file__).resolve().parent / "server.py"


def _parse_server() -> ast.Module:
    return ast.parse(SERVER_PATH.read_text(encoding="utf-8"))


def _registered_models(tree: ast.Module) -> set[str]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "MODEL_GLOBALS" for t in node.targets):
            continue
        return {
            element.value
            for element in ast.walk(node.value)
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        }
    raise AssertionError("server.py no longer defines MODEL_GLOBALS")


def _lazily_loaded_globals(tree: ast.Module) -> set[str]:
    """Names a function declares `global` and then assigns -- the lazy-load shape."""
    declared: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        names = {
            name
            for statement in ast.walk(node)
            if isinstance(statement, ast.Global)
            for name in statement.names
        }
        assigned = {
            target.id
            for statement in ast.walk(node)
            if isinstance(statement, ast.Assign)
            for target in statement.targets
            if isinstance(target, ast.Name)
        }
        declared |= names & assigned
    return declared


def test_every_lazily_loaded_model_is_registered_for_unloading():
    tree = _parse_server()
    missing = _lazily_loaded_globals(tree) - _registered_models(tree)
    assert not missing, (
        f"{sorted(missing)} are loaded into module globals but never released; "
        "add them to MODEL_GLOBALS in server.py"
    )


def test_registry_only_lists_globals_that_exist():
    tree = _parse_server()
    module_level = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    stale = _registered_models(tree) - module_level
    assert not stale, f"MODEL_GLOBALS names {sorted(stale)}, which server.py does not define"


class _FakeProcess:
    """Just enough of Popen for the worker to read a log and give up."""

    def __init__(self, *_args, **_kwargs):
        self.stdout = iter(["Step 1 (0.00%)\n"])

    def wait(self) -> int:
        return 1


def test_reconstruction_releases_model_weights_before_training(tmp_path, monkeypatch):
    # `ns-train` runs in its own process and cannot reuse the VRAM this one
    # holds, so the weights have to be gone before it starts -- not after.
    from backend import reconstruction_jobs

    events: list[str] = []
    monkeypatch.setattr(reconstruction_jobs, "release_gpu_models", lambda: events.append("released"))
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: (events.append("spawned"), _FakeProcess())[1],
    )

    manager = ReconstructionJobManager(tmp_path)
    state, source = manager.create(
        name="Clip",
        quality="preview",
        filename="clip.mp4",
        content_type="video/mp4",
        camera_height=None,
    )
    source.write_bytes(b"stand-in for a video the fake process never reads")

    manager._run_job(state["id"], resume=False)

    assert events[:2] == ["released", "spawned"]
