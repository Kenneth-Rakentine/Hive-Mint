"""Only one shift or `run` works at a time."""

import subprocess
import sys
import textwrap

from tools.single_instance import lock_path, single_instance


def test_a_second_worker_is_turned_away_and_the_lock_frees_on_exit(tmp_path):
    path = lock_path(f"sqlite:///{(tmp_path / 'hivemint.db').as_posix()}")
    assert path == tmp_path / "worker.lock"
    probe = textwrap.dedent(f"""
        from pathlib import Path
        from tools.single_instance import single_instance
        with single_instance(Path({str(path)!r})) as mine:
            print(mine)
    """)
    with single_instance(path) as mine:
        assert mine
        other = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
        assert other.stdout.strip() == "False"
    after = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    assert after.stdout.strip() == "True"


def test_cli_shift_refuses_while_another_worker_holds_the_lock(tmp_path, monkeypatch, capsys):
    from app import cli
    from config.settings import get_settings

    monkeypatch.setenv("HIVEMINT_DB_URL", f"sqlite:///{(tmp_path / 'hivemint.db').as_posix()}")
    get_settings.cache_clear()
    try:
        with single_instance(lock_path(get_settings().db_url)):
            assert cli.main(["shift"]) == 1
    finally:
        get_settings.cache_clear()
    assert "already working" in capsys.readouterr().out
