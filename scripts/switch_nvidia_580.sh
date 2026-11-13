#!/usr/bin/env bash
# =============================================================================
# switch_nvidia_580.sh — откат драйвера NVIDIA на ветку 580 для Isaac Sim 5.1
#
# Зачем: драйверы ветки 595 (у тебя 595.99.02) роняют Isaac Sim 5.1 при старте
# в librtx.scenedb.plugin.so. NVIDIA для Isaac Sim 5.1 на Linux проверяла 580.
#
# Что делает:
#   1. Показывает, что стоит сейчас, и проверяет, что драйвер ставился через apt
#   2. Проверяет, что в репозитории есть 580 и ГОТОВЫЕ модули под твоё ядро
#      (ничего не собирается, модули подписаны Canonical — Secure Boot не мешает)
#   3. Сначала скачивает всё, и только потом меняет — без интернета не застрянешь
#      без драйвера
#   4. Одной транзакцией apt ставит 580 и убирает 595
#   5. Просит перезагрузку
#
# Использование:
#   bash switch_nvidia_580.sh            # с подтверждением
#   bash switch_nvidia_580.sh --dry-run  # только показать, что будет сделано
#
# Если после перезагрузки что-то не так с экраном: Ctrl+Alt+F3, войди в консоль и
#   sudo apt-get install nvidia-driver-595-open linux-modules-nvidia-595-open-generic-hwe-24.04
#   sudo reboot
# — вернёт как было (проверено: та же замена, только в обратную сторону).
# =============================================================================
set -Eeo pipefail

BRANCH=580
OLD_BRANCH_RE='59[0-9]|6[0-9][0-9]'   # всё новее 580, что ломает Isaac Sim 5.1
DRY=0
[[ "${1:-}" == "--dry-run" ]] && DRY=1

G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; B=$'\e[1m'; N=$'\e[0m'
info() { echo -e "${B}▶ $*${N}"; }
ok()   { echo -e "  ${G}✓${N} $*"; }
warn() { echo -e "  ${Y}!${N} $*"; }
die()  { echo -e "\n${R}${B}✗ $*${N}\n"; exit 1; }
run()  { if [[ $DRY == 1 ]]; then echo "  [dry-run] $*"; else "$@"; fi; }

[[ $EUID -ne 0 ]] || die "Запускай без sudo — пароль спрошу сам."

# --------------------------------------------------------------------------
info "Что стоит сейчас"
KERNEL=$(uname -r)
CUR=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | sed -n 1p || true)
ok "Ядро: $KERNEL"
ok "Драйвер: ${CUR:-не загружен}"

if [[ -n "$CUR" && "${CUR%%.*}" == "$BRANCH" ]]; then
  ok "Уже стоит ветка $BRANCH — делать ничего не нужно."
  exit 0
fi

INSTALLED=$(dpkg-query -W -f '${db:Status-Abbrev} ${Package}\n' 2>/dev/null \
  | awk '$1 ~ /^ii/ {print $2}' | grep -E 'nvidia' || true)
OLD_PKGS=$(echo "$INSTALLED" | grep -E -- "-(${OLD_BRANCH_RE})(-|$)" || true)

if [[ -n "$CUR" && -z "$OLD_PKGS" ]]; then
  die "Драйвер $CUR стоит не из apt (видимо, .run-файлом с сайта NVIDIA).
  Сначала удали его:  sudo nvidia-uninstall   — и перезагрузись, потом запусти скрипт снова."
fi
echo "  Будут удалены пакеты:"; echo "$OLD_PKGS" | sed 's/^/      /'

# --------------------------------------------------------------------------
info "Проверяю репозиторий"
run sudo apt-get update -qq

cand() { apt-cache policy "$1" 2>/dev/null | awk '/Candidate:/ {print $2}'; }

DRV_PKG="nvidia-driver-${BRANCH}-open"          # для RTX 20xx и новее NVIDIA советует open-модули
MOD_PKG="linux-modules-nvidia-${BRANCH}-open-${KERNEL}"

[[ -n "$(cand "$DRV_PKG")" && "$(cand "$DRV_PKG")" != "(none)" ]] \
  || die "В репозитории нет $DRV_PKG. Проверь, что включён restricted: sudo add-apt-repository restricted"
ok "$DRV_PKG: $(cand "$DRV_PKG")"

[[ -n "$(cand "$MOD_PKG")" && "$(cand "$MOD_PKG")" != "(none)" ]] \
  || die "Нет готовых модулей $MOD_PKG под твоё ядро $KERNEL.
  Обнови систему (sudo apt full-upgrade), перезагрузись и запусти снова."
ok "$MOD_PKG: есть (готовые, подписанные)"

# Мета-пакет модулей. Он нужен по двум причинам:
#  * модули сами обновляются вместе с ядром;
#  * он «закрывает» зависимость драйвера на nvidia-dkms — без него apt потащит
#    DKMS и попробует СОБИРАТЬ модуль под ядро 7.0, а это может упасть.
has()  { local c; c=$(cand "$1"); [[ -n "$c" && "$c" != "(none)" ]]; }
META=""
# 1) тот же вид мета-пакета, что стоял для старой ветки (самый точный вариант)
old_meta=$(echo "$OLD_PKGS" | grep -E "^linux-modules-nvidia-(${OLD_BRANCH_RE})-open-[a-z]" | sed -n 1p || true)
if [[ -n "$old_meta" ]]; then
  m=$(echo "$old_meta" | sed -E "s/^linux-modules-nvidia-(${OLD_BRANCH_RE})-open-/linux-modules-nvidia-${BRANCH}-open-/")
  has "$m" && META="$m"
fi
# 2) HWE-ядро Ubuntu 24.04
if [[ -z "$META" ]] && dpkg-query -W -f '${db:Status-Abbrev}' linux-generic-hwe-24.04 2>/dev/null | grep -q '^ii'; then
  has "linux-modules-nvidia-${BRANCH}-open-generic-hwe-24.04" && META="linux-modules-nvidia-${BRANCH}-open-generic-hwe-24.04"
fi
# 3) по версии ядра: 7.0.0-42-generic -> ...-generic-7.0
if [[ -z "$META" && "$KERNEL" == *-generic ]]; then
  m="linux-modules-nvidia-${BRANCH}-open-generic-$(echo "$KERNEL" | cut -d. -f1,2)"
  has "$m" && META="$m"
fi
[[ -n "$META" ]] || die "Не нашёл мета-пакет готовых модулей для ядра $KERNEL — без него apt полезет в DKMS.
  Пришли вывод: apt-cache search linux-modules-nvidia-${BRANCH}-open-generic"
ok "Мета-пакет модулей: $META"

NEW_PKGS=("$DRV_PKG" "$MOD_PKG" "$META")
REMOVE=()
while read -r p; do [[ -n "$p" ]] && REMOVE+=("${p}-"); done <<< "$OLD_PKGS"

# Сухой прогон apt: убеждаемся, что DKMS и сборки модуля не будет
info "Проверяю план apt (без изменений)"
SIM=$(apt-get -s install "${NEW_PKGS[@]}" "${REMOVE[@]}" 2>&1) || { echo "$SIM" | tail -15; die "apt не может построить план установки (вывод выше)."; }
if echo "$SIM" | grep -qE "^Inst (dkms|nvidia-dkms-${BRANCH})"; then
  echo "$SIM" | grep -E '^Inst' | sed 's/^/      /'
  die "apt хочет поставить DKMS (сборку модуля из исходников) — останавливаюсь, ничего не изменено. Пришли этот вывод."
fi
ok "DKMS не нужен — ставятся только готовые модули"

# --------------------------------------------------------------------------
echo
echo -e "${B}План:${N} поставить ${NEW_PKGS[*]}"
echo -e "      убрать ${#REMOVE[@]} пакетов ветки ${CUR%%.*}"
echo    "      потом перезагрузка"
if [[ $DRY == 0 ]]; then
  read -r -p "Продолжаем? [y/N] " a
  [[ $a =~ ^([yY]|[дД])$ ]] || die "Отменено, ничего не изменено."
fi

info "Скачиваю пакеты заранее"
run sudo apt-get install -y --download-only "${NEW_PKGS[@]}" "${REMOVE[@]}"
ok "Всё скачано"

info "Меняю драйвер (одна транзакция apt)"
run sudo apt-get install -y "${NEW_PKGS[@]}" "${REMOVE[@]}"
run sudo apt-get autoremove -y

info "Проверка"
LEFT=$(dpkg-query -W -f '${db:Status-Abbrev} ${Package}\n' 2>/dev/null \
  | awk '$1 ~ /^ii/ {print $2}' | grep -E 'nvidia' | grep -E -- "-(${OLD_BRANCH_RE})(-|$)" || true)
if [[ -n "$LEFT" && $DRY == 0 ]]; then
  warn "Остались пакеты новой ветки:"; echo "$LEFT" | sed 's/^/      /'
else
  ok "Пакетов ветки 59x+ не осталось"
fi

cat <<EOF

${G}${B}Готово.${N} Теперь:

  1. Перезагрузись:            sudo reboot
  2. Проверь:                  nvidia-smi        (должно быть 580.xxx)
  3. Доделай установку:        bash install_unitree_sim.sh
     (всё готовое пропустит, повторит только пробный запуск)

Важно: в «Программы и обновления → Дополнительные драйверы» не выбирай 595,
иначе Isaac Sim 5.1 снова начнёт падать.
EOF
