import math
import time

import cv2
import numpy as np
import pytest

from g1_studio.vision import geometry as g
from g1_studio.vision.detect import BallDetector, YoloDetector, annotate
from g1_studio.vision.stream import CameraStream, find_model, load_yolo


def floor_image():
    img = np.full((g.IMG_H, g.IMG_W, 3), (110, 110, 105), np.uint8)
    img[: g.IMG_H // 5] = (180, 180, 185)                    # «стена» сверху
    return img


def draw_ball(img, x, y):
    """Нарисовать мяч, стоящий на полу в точке (x, y) торса, как его видит камера."""
    centre = (x, y, -g.TORSO_HEIGHT + g.BALL_RADIUS)
    u, v = g.project_torso(centre)
    d3 = math.dist(centre, g.CAM_OFFSET)
    cos_t = 1.0 / math.sqrt(1 + ((u - g.CX) / g.FX) ** 2 + ((v - g.CY) / g.FY) ** 2)
    r = g.FX / (math.sqrt((d3 / g.BALL_RADIUS) ** 2 - 1) * cos_t)
    cv2.circle(img, (round(u), round(v)), round(r), (20, 20, 220), -1, cv2.LINE_AA)
    return u, v


@pytest.mark.parametrize("x,y", [(0.6, 0.0), (1.0, 0.3), (1.5, -0.4), (2.2, 0.5)])
def test_geometry_roundtrip(x, y):
    u, v = g.project_torso((x, y, -g.TORSO_HEIGHT))
    bearing, dist = g.ground_locate(u, v)
    assert dist == pytest.approx(math.hypot(x, y), rel=1e-6)
    assert bearing == pytest.approx(math.degrees(math.atan2(y, x)), abs=1e-6)


def test_above_horizon_is_infinite():
    assert g.ground_locate(g.CX, 0)[1] == float("inf") or g.ground_locate(g.CX, 0)[1] > 10


@pytest.mark.parametrize("x,y", [(0.8, 0.1), (1.2, 0.5), (1.6, -0.3), (2.0, 0.0)])
def test_ball_distance(x, y):
    img = floor_image()
    draw_ball(img, x, y)
    ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])   # как в симе
    det = BallDetector().detect(cv2.imdecode(jpg, cv2.IMREAD_COLOR))
    assert det is not None
    assert det.distance_m == pytest.approx(math.hypot(x, y), rel=0.1)
    assert det.bearing_deg == pytest.approx(math.degrees(math.atan2(y, x)), abs=3)
    assert "мяч" in det.describe()


def test_no_ball_on_red_stripe_at_edge():
    img = floor_image()
    cv2.rectangle(img, (0, 300), (639, 306), (20, 20, 220), -1)   # длинная красная полоса через кадр
    assert BallDetector().detect(img) is None


def test_annotate_keeps_size():
    img = floor_image()
    draw_ball(img, 1.0, 0.0)
    det = BallDetector().detect(img)
    out = annotate(img, det, [])
    assert out.shape == img.shape and not np.array_equal(out, img)


def test_yolo_raw_decode():
    """Классический экспорт (1, 4+классы, N) — с NMS на нашей стороне."""
    y = YoloDetector.__new__(YoloDetector)
    y.conf = 0.3
    out = np.zeros((1, 84, 3), np.float32)
    out[0, :4, 0] = [100, 100, 40, 80]       # x, y, w, h
    out[0, 4 + 0, 0] = 0.9                   # person
    out[0, :4, 1] = [102, 101, 40, 80]       # дубль — уберёт NMS
    out[0, 4 + 0, 1] = 0.8
    out[0, :4, 2] = [300, 200, 20, 20]
    out[0, 4 + 56, 2] = 0.1                  # стул, но ниже порога
    rows = y._decode(out)
    assert len(rows) == 1 and int(rows[0][5]) == 0


@pytest.mark.skipif(find_model() is None, reason="нет yolo26*.onnx в models/")
def test_yolo_model_runs():
    pytest.importorskip("onnxruntime")
    yolo, msg = load_yolo()
    assert yolo is not None, msg
    assert len(yolo.names) == 80
    assert yolo.detect(floor_image()) == []


def test_load_yolo_missing():
    yolo, msg = load_yolo("нет_такой.onnx")
    assert yolo is None and "нет модели" in msg


def test_camera_stream_over_zmq():
    zmq = pytest.importorskip("zmq")
    pub = zmq.Context.instance().socket(zmq.PUB)
    port = pub.bind_to_random_port("tcp://127.0.0.1")
    cam = CameraStream("127.0.0.1", port)
    try:
        img = floor_image()
        draw_ball(img, 1.2, 0.2)
        jpg = cv2.imencode(".jpg", img)[1].tobytes()
        t0 = time.monotonic()
        while not cam.alive() and time.monotonic() - t0 < 5:
            pub.send(jpg)                    # PUB/SUB: первые кадры могут уйти до подписки
            time.sleep(0.05)
        assert cam.alive()
        ball = cam.find("ball")
        assert ball is not None and ball.distance_m == pytest.approx(math.hypot(1.2, 0.2), rel=0.1)
        assert cam.visible()[0].label == "ball"
        assert cam.find("person") is None
    finally:
        cam.close()
        pub.close(0)
