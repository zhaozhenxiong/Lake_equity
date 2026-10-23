"""Document resolved Click defaults without evaluating runtime callbacks."""

import importlib.util
from pathlib import Path

import click
import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "sync_docs", ROOT / "scripts" / "dev" / "sync_docs.py"
)
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)


@pytest.mark.parametrize("default", [None, True, False])
def test_boolean_default_matches_runtime(default):
    kwargs = {} if default is None else {"default": default}
    option = click.Option(["--full"], is_flag=True, **kwargs)
    command = click.Command("audit", params=[option])
    with command.make_context("audit", []) as ctx:
        assert generator._default(option, ctx) == str(ctx.params["full"])


def test_callable_default_is_not_evaluated():
    def runtime_default():
        pytest.fail("Documentation must not evaluate runtime defaults")

    option = click.Option(["--date"], default=runtime_default)
    ctx = click.Context(click.Command("daily", params=[option]))
    assert generator._default(option, ctx) == "运行时计算"


def test_reference_matches_registered_commands():
    assert generator.render_cli_options() == (ROOT / "docs/zh/reference/cli-options.md").read_text(
        encoding="utf-8"
    )
