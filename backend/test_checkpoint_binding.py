import numpy as np
import pytest

from backend.tools.checkpoint_binding import rebind_optimizer_parameters, remaining_training_steps


class Parameter:
    def __init__(self, shape):
        self.shape = shape


class Optimizer:
    def __init__(self, parameter, moment):
        self.param_groups = [{"params": [parameter], "lr": 0.0001}]
        self.state = {parameter: {"exp_avg": moment, "step": np.asarray(2000)}}


def test_checkpoint_replacement_keeps_restored_moments_and_updates_the_live_parameter():
    before, after = Parameter((2, 3)), Parameter((5, 3))
    moments = np.full((5, 3), 7.0)
    optimizer = Optimizer(before, moments)
    assert rebind_optimizer_parameters({"means": optimizer}, {"means": [after]}) == 1
    assert optimizer.param_groups[0]["params"][0] is after
    assert before not in optimizer.state
    assert optimizer.state[after]["exp_avg"] is moments
    assert optimizer.state[after]["step"] == 2000
    assert optimizer.param_groups[0]["lr"] == 0.0001


def test_mismatched_checkpoint_state_is_refused_before_training_or_partial_rebinding():
    old, live = Parameter((2, 3)), Parameter((5, 3))
    optimizer = Optimizer(old, np.ones((4, 3)))
    with pytest.raises(ValueError, match="does not match"):
        rebind_optimizer_parameters({"means": optimizer}, {"means": [live]})
    assert optimizer.param_groups[0]["params"][0] is old


def test_fixed_upstream_loader_already_bound_to_the_live_model_needs_no_replacement():
    parameter = Parameter((5, 3))
    optimizer = Optimizer(parameter, np.ones((5, 3)))
    assert rebind_optimizer_parameters({"means": optimizer}, {"means": [parameter]}) == 0


def test_refinement_adds_only_the_steps_missing_from_the_total_budget():
    assert remaining_training_steps(2000) == 2000
    assert remaining_training_steps(20_000, 2000) == 18_000
    assert remaining_training_steps(2500, 2000) == 500
    assert remaining_training_steps(2500, 2500) == 0
    assert remaining_training_steps(2500, 4500) == 0
