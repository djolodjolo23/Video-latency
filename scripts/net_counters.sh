#!/usr/bin/env bash
set -euo pipefail

TABLE="qoe_counters"
CHAIN_IN="qoe_in"
CHAIN_OUT="qoe_out"
IPT_CHAIN="QOE_COUNTERS"

usage() {
  cat <<'USAGE'
Usage:
  scripts/net_counters.sh setup --iface <ifname> --clients <ip1,ip2,...>
  scripts/net_counters.sh show
  scripts/net_counters.sh summary
  scripts/net_counters.sh reset
  scripts/net_counters.sh teardown

Notes:
- Requires sudo.
- Tracks IPv4 traffic to/from the given client IPs on the specified interface.
USAGE
}

require_root() {
  if [[ "$EUID" -ne 0 ]]; then
    echo "This script needs sudo/root." >&2
    exit 1
  fi
}

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

setup_nft() {
  local iface="$1"
  local clients_csv="$2"
  IFS=',' read -r -a clients <<< "$clients_csv"

  nft list table inet "$TABLE" >/dev/null 2>&1 || nft add table inet "$TABLE"
  nft list chain inet "$TABLE" "$CHAIN_IN" >/dev/null 2>&1 || \
    nft add chain inet "$TABLE" "$CHAIN_IN" "{ type filter hook input priority 0 ; policy accept; }"
  nft list chain inet "$TABLE" "$CHAIN_OUT" >/dev/null 2>&1 || \
    nft add chain inet "$TABLE" "$CHAIN_OUT" "{ type filter hook output priority 0 ; policy accept; }"

  nft flush chain inet "$TABLE" "$CHAIN_IN"
  nft flush chain inet "$TABLE" "$CHAIN_OUT"

  for client in "${clients[@]}"; do
    nft add rule inet "$TABLE" "$CHAIN_IN" iifname "$iface" ip saddr "$client" counter
    nft add rule inet "$TABLE" "$CHAIN_OUT" oifname "$iface" ip daddr "$client" counter
  done
}

setup_iptables() {
  local iface="$1"
  local clients_csv="$2"
  IFS=',' read -r -a clients <<< "$clients_csv"

  iptables -N "$IPT_CHAIN" 2>/dev/null || true
  iptables -C INPUT -i "$iface" -j "$IPT_CHAIN" >/dev/null 2>&1 || \
    iptables -I INPUT 1 -i "$iface" -j "$IPT_CHAIN"
  iptables -C OUTPUT -o "$iface" -j "$IPT_CHAIN" >/dev/null 2>&1 || \
    iptables -I OUTPUT 1 -o "$iface" -j "$IPT_CHAIN"

  iptables -F "$IPT_CHAIN"
  for client in "${clients[@]}"; do
    iptables -A "$IPT_CHAIN" -s "$client" -j RETURN
    iptables -A "$IPT_CHAIN" -d "$client" -j RETURN
  done
}

reset_nft() {
  nft reset counters chain inet "$TABLE" "$CHAIN_IN" >/dev/null 2>&1 || true
  nft reset counters chain inet "$TABLE" "$CHAIN_OUT" >/dev/null 2>&1 || true
}

reset_iptables() {
  iptables -Z "$IPT_CHAIN" >/dev/null 2>&1 || true
}

show_nft() {
  nft list table inet "$TABLE"
}

show_iptables() {
  iptables -v -n -L "$IPT_CHAIN"
}

summary_nft() {
  local in_data out_data in_packets in_bytes out_packets out_bytes
  in_data=$( (nft list chain inet "$TABLE" "$CHAIN_IN" 2>/dev/null || true) | awk '
    /counter packets/ {for (i=1;i<=NF;i++) {if ($i=="packets") p+=$(i+1); if ($i=="bytes") b+=$(i+1)}}
    END {printf "%s %s", p+0, b+0}')
  out_data=$( (nft list chain inet "$TABLE" "$CHAIN_OUT" 2>/dev/null || true) | awk '
    /counter packets/ {for (i=1;i<=NF;i++) {if ($i=="packets") p+=$(i+1); if ($i=="bytes") b+=$(i+1)}}
    END {printf "%s %s", p+0, b+0}')
  in_packets=$(echo "$in_data" | awk '{print $1}')
  in_bytes=$(echo "$in_data" | awk '{print $2}')
  out_packets=$(echo "$out_data" | awk '{print $1}')
  out_bytes=$(echo "$out_data" | awk '{print $2}')
  echo "in_packets=${in_packets} in_bytes=${in_bytes} out_packets=${out_packets} out_bytes=${out_bytes}"
}

summary_iptables() {
  (iptables -v -n -L "$IPT_CHAIN" 2>/dev/null || true) | awk '
    NR>2 && $1 ~ /^[0-9]+$/ {
      pkts=$1; bytes=$2; inif=$6; outif=$7;
      if (inif != "*") {in_packets+=pkts; in_bytes+=bytes}
      if (outif != "*") {out_packets+=pkts; out_bytes+=bytes}
    }
    END {
      printf "in_packets=%s in_bytes=%s out_packets=%s out_bytes=%s\n",
        in_packets+0, in_bytes+0, out_packets+0, out_bytes+0
    }'
}

teardown_nft() {
  nft delete chain inet "$TABLE" "$CHAIN_IN" >/dev/null 2>&1 || true
  nft delete chain inet "$TABLE" "$CHAIN_OUT" >/dev/null 2>&1 || true
  nft delete table inet "$TABLE" >/dev/null 2>&1 || true
}

teardown_iptables() {
  iptables -D INPUT -j "$IPT_CHAIN" >/dev/null 2>&1 || true
  iptables -D OUTPUT -j "$IPT_CHAIN" >/dev/null 2>&1 || true
  iptables -F "$IPT_CHAIN" >/dev/null 2>&1 || true
  iptables -X "$IPT_CHAIN" >/dev/null 2>&1 || true
}

main() {
  if [[ $# -lt 1 ]]; then
    usage
    exit 1
  fi

  local action="$1"
  shift

  case "$action" in
    setup)
      local iface=""
      local clients=""
      while [[ $# -gt 0 ]]; do
        case "$1" in
          --iface)
            iface="$2"
            shift 2
            ;;
          --clients)
            clients="$2"
            shift 2
            ;;
          *)
            echo "Unknown option: $1" >&2
            usage
            exit 1
            ;;
        esac
      done

      if [[ -z "$iface" || -z "$clients" ]]; then
        echo "Missing --iface or --clients." >&2
        usage
        exit 1
      fi

      require_root
      if have_cmd nft; then
        setup_nft "$iface" "$clients"
      else
        setup_iptables "$iface" "$clients"
      fi
      ;;
    show)
      require_root
      if have_cmd nft; then
        show_nft
      else
        show_iptables
      fi
      ;;
    summary)
      require_root
      if have_cmd nft; then
        summary_nft
      else
        summary_iptables
      fi
      ;;
    reset)
      require_root
      if have_cmd nft; then
        reset_nft
      else
        reset_iptables
      fi
      ;;
    teardown)
      require_root
      if have_cmd nft; then
        teardown_nft
      else
        teardown_iptables
      fi
      ;;
    *)
      echo "Unknown action: $action" >&2
      usage
      exit 1
      ;;
  esac
}

main "$@"
