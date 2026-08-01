from pathlib import Path


def pytest_configure(config):
    """Keep temp dirs inside the repo.

    The default base (%TEMP%/pytest-of-<user>) is not reliably writable on
    Windows, and these tests write multi-megabyte USD bundles.
    """
    if config.option.basetemp is None:
        config.option.basetemp = str(Path(__file__).parent / ".pytest-tmp")
