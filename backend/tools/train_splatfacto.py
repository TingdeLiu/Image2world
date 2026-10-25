"""Nerfstudio entrypoint with safe Splatfacto optimizer binding on resume.

Nerfstudio 1.1.5 constructs optimizers before loading a checkpoint; Splatfacto's
loader replaces Gaussian Parameters, leaving those optimizers on old objects.
The compatibility hook is confined to this process and preserves original
config class names so standard ns-export and ns-eval can still read the YAML.
"""
from checkpoint_binding import rebind_optimizer_parameters


def main():
    from nerfstudio.engine.trainer import Trainer
    from nerfstudio.models.splatfacto import SplatfactoModel
    from nerfstudio.scripts.train import entrypoint

    original = Trainer._load_checkpoint

    def load_checkpoint(trainer):
        original(trainer)
        if (trainer.config.load_dir is not None or trainer.config.load_checkpoint is not None) and isinstance(trainer.pipeline.model, SplatfactoModel):
            count = rebind_optimizer_parameters(trainer.optimizers.optimizers, trainer.pipeline.get_param_groups())
            print(f"[resume] Rebound {count} optimizer parameter references; continuing from step {trainer._start_step}", flush=True)

    Trainer._load_checkpoint = load_checkpoint
    entrypoint()


if __name__ == "__main__":
    main()
