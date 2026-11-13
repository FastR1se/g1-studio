#!/usr/bin/env bash
# =============================================================================
# setup_studio.sh — ставит G1 Studio в ОТДЕЛЬНОЕ окружение (не трогая unitree_sim с Isaac Sim).
#
# Зачем отдельное: PySide6 и свежий onnxruntime/opencv не должны конфликтовать с пинами
# Isaac Sim (numpy 1.26, torch 2.7 и т.д.). Сим G1 Studio запускает сама — в его родном env.
#
# Что нужно заранее: scripts/install_unitree_sim.sh (он собирает CycloneDDS и качает
# unitree_sdk2_python в ~/unitree_ws — G1 Studio берёт их оттуда).
#
# Запуск:
#   bash scripts/setup_studio.sh              # conda-окружение g1studio
#   bash scripts/setup_studio.sh --bench      # + MuJoCo для проверки без Isaac Sim
#   bash scripts/setup_studio.sh --venv ~/.venvs/g1studio   # без conda: обычный venv (python3.10+)
#
# Переменные: ROOT=~/unitree_ws, ENV_NAME=g1studio, CYCLONEDDS_HOME, UNITREE_SDK
# Повторный запуск безопасен: что уже стоит — обновится, остальное доустановится.
# =============================================================================
set -Eeuo pipefail

ROOT="${ROOT:-$HOME/unitree_ws}"
ENV_NAME="${ENV_NAME:-g1studio}"
CYCLONEDDS_HOME="${CYCLONEDDS_HOME:-$ROOT/cyclonedds/install}"
UNITREE_SDK="${UNITREE_SDK:-$ROOT/unitree_sdk2_python}"
PY_VER="3.11"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BENCH=0
VENV=""
NO_APT=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bench) BENCH=1 ;;
    --venv) VENV="${2:?--venv: укажи папку}"; shift ;;
    --no-apt) NO_APT=1 ;;
    -h|--help) sed -n 2,19p "$0"; exit 0 ;;
    *) echo "Неизвестный параметр: $1 (см. --help)"; exit 2 ;;
  esac
  shift
done

G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; B=$'\e[1m'; N=$'\e[0m'
info() { echo -e "${B}▶ $*${N}"; }
ok()   { echo -e "  ${G}✓${N} $*"; }
warn() { echo -e "  ${Y}!${N} $*"; }
die()  { echo -e "\n${R}${B}✗ $*${N}\n"; exit 1; }
trap 'echo -e "\n${R}${B}✗ Упало на строке $LINENO: $BASH_COMMAND${N}\n  Исправь причину и запусти ещё раз.\n"' ERR

[[ $EUID -ne 0 || -n "${ALLOW_ROOT:-}" ]] || die "Не запускай через sudo — скрипт сам попросит пароль, где нужно."

# ---------------------------------------------------------------------------
info "Проверяю, что уже есть"
[[ -f "$REPO_DIR/pyproject.toml" ]] || die "Не вижу pyproject.toml в $REPO_DIR — запускай из репозитория g1-studio."
if [[ -f "$CYCLONEDDS_HOME/lib/libddsc.so" ]]; then
  ok "CycloneDDS: $CYCLONEDDS_HOME"
else
  die "Нет CycloneDDS в $CYCLONEDDS_HOME.
  Сначала поставь сим: bash scripts/install_unitree_sim.sh
  (или укажи свою сборку: CYCLONEDDS_HOME=/путь/к/install bash scripts/setup_studio.sh)"
fi
[[ -f "$UNITREE_SDK/setup.py" ]] || die "Нет unitree_sdk2_python в $UNITREE_SDK — сначала bash scripts/install_unitree_sim.sh"
ok "unitree_sdk2_python: $UNITREE_SDK"

# ---------------------------------------------------------------------------
if [[ $NO_APT -eq 0 ]] && command -v dpkg >/dev/null; then
  missing=()
  for p in libxcb-cursor0 libegl1; do
    dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p")
  done
  if (( ${#missing[@]} )); then
    info "Системные библиотеки для окна Qt: ${missing[*]}"
    sudo apt-get install -y "${missing[@]}"
  fi
  ok "библиотеки Qt на месте"
fi

# ---------------------------------------------------------------------------
if [[ -n "$VENV" ]]; then
  info "Окружение: venv $VENV"
  if [[ ! -x "$VENV/bin/python" ]]; then
    PY="${PYTHON:-python3}"
    "$PY" -c 'import sys; assert sys.version_info >= (3, 10)' 2>/dev/null \
      || die "$PY старше 3.10. Укажи другой: PYTHON=python3.11 bash scripts/setup_studio.sh --venv $VENV"
    "$PY" -m venv "$VENV"
  fi
  ENV_BIN="$VENV/bin"
else
  info "Окружение: conda $ENV_NAME (Python $PY_VER)"
  command -v conda >/dev/null || die "conda не найдена. Поставь Miniconda или используй --venv папка"
  # shellcheck disable=SC1091
  source "$(conda info --base)/etc/profile.d/conda.sh"
  if conda env list | awk -v e="$ENV_NAME" '$1==e {f=1} END {exit !f}'; then
    ok "уже есть"
  else
    # conda-forge + override-channels: без вопроса про ToS каналов defaults
    conda create -y -n "$ENV_NAME" -c conda-forge --override-channels "python=$PY_VER"
  fi
  ENV_BIN="$(conda run -n "$ENV_NAME" python -c 'import sys,os;print(os.path.dirname(sys.executable))')"
fi
PYBIN="$ENV_BIN/python"
pipi() { CYCLONEDDS_HOME="$CYCLONEDDS_HOME" "$PYBIN" -m pip install --no-cache-dir "$@"; }
ok "python: $("$PYBIN" --version) ($PYBIN)"

# setuptools 82 убрал pkg_resources — некоторые старые пакеты без него не собираются
BUILD_CONSTRAINTS="$(mktemp)"
echo "setuptools<82" > "$BUILD_CONSTRAINTS"
export PIP_BUILD_CONSTRAINT="$BUILD_CONSTRAINTS"

info "Пакеты"
pipi -q --upgrade pip
pipi -q "setuptools<82" wheel
# cyclonedds 0.10.2 собирается против нашей CycloneDDS (путь к libddsc запоминается при сборке)
pipi -q "cyclonedds==0.10.2"
# unitree_sdk2py тянет opencv-python, а он ломает Qt — ставим без зависимостей, их даёт g1-studio
pipi -q --no-deps -e "$UNITREE_SDK"
extras="dev"
[[ $BENCH -eq 1 ]] && extras="dev,bench"
pipi -q -e "${REPO_DIR}[${extras}]"
if "$PYBIN" -m pip show opencv-python >/dev/null 2>&1; then
  warn "убираю opencv-python (конфликтует с Qt), оставляю opencv-python-headless"
  "$PYBIN" -m pip uninstall -y -q opencv-python
  pipi -q --force-reinstall --no-deps opencv-python-headless
fi
rm -f "$BUILD_CONSTRAINTS"
ok "установлено"

# ---------------------------------------------------------------------------
info "Проверка"
"$PYBIN" - <<'EOF'
import importlib
for m in ("PySide6.QtWidgets", "cv2", "zmq", "onnxruntime", "cyclonedds", "unitree_sdk2py.core.channel", "g1_studio.cli"):
    importlib.import_module(m)
print("  ✓ все модули импортируются")
EOF
QT_QPA_PLATFORM=offscreen "$PYBIN" - <<'EOF'
from PySide6.QtWidgets import QApplication, QLabel
app = QApplication([])
w = QLabel("G1 Studio"); w.show(); app.processEvents()
print("  ✓ Qt рисует окно")
EOF

# ---------------------------------------------------------------------------
info "Ярлыки"
mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications"
ln -sf "$ENV_BIN/g1-studio" "$HOME/.local/bin/g1-studio"
ok "команда: ~/.local/bin/g1-studio"
cat > "$HOME/.local/share/applications/g1-studio.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=G1 Studio
Comment=Пульт для Unitree G1 в Isaac Sim
Exec=$ENV_BIN/g1-studio
Icon=applications-science
Terminal=false
Categories=Development;Science;
EOF
ok "ярлык в меню приложений: G1 Studio"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) warn "добавь ~/.local/bin в PATH (или перелогинься)";; esac

echo -e "\n${G}${B}✓ G1 Studio установлена.${N}"
echo "  Запуск:        g1-studio            (или «G1 Studio» в меню)"
echo "  Проверка:      g1-studio doctor"
echo "  Без Isaac Sim: g1-studio --dry      (интерфейс и планы Qwen)"
[[ $BENCH -eq 1 ]] && echo "  MuJoCo-стенд:  в настройках «Что запускать» = bench, потом «▶ Запустить сим»"
