#!/usr/bin/env bash
# =============================================================================
# install_unitree_sim.sh — установка unitree_sim_isaaclab без конфликтов
#
# Почему оригинальный auto_setup_env.sh ломается:
#   * Клонирует Isaac Lab с ветки main БЕЗ фиксации версии. Main сейчас —
#     Isaac Lab 3.x под Isaac Sim 6 и Python 3.12; Isaac Sim 5.1 там не
#     поддерживается -> конфликт всего со всем.
#   * pip качает в кэш ~/.cache/pip десятки ГБ и не чистит их -> диск забит.
#   * fetch_assets.sh запускается через sudo -> файлы root, дальше ошибки прав.
#   * Ничего не мешает requirements.txt перезаписать torch/numpy/psutil/cryptography.
#   * Свежий setuptools (82+) без pkg_resources -> flatdict, а с ним Isaac Lab,
#     не ставится, но isaaclab.sh об этом молчит.
#   * При сбое приходится начинать с нуля.
#
# Что делает этот скрипт:
#   * Фиксирует связку: Isaac Sim 5.1.0 + Isaac Lab v2.3.2 + Python 3.11
#     + PyTorch 2.7.0 (CUDA 12.8) + numpy<2
#   * Защищает её constraints-файлом: никакой pip install её не перепишет
#   * Ставит без кэша pip, временные файлы держит на диске, в конце чистит
#   * Проверяет железо, драйвер и место ДО скачивания гигабайтов
#   * Можно перезапускать: готовые шаги пропускаются
#   * Пишет полный лог в $ROOT/install.log
#
# Использование:
#   bash install_unitree_sim.sh
#
# Настройки (через переменные окружения):
#   ROOT=~/unitree_ws        куда ставить всё (по умолчанию ~/unitree_ws)
#   ENV_NAME=unitree_sim     имя conda-окружения
#   ISAACSIM_VER=5.1.0       5.1.0 (по умолчанию) или 5.0.0 (для RTX 50xx по совету Unitree)
#   SMOKE_TEST=0             пропустить пробный запуск Isaac Sim в конце
#   MIN_FREE_GB=60           сколько свободного места требовать
#
# Пример:  ROOT=/data/robots bash install_unitree_sim.sh
# =============================================================================
set -Eeo pipefail
USER="${USER:-$(id -un)}"

# ---------------------------- настройки --------------------------------------
ROOT="${ROOT:-$HOME/unitree_ws}"
ENV_NAME="${ENV_NAME:-unitree_sim}"
ISAACSIM_VER="${ISAACSIM_VER:-5.1.0}"
SMOKE_TEST="${SMOKE_TEST:-1}"
MIN_FREE_GB="${MIN_FREE_GB:-60}"

PY_VER="3.11"
TORCH_VER="2.7.0"
TORCHVISION_VER="0.22.0"
TORCHAUDIO_VER="2.7.0"
TORCH_INDEX="https://download.pytorch.org/whl/cu128"   # cu128 нужен и для RTX 50xx
NVIDIA_INDEX="https://pypi.nvidia.com"
CYCLONEDDS_BRANCH="releases/0.10.x"                    # как требует unitree_sdk2_python
MIN_DRIVER="580.65.06"                                 # драйвер, на котором тестировали Isaac Sim 5.x

case "$ISAACSIM_VER" in
  5.1.0) ISAACLAB_REF="${ISAACLAB_REF:-v2.3.2}" ;;     # последний релиз Isaac Lab под Isaac Sim 5.1
  5.0.0) ISAACLAB_REF="${ISAACLAB_REF:-v2.2.0}" ;;     # версия, которую Unitree ставит под 5.0
  *) echo "Поддерживаются только ISAACSIM_VER=5.1.0 или 5.0.0"; exit 1 ;;
esac

REPO="$ROOT/unitree_sim_isaaclab"
STATE="$ROOT/.install_state"
CONSTRAINTS="$ROOT/constraints.txt"
BUILD_CONSTRAINTS="$ROOT/build-constraints.txt"
LOG="$ROOT/install.log"

# ---------------------------- оформление -------------------------------------
G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; B=$'\e[1m'; N=$'\e[0m'
info() { echo -e "${B}▶ $*${N}"; }
ok()   { echo -e "  ${G}✓${N} $*"; }
warn() { echo -e "  ${Y}!${N} $*"; }
die()  { echo -e "\n${R}${B}✗ $*${N}\n"; exit 1; }

on_error() {
  echo -e "\n${R}${B}✗ Упало на строке $1: $2${N}"
  echo -e "  Полный лог: $LOG"
  echo -e "  Исправь причину и просто запусти скрипт ещё раз — готовые шаги пропустятся.\n"
}
trap 'on_error $LINENO "$BASH_COMMAND"' ERR

# Шаг выполняется один раз; после успеха ставится метка
step() {
  local name="$1"; shift
  if [[ -f "$STATE/$name.done" ]]; then
    ok "$name — уже сделано"
    return 0
  fi
  info "$name"
  "$@"
  touch "$STATE/$name.done"
  ok "$name — готово"
}

# Важно: нигде не используем `| head -1` — под pipefail он ловит SIGPIPE (код 141)
# и роняет скрипт. Вместо него sed -n 1p / awk, они дочитывают вход до конца.
version_ge() { [[ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | sed -n 1p)" == "$2" ]]; }

# Есть ли conda-окружение с таким именем (без grep -q — та же история с SIGPIPE)
env_exists() { conda env list | awk -v e="$1" '$1==e {f=1} END {exit !f}'; }

glibc_version() {
  local v
  v=$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}') || true
  [[ -n "$v" ]] || v=$(ldd --version 2>/dev/null | awk 'NR==1 {print $NF}') || true
  [[ -n "$v" ]] || v=$(python3 -c 'import platform;print(platform.libc_ver()[1])' 2>/dev/null) || true
  echo "$v"
}

pipi() { python -m pip install --no-cache-dir "$@"; }

# ---------------------------- шаги -------------------------------------------
preflight() {
  [[ $EUID -ne 0 ]] || die "Не запускай через sudo. Скрипт сам попросит пароль, где нужно."

  # ОС и GLIBC (pip-версия Isaac Sim требует GLIBC 2.35+, т.е. Ubuntu 22.04+)
  local glibc
  glibc=$(glibc_version)
  if [[ -z "$glibc" ]]; then
    warn "Не смог определить версию GLIBC — продолжаю (нужна Ubuntu 22.04 или новее)"
  else
    version_ge "$glibc" "2.35" || die "GLIBC $glibc < 2.35. Нужна Ubuntu 22.04 или новее."
    ok "GLIBC $glibc"
  fi

  # GPU и драйвер
  command -v nvidia-smi >/dev/null || die "nvidia-smi не найден. Поставь драйвер NVIDIA."
  local gpu drv vram
  nvidia-smi >/dev/null 2>&1 || die "nvidia-smi не видит видеокарту. На ноутбуке: sudo prime-select nvidia и перезагрузка."
  gpu=$(nvidia-smi --query-gpu=name --format=csv,noheader | sed -n 1p)
  drv=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | sed -n 1p)
  vram=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | sed -n 1p | tr -dc '0-9')
  ok "GPU: $gpu, ${vram} МБ VRAM, драйвер $drv"
  local drv_major="${drv%%.*}"
  if (( drv_major >= 590 )) && [[ "${ALLOW_DRIVER:-0}" != "1" ]]; then
    # Ветка 59x+ роняет Isaac Sim 5.1 при старте (librtx.scenedb.plugin.so) — подтверждено NVIDIA
    die "Драйвер $drv (ветка $drv_major) несовместим с Isaac Sim 5.1: он падает при старте.
  Нужна ветка 580. Запусти:  bash switch_nvidia_580.sh   и перезагрузись.
  (Проверить всё равно: ALLOW_DRIVER=1 bash install_unitree_sim.sh)"
  elif ! version_ge "$drv" "$MIN_DRIVER"; then
    warn "Драйвер $drv старше $MIN_DRIVER, на котором тестировали Isaac Sim 5.x."
    warn "Может работать, но если будут вылеты — поставь ветку 580: bash switch_nvidia_580.sh"
  fi
  (( vram < 10000 )) && warn "VRAM меньше 10 ГБ: в симе держи 1 среду, камеры ≤640×480, закрывай браузер."

  # Оперативка
  local ram_gb swap_gb
  ram_gb=$(free -g | awk '/^Mem:/ {print $2}')
  swap_gb=$(free -g | awk '/^Swap:/ {print $2}')
  ok "RAM: ${ram_gb} ГБ, swap: ${swap_gb} ГБ"
  if (( ram_gb + swap_gb < 32 )); then
    warn "RAM+swap меньше 32 ГБ (минимум для Isaac Sim). Isaac Sim может вылетать по памяти."
    warn "Добавь swap на 16 ГБ:"
    warn "  sudo fallocate -l 16G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile"
    warn "  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab"
  fi

  # Место на диске — проверяем ДО того, как качать
  # При перезапуске после установки Isaac Sim (он сам занимает ~20 ГБ) требуем меньше
  local free_gb need_gb="$MIN_FREE_GB"
  [[ -f "$STATE/07_isaacsim.done" ]] && need_gb=15
  free_gb=$(df -BG --output=avail "$ROOT" | tail -1 | tr -dc '0-9')
  (( free_gb >= need_gb )) || die "Свободно ${free_gb} ГБ в $ROOT, нужно минимум ${need_gb}. Запусти clean_unitree_install.sh."
  ok "Свободно на диске: ${free_gb} ГБ"

  command -v conda >/dev/null || die "conda не найдена. Поставь Miniconda: https://docs.conda.io/en/latest/miniconda.html"
  ok "conda: $(conda --version)"
}

apt_deps() {
  sudo apt-get update
  sudo apt-get install -y build-essential cmake git git-lfs unzip openssl
  git lfs install
}

git_clone() {  # url папка [тег/ветка]
  local url="$1" dir="$2" ref="${3:-}"
  if [[ -d "$dir/.git" ]]; then
    ok "$(basename "$dir") уже склонирован"
    return 0
  fi
  rm -rf -- "$dir"   # недоклонированная папка от прошлой попытки
  if [[ -n "$ref" ]]; then
    git clone --depth 1 --branch "$ref" "$url" "$dir"
  else
    git clone --depth 1 "$url" "$dir"
  fi
}

clone_repos() {
  cd "$ROOT"
  git_clone https://github.com/unitreerobotics/unitree_sim_isaaclab.git "$REPO"
  git -C "$REPO" submodule update --init --depth 1

  # Isaac Lab строго нужной версии — главный источник конфликтов
  if [[ -d "$ROOT/IsaacLab/.git" ]]; then
    local head want
    head=$(git -C "$ROOT/IsaacLab" rev-parse HEAD)
    want=$(git -C "$ROOT/IsaacLab" rev-parse "${ISAACLAB_REF}^{commit}" 2>/dev/null || echo none)
    if [[ "$head" != "$want" ]]; then
      warn "В $ROOT/IsaacLab лежит не $ISAACLAB_REF (скорее всего, main от старой попытки) — переклонирую"
      rm -rf -- "$ROOT/IsaacLab"
    fi
  fi
  git_clone https://github.com/isaac-sim/IsaacLab.git "$ROOT/IsaacLab" "$ISAACLAB_REF"

  git_clone https://github.com/eclipse-cyclonedds/cyclonedds.git "$ROOT/cyclonedds" "$CYCLONEDDS_BRANCH"
  git_clone https://github.com/unitreerobotics/unitree_sdk2_python.git "$ROOT/unitree_sdk2_python"

  # Если раньше что-то делалось через sudo — вернуть права
  if [[ -n "$(find "$REPO" -user root -print -quit 2>/dev/null)" ]]; then
    warn "В репозитории есть файлы root, возвращаю права"
    sudo chown -R "$USER:$(id -gn)" "$REPO"
  fi
}

build_cyclonedds() {
  cmake -S "$ROOT/cyclonedds" -B "$ROOT/cyclonedds/build" \
        -DCMAKE_INSTALL_PREFIX="$ROOT/cyclonedds/install" \
        -DBUILD_EXAMPLES=OFF -DBUILD_TESTING=OFF
  cmake --build "$ROOT/cyclonedds/build" --target install -j"$(nproc)"
}

write_constraints() {
  # Пишется при КАЖДОМ запуске (не шаг), чтобы исправления доезжали и при перезапуске
  cat > "$CONSTRAINTS" <<EOF
# Зафиксированные версии. Любой pip install в окружении $ENV_NAME обязан с ними
# совпадать — иначе pip честно упадёт, а не тихо сломает Isaac Sim.
torch==$TORCH_VER
torchvision==$TORCHVISION_VER
torchaudio==$TORCHAUDIO_VER
# Жёсткие версии самого Isaac Sim 5.1 (isaacsim-kernel / isaacsim-core / msal):
numpy==1.26.0
psutil==5.9.8
cryptography==44.0.0
packaging==23.0
# setuptools 82 удалил pkg_resources -> не собирается flatdict (нужен Isaac Lab)
setuptools<82
# wheel >= 0.46 требует packaging >= 24, а Isaac Sim держит 23.0
wheel<0.46
# pyarrow 26 требует numpy 2, но НЕ объявляет это в зависимостях -> pip не видит
# конфликта, а rerun-sdk (нужен Unitree) падает при импорте. 25.x работает с numpy 1.26
pyarrow<26
EOF
  # Обычные constraints pip не применяет к изолированной сборке пакетов
  # из исходников — для неё отдельный файл (PIP_BUILD_CONSTRAINT, pip >= 25.3)
  printf 'setuptools<82\n' > "$BUILD_CONSTRAINTS"
}

conda_init() {
  # shellcheck disable=SC1091
  source "$(conda info --base)/etc/profile.d/conda.sh"
}

create_env() {
  if env_exists "$ENV_NAME"; then
    warn "Окружение $ENV_NAME осталось от прошлой попытки — пересоздаю начисто"
    conda env remove -n "$ENV_NAME" -y
  fi
  # conda-forge + override-channels: обходим требование принять ToS каналов defaults
  conda create -y -n "$ENV_NAME" -c conda-forge --override-channels "python=$PY_VER" libstdcxx-ng
}

env_vars() {
  # Переменные окружения conda (ставятся при каждом запуске — дёшево и всегда актуально)
  conda env config vars set -n "$ENV_NAME" \
    OMNI_KIT_ACCEPT_EULA=YES \
    CYCLONEDDS_HOME="$ROOT/cyclonedds/install" \
    PIP_CONSTRAINT="$CONSTRAINTS" \
    PIP_BUILD_CONSTRAINT="$BUILD_CONSTRAINTS" >/dev/null
}

activate_env() {
  conda activate "$ENV_NAME"
  [[ "$(python -c 'import sys;print(f"{sys.version_info[0]}.{sys.version_info[1]}")')" == "$PY_VER" ]] \
    || die "В окружении не Python $PY_VER"
  export PIP_CONSTRAINT="$CONSTRAINTS" PIP_BUILD_CONSTRAINT="$BUILD_CONSTRAINTS" PIP_NO_CACHE_DIR=1
  export CYCLONEDDS_HOME="$ROOT/cyclonedds/install" OMNI_KIT_ACCEPT_EULA=YES
  export TMPDIR="$ROOT/.tmp"; mkdir -p "$TMPDIR"

  # PIP_BUILD_CONSTRAINT понимает только pip >= 25.3
  local pipv
  pipv=$(python -m pip --version | awk '{print $2}')
  if ! version_ge "$pipv" "25.3"; then
    info "Обновляю pip $pipv -> свежий (нужен для build-constraints)"
    python -m pip install --no-cache-dir --upgrade pip
  fi
}

install_torch() {
  pipi "torch==$TORCH_VER" "torchvision==$TORCHVISION_VER" "torchaudio==$TORCHAUDIO_VER" --index-url "$TORCH_INDEX"
}

install_isaacsim() {
  pipi "isaacsim[all,extscache]==$ISAACSIM_VER" --extra-index-url "$NVIDIA_INDEX"
}

install_isaaclab() {
  # flatdict==4.0.1 собирается только из исходников и требует pkg_resources.
  # Ставим его заранее без изоляции сборки, на setuptools<82 — так он точно соберётся.
  # (pip запрещает --no-build-isolation вместе с PIP_BUILD_CONSTRAINT — убираем её на эту команду)
  pipi "setuptools<82" "wheel<0.46"
  env -u PIP_BUILD_CONSTRAINT python -m pip install --no-cache-dir --no-build-isolation flatdict==4.0.1

  cd "$ROOT/IsaacLab"
  # rsl_rl вместо all: не тащим лишние RL-фреймворки
  ./isaaclab.sh --install rsl_rl

  # isaaclab.sh не возвращает ошибку, если модуль не встал (так и было в прошлый раз),
  # поэтому проверяем сами. Только «установлен ли пакет», без import: модули Isaac Lab
  # тянут pxr (USD), а он появляется лишь после запуска Isaac Sim — это делает smoke_test.
  python - <<'EOF' || die "Isaac Lab установился не полностью. Найди в логе первую строку с ERROR выше."
import importlib.util, sys
missing = [m for m in ("isaaclab", "isaaclab_tasks", "isaaclab_rl", "isaaclab_assets", "flatdict")
           if importlib.util.find_spec(m) is None]
print("  не установлены:", ", ".join(missing)) if missing else print("  ✓ модули Isaac Lab на месте")
sys.exit(1 if missing else 0)
EOF
}

fix_pyarrow() {
  # Для окружений, где уже успел встать pyarrow 26 (он ломает rerun при numpy 1.26)
  pipi "pyarrow<26"
}

fix_pins() {
  # Возвращает версии, которые прошлые запуски могли сломать (psutil 7.x, cryptography 50.x).
  # pyopenssl 25.1.0 — последний, совместимый с cryptography 44 (нужен aiortc).
  pipi numpy==1.26.0 psutil==5.9.8 cryptography==44.0.0 packaging==23.0 pyopenssl==25.1.0 "wheel<0.46"
}

install_unitree() {
  cd "$ROOT/unitree_sdk2_python"
  pipi -e .

  cd "$REPO"
  pipi -r requirements.txt

  # teleimager: брать картинки из Isaac Sim, 480x640 (как в оригинальном скрипте)
  sed -i 's/type:.*/type: isaacsim/' teleimager/cam_config_server.yaml
  sed -i 's/image_shape:.*/image_shape: [480, 640]/' teleimager/cam_config_server.yaml
  pipi -e teleimager

  # Сертификаты для xr_teleoperate — без интерактивных вопросов
  local cfg="$HOME/.config/xr_teleoperate"
  if [[ ! -f "$cfg/cert.pem" ]]; then
    mkdir -p "$cfg"
    openssl req -x509 -nodes -days 365 -newkey rsa:2048 -subj "/CN=localhost" \
      -keyout "$cfg/key.pem" -out "$cfg/cert.pem" 2>/dev/null
  fi
}

fetch_assets() {
  cd "$REPO"
  bash fetch_assets.sh   # без sudo!
}

verify() {
  python - <<'EOF'
import sys
fail = False
def check(name, fn):
    global fail
    try:
        print(f"  ✓ {name}: {fn()}")
    except Exception as e:
        print(f"  ✗ {name}: {e}")
        fail = True

import torch
check("torch", lambda: f"{torch.__version__}, CUDA {torch.version.cuda}, GPU доступен: {torch.cuda.is_available()}")
if not torch.cuda.is_available():
    fail = True
from importlib.metadata import version
want = {"numpy": "1.26.0", "psutil": "5.9.8", "cryptography": "44.0.0"}
for pkg, v in want.items():
    def chk(pkg=pkg, v=v):
        got = version(pkg)
        if got != v:
            raise RuntimeError(f"{got}, а Isaac Sim нужна {v}")
        return got
    check(pkg, chk)
for mod in ["isaacsim", "unitree_sdk2py", "cyclonedds", "teleimager"]:
    check(mod, lambda mod=mod: (__import__(mod), "импортируется")[1])
# Бинарные пакеты, собранные под numpy: ловим несовместимость с numpy 1.26 здесь,
# а не через минуту загрузки сцены в sim_main.py (так было с pyarrow 26)
import importlib
for mod in ["pyarrow", "rerun", "cv2", "scipy.spatial", "h5py", "matplotlib", "onnxruntime", "zmq"]:
    check(mod, lambda mod=mod: (importlib.import_module(mod), "импортируется")[1])
# Модули Isaac Lab импортируются только внутри запущенного Isaac Sim (нужен pxr),
# здесь проверяем, что они установлены; настоящий импорт — в smoke-тесте
import importlib.util
for mod in ["isaaclab", "isaaclab_tasks", "isaaclab_rl", "isaaclab_assets"]:
    def found(mod=mod):
        if importlib.util.find_spec(mod) is None:
            raise ModuleNotFoundError(f"не установлен")
        return "установлен"
    check(mod, found)
sys.exit(1 if fail else 0)
EOF
  # fastapi/starlette — известное расхождение самого Isaac Lab 2.3.2 (он специально
  # поднимает starlette до 0.49.1 из-за уязвимости), на работу не влияет
  local issues
  issues=$(python -m pip check 2>&1 | grep -viE 'fastapi|starlette|No broken requirements' || true)
  if [[ -n "$issues" ]]; then
    warn "pip check нашёл расхождения (обычно некритично, но если сим упадёт — смотреть сюда):"
    echo "$issues" | sed 's/^/      /'
  else
    ok "pip check: критичных конфликтов нет"
  fi
}

smoke_test() {
  if [[ "$SMOKE_TEST" != "1" ]]; then
    warn "Пробный запуск пропущен (SMOKE_TEST=0)"
    return 0
  fi
  info "Пробный запуск Isaac Sim + Isaac Lab без окна, с камерами."
  info "Первый раз компилирует шейдеры — до 10–15 минут и может казаться зависшим. Это нормально."
  # Порядок как в официальных примерах Isaac Lab: сначала AppLauncher запускает
  # симулятор, и только потом импортируется всё остальное (pxr появляется здесь)
  # Важно: app.close() завершает процесс из C++ — всё, что Python держал в буфере
  # stdout, теряется (поэтому в прошлый раз пропала метка OK). Поэтому:
  #   * результат пишем в отдельный файл с fsync ДО close();
  #   * python запускаем без буферизации (-u).
  local marker="$ROOT/.tmp/smoke.ok"
  rm -f "$marker"
  cat > "$ROOT/.tmp/smoke_test.py" <<'EOF'
import os, sys
marker = sys.argv[1]

def stage(msg):
    print(f"[smoke] {msg}", flush=True)

stage("запускаю Isaac Sim (headless + камеры)")
from isaaclab.app import AppLauncher
app = AppLauncher(headless=True, enable_cameras=True).app

stage("Isaac Sim запущен, импортирую Isaac Lab")
import isaaclab_tasks  # noqa: F401  — регистрирует задачи, тянет весь Isaac Lab
from isaaclab.sensors import CameraCfg, MultiMeshRayCasterCfg  # noqa: F401  — камера и наш лидар
from isaaclab.sim import SimulationCfg, SimulationContext

stage("создаю сцену и делаю 10 шагов физики")
sim = SimulationContext(SimulationCfg(dt=0.01))
sim.reset()
for _ in range(10):
    sim.step()

with open(marker, "w") as f:
    f.write("ISAAC_SIM_STARTED_OK\n")
    f.flush()
    os.fsync(f.fileno())
stage("ISAAC_SIM_STARTED_OK — закрываю")
app.close()
EOF
  # Код выхода сохраняем в файл: PIPESTATUS после `|| true` уже не тот
  local rc
  ( r=0; timeout 1800 python -u "$ROOT/.tmp/smoke_test.py" "$marker" 2>&1 || r=$?; echo "$r" > "$ROOT/.tmp/smoke.rc" ) \
    | tee "$ROOT/.tmp/smoke.log" | grep -E '^\[smoke\]|Traceback|Error:' || true
  rc=$(cat "$ROOT/.tmp/smoke.rc" 2>/dev/null || echo 1)

  if [[ -f "$marker" ]]; then
    ok "Isaac Sim + Isaac Lab запускаются, физика шагает, камера и лидар доступны"
    return 0
  fi

  # Не прошло — собираем всё, что поможет понять почему
  cp "$ROOT/.tmp/smoke.log" "$ROOT/smoke.log"
  local kitlog
  kitlog=$(ls -t "$CONDA_PREFIX"/lib/python3.11/site-packages/isaacsim/kit/logs/Kit/Isaac-Sim/*/kit_*.log 2>/dev/null | sed -n 1p || true)
  [[ -n "$kitlog" ]] && cp "$kitlog" "$ROOT/kit.log"
  case "$rc" in
    124) die "Пробный запуск не уложился в 30 минут (завис). Пришли $ROOT/smoke.log и $ROOT/kit.log" ;;
    137) die "Процесс убит системой — скорее всего, кончилась оперативка (OOM).
  Проверь: journalctl -k -b | grep -i -E 'oom|killed process'
  Помогает: закрыть браузер и всё тяжёлое, увеличить swap до 32 ГБ. Пришли $ROOT/kit.log" ;;
    *)   die "Пробный запуск не прошёл (код $rc). Пришли $ROOT/smoke.log и $ROOT/kit.log" ;;
  esac
}

cleanup() {
  conda clean --all -y >/dev/null 2>&1 || true
  rm -rf -- "$ROOT/.tmp" "$ROOT/cyclonedds/build"
  ok "Кэши и временные файлы удалены"
}

# ---------------------------- запуск -----------------------------------------
mkdir -p "$ROOT" "$STATE" "$ROOT/.tmp"
exec > >(tee -a "$LOG") 2>&1

echo -e "\n${B}=== Установка unitree_sim_isaaclab ($(date '+%F %T')) ===${N}"
echo "  Isaac Sim $ISAACSIM_VER | Isaac Lab $ISAACLAB_REF | Python $PY_VER | torch $TORCH_VER (cu128)"
echo "  Папка: $ROOT | conda env: $ENV_NAME | лог: $LOG"
echo

info "Проверка системы"; preflight
step "01_apt"             apt_deps
step "02_clone"           clone_repos
step "03_cyclonedds"      build_cyclonedds
write_constraints
conda_init
step "05_conda_env"       create_env
env_vars
activate_env
step "06_torch"           install_torch
step "07_isaacsim"        install_isaacsim
step "07b_fix_pins"       fix_pins
# _v2: в прошлой версии скрипта эти шаги могли «пройти» с ошибкой — переделываем
step "08_isaaclab_v2"     install_isaaclab
step "09_unitree_v2"      install_unitree
step "09c_pyarrow"        fix_pyarrow
step "10_assets"          fetch_assets
info "Проверка установки"; verify
step "11_smoke_test"      smoke_test
cleanup

FREE_LEFT=$(df -h --output=avail "$ROOT" | tail -1 | tr -d ' ')
cat <<EOF

${G}${B}=== Готово! ===${N}  Свободно на диске: $FREE_LEFT

Запуск (первый раз сцена грузится несколько минут):

  conda activate $ENV_NAME
  cd $REPO
  python sim_main.py --device cpu --enable_cameras --task Isaac-Move-Cylinder-G129-Dex1-Wholebody --enable_dex1_dds --robot_type g129

  (--device — где считается ФИЗИКА; рендер и камеры всегда на видеокарте)

Управление с клавиатуры (во втором терминале):

  conda activate $ENV_NAME && cd $REPO && python send_commands_keyboard.py

На ноутбуке: включи дискретную видеокарту (sudo prime-select nvidia + перезагрузка)
и держи ноут на зарядке.

Версии защищены файлом $CONSTRAINTS. Если очень надо поставить
пакет вне этих рамок: PIP_CONSTRAINT= pip install <пакет>
EOF
