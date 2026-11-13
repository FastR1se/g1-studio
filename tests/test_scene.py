import json

import pytest

from g1_studio.sim import scene


def test_ball_on_off(sim_repo):
    path = sim_repo / scene.TASK_FILE
    original = path.read_text(encoding="utf-8")
    assert scene.set_ball(sim_repo, True)
    text = path.read_text(encoding="utf-8")
    assert "_g1_ball_cfg()" in text and "G1_BALL_POS" in text
    compile(text, "x", "exec")
    assert (path.parent / (path.name + ".bak")).is_file()
    assert not scene.set_ball(sim_repo, True)                 # повторно — без изменений
    assert scene.status(sim_repo).ball
    assert scene.set_ball(sim_repo, False)
    assert path.read_text(encoding="utf-8").strip() == original.strip()


def test_objects_and_ball_independent(sim_repo):
    data = {"objects": [{"label": "person", "usd": "x.usd", "pos": [0, 0, 0]},
                        {"label": "chair", "usd": "y.usd", "pos": [1, 0, 0]}]}
    (sim_repo / scene.OBJECTS_JSON).write_text(json.dumps(data), encoding="utf-8")
    assert scene.set_objects(sim_repo, True)
    assert scene.set_ball(sim_repo, True)
    st = scene.status(sim_repo)
    assert st.ball and st.objects and st.objects_list == ["person", "chair"]
    text = (sim_repo / scene.TASK_FILE).read_text(encoding="utf-8")
    assert text.count("# G1_OBJECTS") == scene.MAX_OBJECTS
    compile(text, "x", "exec")
    scene.set_objects(sim_repo, False)
    st = scene.status(sim_repo)
    assert st.ball and not st.objects
    assert "_g1_obj" not in (sim_repo / scene.TASK_FILE).read_text(encoding="utf-8")


def test_changed_task_file_is_refused(sim_repo):
    (sim_repo / scene.TASK_FILE).write_text("class Something:\n    pass\n", encoding="utf-8")
    with pytest.raises(scene.SceneError, match="ObjectTableSceneCfg"):
        scene.set_ball(sim_repo, True)


def test_missing_repo(tmp_path):
    st = scene.status(tmp_path / "нет")
    assert st.error and st.describe()[0].startswith("✗")
    with pytest.raises(scene.SceneError):
        scene.set_ball(tmp_path / "нет", True)


def test_isaac_script_shipped():
    assert scene.find_objects_script().is_file()
