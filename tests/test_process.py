import stat
import sys
import time

import pytest

from g1_studio.config import Config
from g1_studio.sim.process import ManagedProcess, SimError, env_python_command, sim_command

FAKE_SIM = r'''
import signal, sys, time
print("sim: старт", sys.argv[1:], flush=True)
def bye(*_):
    print("sim: получил Ctrl+C, закрываюсь", flush=True)
    sys.exit(0)
signal.signal(signal.SIGINT, bye)
while True:
    time.sleep(0.05)
'''


@pytest.fixture
def fake_conda(tmp_path):
    """«conda», которая выполняет `conda run ... -n ENV python ARGS` нашим Python."""
    conda = tmp_path / "bin" / "conda"
    conda.parent.mkdir()
    conda.write_text(f'''#!/bin/sh
# conda run --no-capture-output -n ENV python ...
shift; shift; shift; shift; shift
exec {sys.executable} "$@"
''')
    conda.chmod(conda.stat().st_mode | stat.S_IEXEC)
    return conda


def test_sim_command_isaac(sim_repo, fake_conda):
    cfg = Config()
    cfg.sim.repo, cfg.sim.conda = str(sim_repo), str(fake_conda)
    cfg.sim.headless, cfg.sim.ball = True, False
    cmd, cwd, env = sim_command(cfg)
    assert cmd[:5] == [str(fake_conda), "run", "--no-capture-output", "-n", "unitree_sim"]
    assert "sim_main.py" in cmd and "--enable_cameras" in cmd and "--headless" in cmd
    assert cmd[cmd.index("--device") + 1] == "cpu"
    assert cwd == sim_repo and env["G1_BALL"] == "0" and env["G1_BALL_POS"] == "-2.7,-2.0"


def test_sim_command_errors(tmp_path, sim_repo):
    cfg = Config()
    cfg.sim.repo = str(tmp_path / "нет")
    with pytest.raises(SimError, match="sim_main.py"):
        sim_command(cfg)


def test_sim_command_bench():
    cfg = Config()
    cfg.sim.backend = "bench"
    cmd, _, _ = sim_command(cfg)
    assert cmd[:4] == [sys.executable, "-u", "-m", "g1_studio.bench"] and "--ball-pos=-2.7,-2.0" in cmd


def test_managed_process_graceful_stop(sim_repo, fake_conda, tmp_path):
    (sim_repo / "sim_main.py").write_text(FAKE_SIM, encoding="utf-8")
    cfg = Config()
    cfg.sim.repo, cfg.sim.conda = str(sim_repo), str(fake_conda)
    lines, exits = [], []
    p = ManagedProcess("сим", on_line=lines.append, on_exit=exits.append)
    cmd, cwd, env = sim_command(cfg)
    p.start(cmd, cwd, env)
    t0 = time.monotonic()
    while not lines and time.monotonic() - t0 < 10:
        time.sleep(0.05)
    assert p.running and "sim: старт" in lines[0] and "--enable_cameras" in lines[0]
    with pytest.raises(SimError):
        p.start(cmd, cwd, env)                       # второй раз не запускаем
    rc = p.stop(timeout=5)
    assert rc == 0 and not p.running
    time.sleep(0.2)
    assert any("Ctrl+C" in line for line in lines) and exits == [0]


def test_managed_process_kill_stubborn(tmp_path):
    script = tmp_path / "stubborn.py"
    script.write_text("import signal, time\nsignal.signal(signal.SIGINT, signal.SIG_IGN)\n"
                      "signal.signal(signal.SIGTERM, signal.SIG_IGN)\nprint('up', flush=True)\n"
                      "while True: time.sleep(0.1)\n")
    lines = []
    p = ManagedProcess("упрямый", on_line=lines.append)
    p.start([sys.executable, str(script)], tmp_path)
    while not lines:
        time.sleep(0.05)
    rc = p.stop(timeout=0.5)
    assert rc is not None and rc < 0 and not p.running   # добит SIGKILL


def test_env_python_command(fake_conda):
    cfg = Config()
    cfg.sim.conda = str(fake_conda)
    cmd = env_python_command(cfg, "x.py", "--out", "y.json")
    assert cmd[-5:] == ["python", "-u", "x.py", "--out", "y.json"] and cmd[1] == "run"
