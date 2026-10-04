#!/bin/sh
# Deploy SimRack to a Proxmox host and (re)start the service.
# Run from the project root:  SIMRACK_HOST=<ssh host> ./deploy/deploy.sh
# state/ (the lab profile, the tokens, the pause) and simrack.env stay on the
# host and are never copied over; see README.md, Deploy.
set -e
HOST="${SIMRACK_HOST:?Set SIMRACK_HOST to the Proxmox host, as ssh knows it}"
DEST="${SIMRACK_DEST:-/opt/simrack}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> copying to $HOST:$DEST"
ssh -o BatchMode=yes "$HOST" "mkdir -p $DEST/state"
COPYFILE_DISABLE=1 tar -C "$ROOT" -cf - --exclude=__pycache__ --exclude=state --exclude=.git --exclude='.DS_Store' --exclude='._*' --exclude=simrack.env . \
  | ssh -o BatchMode=yes "$HOST" "tar -C $DEST -xf - && find $DEST -name '._*' -type f -delete"

echo "==> running the test suite on $HOST (a failure stops the deploy before the restart)"
ssh -o BatchMode=yes "$HOST" "cd $DEST && python3 -m unittest discover -s tests -t . > /tmp/simrack-tests.log 2>&1; rc=\$?; tail -3 /tmp/simrack-tests.log; exit \$rc"

echo "==> copying the optional sandbox hookscript (sbx* bridges only; see README, Deploy)"
ssh -o BatchMode=yes "$HOST" "mkdir -p /var/lib/vz/snippets"
scp -q "$ROOT/deploy/simrack-sbx.sh" "$HOST:/var/lib/vz/snippets/simrack-sbx.sh"
ssh -o BatchMode=yes "$HOST" "chmod 755 /var/lib/vz/snippets/simrack-sbx.sh"

if [ -f "$ROOT/deploy/simrack.service" ]; then
  echo "==> installing the systemd unit"
  scp -q "$ROOT/deploy/simrack.service" "$HOST:/etc/systemd/system/simrack.service"
fi

# enable --now does nothing to a running service, so restart to load the new code.
echo "==> restarting simrack"
ssh -o BatchMode=yes "$HOST" 'systemctl daemon-reload && systemctl enable simrack && systemctl restart simrack && sleep 1 && systemctl --no-pager --lines=3 status simrack'

echo "==> smoke test"
# Anything else on port 8787 answers too, so check it is SimRack's page.
ssh -o BatchMode=yes "$HOST" 'curl -s http://127.0.0.1:8787/ | grep -q "<title>SimRack</title>"' \
  || { echo "GET / did not return the SimRack page: is another service on port 8787?" >&2; exit 1; }
echo "GET /            -> SimRack"
# With SIMRACK_TOKEN in simrack.env, every API call needs it, even on loopback.
# printf is a shell builtin, so the token never shows in the process list.
ssh -o BatchMode=yes "$HOST" "set -a; [ ! -f $DEST/simrack.env ] || . $DEST/simrack.env; set +a; "'printf "Authorization: Bearer %s\n" "${SIMRACK_TOKEN:-}" | curl -s -H @- http://127.0.0.1:8787/api/state | python3 -c "import json,sys; d=json.load(sys.stdin); print(\"free RAM MB:\", d[\"host\"][\"free_ram_mb\"]); print(\"recipes:\", [r[\"name\"] for r in d[\"recipes\"]]); print(\"mist:\", d[\"mist\"]); print(\"profile:\", d[\"profile\"]); print(\"sandboxes:\", len(d[\"sandboxes\"]))"'
echo "==> tunnel: ssh -N -L 8787:127.0.0.1:8787 $HOST"
