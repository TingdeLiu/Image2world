"""Repair optimizer references after a loader replaces model Parameters.

No optimizer or scheduler is recreated, and the restored Adam moments/step are
kept. This helper has no torch dependency so the ownership contract runs in CI.
"""
from __future__ import annotations


def remaining_training_steps(target: int, completed: int = 0) -> int:
    """Nerfstudio's max_num_iterations is additional work, not a total target."""
    if target < 1 or completed < 0:
        raise ValueError("Training steps must be non-negative with a positive target")
    return max(0, target - completed)


def rebind_optimizer_parameters(optimizers: dict, live_groups: dict) -> int:
    replacements = []
    for name, optimizer in optimizers.items():
        live = list(live_groups[name])
        old = [parameter for group in optimizer.param_groups for parameter in group["params"]]
        if len(old) != len(live):
            raise ValueError(f"Optimizer group {name} changed its parameter count")
        for previous, current in zip(old, live):
            state = optimizer.state.get(previous, {})
            for key, value in state.items():
                shape = getattr(value, "shape", ())
                if len(shape) and tuple(shape) != tuple(current.shape):
                    raise ValueError(f"Restored optimizer {name}.{key} does not match the live model")
            replacements.append((optimizer, previous, current, state))
    # Validate every group before changing any references.
    changed = 0
    for optimizer, previous, current, state in replacements:
        if previous is current:
            continue
        for group in optimizer.param_groups:
            group["params"] = [current if parameter is previous else parameter for parameter in group["params"]]
        optimizer.state.pop(previous, None)
        if state:
            optimizer.state[current] = state
        changed += 1
    return changed
