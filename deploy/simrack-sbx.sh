#!/bin/bash
# Optional Proxmox hookscript for SimRack sandbox guests (local:snippets/simrack-sbx.sh).
# SimRack opens LACP itself after every start it makes. Set this on the vJunos
# template once, as root (qm set <template> --hookscript local:snippets/simrack-sbx.sh),
# so starts from the Proxmox GUI keep it too; every clone copies it.
# Lets LLDP and LACP cross the sandbox cable bridges (sbx*). Any other bridge,
# the live lab's included, is left alone. If the lab profile changes
# [sandbox] bridge_prefix, change the sbx* pattern below to match.
vmid=$1; phase=$2
[ "$phase" = "post-start" ] || exit 0
sleep 2
for tap in /sys/class/net/tap${vmid}i*; do
  [ -e "$tap" ] || continue
  t=$(basename "$tap")
  br=$(basename "$(readlink -f "$tap/brport/bridge" 2>/dev/null)")
  case "$br" in sbx*)
    echo 65528 > /sys/class/net/$br/bridge/group_fwd_mask
    echo 16388 > /sys/class/net/$t/brport/group_fwd_mask ;;
  esac
done
exit 0
