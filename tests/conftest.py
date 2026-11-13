import json
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Настройки и логи — во временной папке, чтобы тесты не трогали ~/.config."""
    monkeypatch.setenv("G1_STUDIO_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("QWEN_API_KEY", raising=False)


class FakeLLM:
    """Мини-сервер, похожий на Ollama (/api/tags, /api/chat) и OpenAI API (/chat/completions)."""

    def __init__(self):
        self.plans: dict[str, dict] = {}
        self.models = ["qwen3:1.7b"]
        self.requests: list[dict] = []
        self.raw: str | None = None          # вернуть как есть (проверка «не JSON»)
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, obj, code=200):
                b = json.dumps(obj, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                self._send({"models": [{"name": m} for m in fake.models]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append({"path": self.path, "body": body, "headers": dict(self.headers)})
                user = [m["content"] for m in body["messages"] if m["role"] == "user"][0]
                content = fake.raw if fake.raw is not None else json.dumps(
                    fake.plans.get(user, {"reply": "не понял", "steps": []}), ensure_ascii=False)
                if self.path.endswith("/api/chat"):
                    self._send({"message": {"content": content}})
                else:
                    self._send({"choices": [{"message": {"content": content}}]})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def fake_llm():
    f = FakeLLM()
    yield f
    f.close()


class FakeLink:
    """Сим в одну строку: курс интегрирует присланную wz с задержкой ответа робота."""

    kind = "fake"
    domain = 1
    quat_order = "xyzw"

    def __init__(self, imu: bool = True, speed: float = 1.0):
        self.imu = imu
        self.speed = speed
        self.sent: list[tuple] = []
        self._yaw = 0.0
        self._wz = 0.0
        self._t = time.monotonic()
        self.x = 0.0

    def send(self, vx, vy, wz, h):
        now = time.monotonic()
        dt = (now - self._t) * self.speed
        self._t = now
        self._wz += (wz - self._wz) * min(dt / 0.1, 1.0)
        self._yaw += self._wz * dt
        self.x += vx * dt
        self.sent.append((vx, vy, wz, h))

    def yaw(self):
        return math.atan2(math.sin(self._yaw), math.cos(self._yaw)) if self.imu else None

    def state_age(self):
        return 0.0

    def state_rate(self):
        return 100.0


@pytest.fixture
def fake_link():
    return FakeLink()


TASK_FIXTURE = '''from isaaclab.utils import configclass
from isaaclab.scene import InteractiveSceneCfg


@configclass
class ObjectTableSceneCfg(InteractiveSceneCfg):
    """Сцена: стол, цилиндр, робот."""
    robot = None
    robot_camera = CameraPresets.g1_world_camera()
    packing_table = None


@configclass
class MoveCylinderG129Dex1WholebodyEnvCfg:
    scene = ObjectTableSceneCfg()
'''


@pytest.fixture
def sim_repo(tmp_path):
    """Минимальная копия unitree_sim_isaaclab с файлом задачи."""
    from g1_studio.sim.scene import TASK_FILE
    repo = tmp_path / "unitree_sim_isaaclab"
    task = repo / TASK_FILE
    task.parent.mkdir(parents=True)
    task.write_text(TASK_FIXTURE, encoding="utf-8")
    (repo / "sim_main.py").write_text("print('sim')\n")
    return repo
