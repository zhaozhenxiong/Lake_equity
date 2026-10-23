import subprocess
import sys

import pytest

from cnequity import keep_awake as mod


def test_the_opt_out_holds_nothing(monkeypatch):
    monkeypatch.setenv(mod.ALLOW_SLEEP_ENV, "1")
    with mod.keep_awake() as held:
        assert held is False


def test_macos_ties_caffeinate_to_this_process_and_releases_it(monkeypatch):
    monkeypatch.delenv(mod.ALLOW_SLEEP_ENV, raising=False)
    monkeypatch.setattr(mod.sys, "platform", "darwin")
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/caffeinate")
    spawned: list = []

    class _Proc:
        terminated = False

        def __init__(self, argv, **_kw):
            self.argv = argv
            spawned.append(self)

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(mod.subprocess, "Popen", _Proc)
    with mod.keep_awake() as held:
        assert held is True
        assert spawned[0].argv[1:3] == ["-i", "-w"]
    assert spawned[0].terminated


def test_a_missing_caffeinate_is_not_an_error(monkeypatch):
    monkeypatch.delenv(mod.ALLOW_SLEEP_ENV, raising=False)
    monkeypatch.setattr(mod.sys, "platform", "darwin")
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    with mod.keep_awake() as held:
        assert held is False


def test_linux_does_nothing(monkeypatch):
    monkeypatch.delenv(mod.ALLOW_SLEEP_ENV, raising=False)
    monkeypatch.setattr(mod.sys, "platform", "linux")
    with mod.keep_awake() as held:
        assert held is False


@pytest.mark.skipif(sys.platform != "darwin", reason="real caffeinate")
def test_real_caffeinate_holds_and_lets_go(monkeypatch):
    monkeypatch.delenv(mod.ALLOW_SLEEP_ENV, raising=False)
    with mod.keep_awake() as held:
        assert held is True
        # Other processes' assertion names are not guaranteed to be UTF-8.
        out = subprocess.run(
            ["pmset", "-g", "assertions"], capture_output=True, text=True, errors="replace"
        )
        assert "caffeinate" in out.stdout
