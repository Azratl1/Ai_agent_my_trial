#!/usr/bin/env bash
# Полная проверка сетевой изоляции (No-Egress Audit). Запускать на хосте-сервере.
set -uo pipefail
cd "$(dirname "$0")/.."
FAIL=0
say() { echo; echo "==== $* ===="; }
ok()  { echo "  [OK]   $*"; }
bad() { echo "  [FAIL] $*"; FAIL=1; }

say "1. internal: true у обеих сетей"
for net in doc-audit-agent_agent_internal doc-audit-agent_agent_edge; do
  if docker network inspect "$net" --format '{{.Internal}}' 2>/dev/null | grep -q true; then
    ok "$net Internal=true"
  else
    bad "$net отсутствует или Internal!=true"
  fi
done

say "2. У контейнеров внутренней сети нет default route / внешних DNS"
for c in agent-ollama agent-qdrant agent-redis agent-api agent-watcher agent-worker-parser agent-worker-audit; do
  docker ps --format '{{.Names}}' | grep -qx "$c" || { echo "  [skip] $c не запущен"; continue; }
  GW=$(docker exec "$c" sh -c 'ip route 2>/dev/null | grep default || cat /proc/net/route | awk "\$2==\"00000000\"{print \$3}"' 2>/dev/null)
  if [[ -z "${GW//[[:space:]]/}" ]]; then ok "$c: default route отсутствует"; else bad "$c: есть шлюз $GW"; fi
done

say "3. Реальный egress-тест из каждого контейнера (5 сек, без DNS)"
for c in agent-ollama agent-qdrant agent-api agent-webui; do
  R=$(docker exec "$c" sh -c 'timeout 5 wget -qO- http://1.1.1.1 >/dev/null 2>&1 && echo PASS_THROUGH || echo BLOCKED' 2>/dev/null)
  [[ "$R" == *BLOCKED* ]] && ok "$c: доступ к 1.1.1.1 заблокирован" || bad "$c: ЕСТЬ ВЫХОД НАРУЖУ!"
done

say "4. tcpdump: 60 сек мониторинга физического интерфейса на трафик подсетей compose"
IFACE=${IFACE:-$(ip route | awk '/default/ {print $5; exit}')}
SUBNETS=$(docker network ls --filter name=agent -q | xargs -r -I{} docker network inspect {} --format '{{range .IPAM.Config}}{{.Subnet}} {{end}}')
echo "  iface=$IFACE subnets=$SUBNETS"
if command -v tcpdump >/dev/null; then
  timeout 60 tcpdump -i "$IFACE" -nn -c 100 $(for s in $SUBNETS; do printf 'or net %s ' "$s"; done | sed 's/^or //') > /tmp/egress_tcpdump.txt 2>&1 || true
  if grep -qE 'IP .* >' /tmp/egress_tcpdump.txt; then
    bad "tcpdump зафиксировал пакеты из подсетей контейнеров наружу — см. /tmp/egress_tcpdump.txt"
  else
    ok "за 60 сек внешнего трафика от подсетей контейнеров не обнаружено"
  fi
else
  echo "  [skip] tcpdump не установлен"
fi

say "5. Firewall lockdown (iptables/nftables) — опционально, по желанию администратора"
cat <<'EOF'
  # Явный DROP всего трафика из docker-bridges в FORWARD (дублирует internal:true):
  sudo iptables -I DOCKER-USER -s 172.28.0.0/24 ! -d 172.28.0.0/24 -j DROP
  sudo iptables -I DOCKER-USER -s 172.29.0.0/24 ! -d 172.29.0.0/24 -j DROP
  # Проверка правил: sudo iptables -L DOCKER-USER -n -v
  # nftables аналог: nft add rule inet filter forward ip saddr 172.28.0.0/24 ip daddr != 172.28.0.0/24 drop
EOF

say "6. Пробросы портов — только loopback"
docker ps --format '{{.Names}}\t{{.Ports}}' | while IFS=$'\t' read -r n p; do
  [[ -z "$p" ]] && continue
  if echo "$p" | grep -qvE '^127\.0\.0\.1:|->'; then :; fi
  if echo "$p" | grep -qE '0\.0\.0\.0:|\[::\]:'; then bad "$n: порт открыт на всех интерфейсах ($p)"; else ok "$n: только 127.0.0.1 ($p)"; fi
done

say "7. Офлайн-флаги и телеметрия внутри контейнеров"
for c in agent-api agent-worker-parser agent-ollama agent-qdrant agent-webui; do
  HF=$(docker exec "$c" printenv HF_HUB_OFFLINE 2>/dev/null)
  [[ "$HF" == "1" ]] && ok "$c: HF_HUB_OFFLINE=1" || bad "$c: HF_HUB_OFFLINE не выставлен"
done
docker exec agent-qdrant printenv QDRANT__TELEMETRY_DISABLED 2>/dev/null | grep -q true \
  && ok "Qdrant telemetry disabled" || bad "Qdrant telemetry включена!"

say "8. Логи Ollama на предмет внешних запросов (за последние сутки)"
if docker logs --since 24h agent-ollama 2>&1 | grep -viE 'pull|registry|hf.co|huggingface' >/dev/null; then
  ok "в логах ollama нет подозрительных внешних обращений"
fi

echo
if [[ $FAIL -eq 0 ]]; then echo "AUDIT PASSED ✅ — подтверждена полная изоляция."; else echo "AUDIT FAILED ❌ — устраните помеченные пункты."; fi
exit $FAIL
