#!/usr/bin/env bash
# =============================================================================
# clean_unitree_install.sh — чистит хвосты неудачной установки unitree_sim_isaaclab
#
# Что делает (по каждому пункту спрашивает подтверждение):
#   1. Показывает, сколько места и памяти занято сейчас
#   2. Предлагает убить зависшие процессы установки (pip, cmake, Isaac Sim)
#   3. Удаляет conda-окружения unitree_sim_env / unitree_sim
#   4. Удаляет клоны IsaacLab, cyclonedds, unitree_sdk2_python
#   5. Возвращает тебе права на файлы, созданные через sudo
#   6. Чистит кэши pip, conda, Omniverse/Isaac Sim, шейдеров, /tmp
#   7. Показывает, сколько места освободилось
#
# Использование:
#   bash clean_unitree_install.sh [ПАПКА] [ИМЯ_ENV] [-y] [--with-repo]
#
#   ПАПКА        где лежат unitree_sim_isaaclab, IsaacLab и т.д. (по умолчанию: текущая;
#                если запустить внутри unitree_sim_isaaclab — возьмёт родительскую)
#   ИМЯ_ENV      имя conda-окружения (по умолчанию unitree_sim_env и unitree_sim)
#   -y           удалять без вопросов
#   --with-repo  удалить и сам unitree_sim_isaaclab (по умолчанию НЕ трогается,
#                там могут быть твои правки)
# =============================================================================
set -o pipefail
USER="${USER:-$(id -un)}"

YES=0
WITH_REPO=0
POS=()
for a in "$@"; do
  case "$a" in
    -y|--yes) YES=1 ;;
    --with-repo) WITH_REPO=1 ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) POS+=("$a") ;;
  esac
done

ROOT="${POS[0]:-$PWD}"
[[ -f "$ROOT/sim_main.py" ]] && ROOT="$(dirname "$ROOT")"
ROOT="$(cd "$ROOT" 2>/dev/null && pwd)" || { echo "Папка не найдена: ${POS[0]}"; exit 1; }

if [[ -n "${POS[1]:-}" ]]; then
  ENV_NAMES=("${POS[1]}")
else
  ENV_NAMES=(unitree_sim_env unitree_sim)
fi

G=$'\e[32m'; Y=$'\e[33m'; B=$'\e[1m'; N=$'\e[0m'

say()  { echo -e "\n${B}== $* ==${N}"; }
ok()   { echo -e "  ${G}✓${N} $*"; }
warn() { echo -e "  ${Y}!${N} $*"; }

ask() {
  [[ $YES == 1 ]] && return 0
  local r
  read -r -p "  $1 [y/N] " r
  [[ $r =~ ^([yY]|[дД])$ ]]
}

size_of() { du -sh "$1" 2>/dev/null | cut -f1; }

free_gb() { df -BG --output=avail "$1" 2>/dev/null | tail -1 | tr -dc '0-9'; }

# Защита от удаления чего-то важного
is_dangerous() {
  local p
  p="$(realpath -m "$1")"
  [[ "$p" == "/" || "$p" == "$HOME" || "$p" == "/home" || "$p" == "/usr" || "$p" == "/etc" || "$p" == "$ROOT" ]]
}

remove_path() {
  local p="$1" desc="$2"
  [[ -e "$p" ]] || return 0
  if is_dangerous "$p"; then warn "пропускаю опасный путь: $p"; return 0; fi
  echo "  $desc: $p (${B}$(size_of "$p")${N})"
  if ask "Удалить?"; then
    if rm -rf -- "$p" 2>/dev/null; then
      ok "удалено"
    else
      warn "часть файлов принадлежит root (видимо, ставилось через sudo) — удаляю через sudo"
      sudo rm -rf -- "$p" && ok "удалено"
    fi
  fi
}

BEFORE_HOME=$(free_gb "$HOME")
BEFORE_ROOT=$(free_gb "$ROOT")

# ----------------------------------------------------------------------------
say "Сейчас"
echo "  Рабочая папка: $ROOT"
df -h "$HOME" "$ROOT" 2>/dev/null | sort -u
echo
free -h

# ----------------------------------------------------------------------------
say "1. Зависшие процессы установки"
PIDS=$(pgrep -u "$USER" -f 'pip install|isaaclab\.sh|cmake --build|isaacsim|kit/kit|fetch_assets|git-lfs' 2>/dev/null | grep -v "^$$\$" || true)
if [[ -n "$PIDS" ]]; then
  # shellcheck disable=SC2086
  ps -o pid,rss,etime,cmd -p $PIDS | cut -c1-150
  if ask "Убить эти процессы (освободит оперативку)?"; then
    # shellcheck disable=SC2086
    kill $PIDS 2>/dev/null; sleep 2
    # shellcheck disable=SC2086
    kill -9 $PIDS 2>/dev/null
    ok "процессы остановлены"
  fi
else
  ok "ничего не висит"
fi

# ----------------------------------------------------------------------------
say "2. Conda-окружения"
if command -v conda >/dev/null 2>&1; then
  # shellcheck disable=SC1091
  source "$(conda info --base)/etc/profile.d/conda.sh"
  for env in "${ENV_NAMES[@]}"; do
    if conda env list | awk -v e="$env" '$1==e {f=1} END {exit !f}'; then
      prefix=$(conda env list | awk -v e="$env" '$1==e {print $NF}')
      echo "  окружение $env: $prefix (${B}$(size_of "$prefix")${N})"
      if ask "Удалить окружение $env?"; then
        [[ "${CONDA_DEFAULT_ENV:-}" == "$env" ]] && conda deactivate
        conda env remove -n "$env" -y >/dev/null 2>&1 || true
        # conda иногда оставляет папку с мусором
        [[ -d "$prefix" ]] && rm -rf -- "$prefix" 2>/dev/null
        ok "окружение $env удалено"
      fi
    fi
  done
  echo "  кэш пакетов conda: $(size_of "$(conda info --base)/pkgs")"
  if ask "Очистить кэш conda (conda clean --all)?"; then
    conda clean --all -y >/dev/null 2>&1 && ok "кэш conda очищен"
  fi
else
  warn "conda не найдена, пропускаю"
fi

# ----------------------------------------------------------------------------
say "3. Склонированные зависимости"
remove_path "$ROOT/IsaacLab"            "Isaac Lab (скорее всего, не той версии)"
remove_path "$ROOT/cyclonedds"          "CycloneDDS"
remove_path "$ROOT/unitree_sdk2_python" "unitree_sdk2_python"
remove_path "$ROOT/.install_state"      "метки прошлой установки"
remove_path "$ROOT/.tmp"                "временные файлы установки"

# ----------------------------------------------------------------------------
say "4. Репозиторий unitree_sim_isaaclab"
REPO="$ROOT/unitree_sim_isaaclab"
if [[ -d "$REPO" ]]; then
  if [[ $WITH_REPO == 1 ]]; then
    remove_path "$REPO" "репозиторий целиком"
  else
    echo "  $REPO (${B}$(size_of "$REPO")${N}) — не трогаю (добавь --with-repo, чтобы удалить)"
    if [[ -n "$(find "$REPO" -user root -print -quit 2>/dev/null)" ]]; then
      warn "в репозитории есть файлы root (их создал 'sudo bash fetch_assets.sh')"
      if ask "Вернуть права на файлы пользователю $USER?"; then
        sudo chown -R "$USER:$(id -gn)" "$REPO" && ok "права исправлены"
      fi
    fi
  fi
else
  ok "репозитория нет"
fi

# ----------------------------------------------------------------------------
say "5. Кэши"
remove_path "$HOME/.cache/pip"               "кэш pip (там лежат скачанные колёса Isaac Sim, гигабайты)"
remove_path "$HOME/.cache/ov"                "кэш Omniverse/Isaac Sim"
remove_path "$HOME/.local/share/ov"          "данные Omniverse Kit (пересоздадутся при запуске)"
remove_path "$HOME/.nvidia-omniverse/logs"   "логи Omniverse"
remove_path "$HOME/.cache/nvidia/GLCache"    "кэш шейдеров NVIDIA"
remove_path "$HOME/.cache/warp"              "кэш NVIDIA Warp"
remove_path "$HOME/.cache/packman"           "кэш packman"

# Временные файлы pip/сборки в /tmp
TMP_LEFT=$(find /tmp -maxdepth 1 -user "$USER" \( -name 'pip-*' -o -name 'tmp*' -o -name 'build-*' \) 2>/dev/null)
if [[ -n "$TMP_LEFT" ]]; then
  echo "  мусор в /tmp: $(echo "$TMP_LEFT" | wc -l) шт. ($(echo "$TMP_LEFT" | xargs -d '\n' du -sch 2>/dev/null | tail -1 | cut -f1))"
  if ask "Удалить?"; then
    echo "$TMP_LEFT" | xargs -d '\n' rm -rf -- 2>/dev/null
    ok "удалено"
  fi
fi

if ask "Очистить кэш apt (sudo apt-get clean)?"; then
  sudo apt-get clean && ok "кэш apt очищен"
fi

# ----------------------------------------------------------------------------
say "Итог"
AFTER_HOME=$(free_gb "$HOME")
AFTER_ROOT=$(free_gb "$ROOT")
df -h "$HOME" "$ROOT" 2>/dev/null | sort -u
echo
free -h
echo
echo -e "  Освободилось в домашней папке: ${G}${B}$((AFTER_HOME - BEFORE_HOME)) ГБ${N}"
[[ "$(df --output=target "$HOME" | tail -1)" != "$(df --output=target "$ROOT" | tail -1)" ]] && \
  echo -e "  Освободилось в $ROOT: ${G}${B}$((AFTER_ROOT - BEFORE_ROOT)) ГБ${N}"
echo -e "  Свободно сейчас: ${B}${AFTER_ROOT} ГБ${N} (для установки нужно минимум 60)"
echo
echo "  Готово. Теперь можно ставить заново: bash install_unitree_sim.sh"
