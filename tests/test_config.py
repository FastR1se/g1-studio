from g1_studio.config import Config, restart_needed


def test_created_with_defaults(tmp_path):
    cfg = Config.load()
    assert cfg.path.is_file()
    assert cfg.sim.conda_env == "unitree_sim" and cfg.dds.domain == 1
    text = cfg.path.read_text(encoding="utf-8")
    assert "[qwen]" in text and "# Модель" in text          # с комментариями для правки руками


def test_roundtrip():
    cfg = Config.load()
    cfg.qwen.model = 'qwen3:4b "q"'
    cfg.robot.sim_speed = 0.6
    cfg.sim.headless = True
    cfg.save()
    again = Config.load()
    assert again.qwen.model == 'qwen3:4b "q"'
    assert again.robot.sim_speed == 0.6 and again.sim.headless is True
    assert not cfg.diff(again)


def test_bad_values_fall_back(tmp_path, caplog):
    p = tmp_path / "c.toml"
    p.write_text('[dds]\ndomain = "один"\nquat_order = "zyx"\n[yolo]\nconf = 5.0\nthreads = 2\n'
                 '[nope]\nx = 1\n[ui]\nunknown = 1\n', encoding="utf-8")
    cfg = Config.load(p)
    assert cfg.dds.domain == 1 and cfg.dds.quat_order == "xyzw"
    assert cfg.yolo.conf == 0.35 and cfg.yolo.threads == 2
    assert "неизвестный раздел" in caplog.text and "неизвестное поле" in caplog.text


def test_int_accepted_for_float(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[robot]\nsim_speed = 1\n", encoding="utf-8")
    assert Config.load(p).robot.sim_speed == 1.0


def test_broken_toml(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[sim\n", encoding="utf-8")
    assert Config.load(p).sim.backend == "isaac"


def test_restart_needed():
    a = Config()
    b = a.copy()
    b.sim.device = "cuda"
    b.qwen.model = "qwen3:4b"
    assert restart_needed(a.diff(b)) == {"sim"}
    b.dds.domain = 0
    assert restart_needed(a.diff(b)) == {"sim", "app"}


def test_api_key_from_env(monkeypatch):
    cfg = Config()
    monkeypatch.setenv("QWEN_API_KEY", "k")
    assert cfg.api_key() == "k"
    cfg.qwen.api_key = "file"
    assert cfg.api_key() == "file"
