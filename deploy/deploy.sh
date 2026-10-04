#!/bin/sh
# Deploy LabFront to a Proxmox host and (re)start the service.
# Run from the project root:  LABFRONT_HOST=<ssh host> ./deploy/deploy.sh
# A lab-profile.toml in the project root is copied too; see README.md.
set -e
HOST="${LABFRONT_HOST:?Set LABFRONT_HOST to the Proxmox host, as ssh knows it}"
DEST="${LABFRONT_DEST:-/opt/labfront}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> copying to $HOST:$DEST"
ssh -o BatchMode=yes "$HOST" "mkdir -p $DEST/state"
COPYFILE_DISABLE=1 tar -C "$ROOT" -cf - --exclude=__pycache__ --exclude=state --exclude=.git --exclude='.DS_Store' --exclude='._*' --exclude=labfront.env . \
  | ssh -o BatchMode=yes "$HOST" "tar -C $DEST -xf - && find $DEST -name '._*' -type f -delete"

echo "==> running the test suite on $HOST (a failure stops the deploy before the restart)"
ssh -o BatchMode=yes "$HOST" "cd $DEST && python3 -m unittest discover -s tests -t . > /tmp/labfront-tests.log 2>&1; rc=\$?; tail -3 /tmp/labfront-tests.log; exit \$rc"

echo "==> installing the sandbox hookscript (sbx* bridges only)"
scp -q "$ROOT/deploy/labfront-sbx.sh" "$HOST:/var/lib/vz/snippets/labfront-sbx.sh"
ssh -o BatchMode=yes "$HOST" "chmod 755 /var/lib/vz/snippets/labfront-sbx.sh"

if [ -f "$ROOT/deploy/labfront.service" ]; then
  echo "==> installing the systemd unit"
  scp -q "$ROOT/deploy/labfront.service" "$HOST:/etc/systemd/system/labfront.service"
fi

# enable --now does nothing to a running service, so restart to load the new code.
echo "==> restarting labfront"
ssh -o BatchMode=yes "$HOST" 'systemctl daemon-reload && systemctl enable labfront && systemctl restart labfront && sleep 1 && systemctl --no-pager --lines=3 status labfront'

echo "==> smoke test"
ssh -o BatchMode=yes "$HOST" 'curl -s -o /dev/null -w "GET /            -> %{http_code}\n" http://127.0.0.1:8787/; curl -s http://127.0.0.1:8787/api/state | python3 -c "import json,sys; d=json.load(sys.stdin); print(\"free RAM MB:\", d[\"host\"][\"free_ram_mb\"]); print(\"recipes:\", [r[\"name\"] for r in d[\"recipes\"]]); print(\"mist:\", d[\"mist\"]); print(\"profile:\", d[\"profile\"]); print(\"sandboxes:\", len(d[\"sandboxes\"]))"'
echo "==> tunnel: ssh -N -L 8787:127.0.0.1:8787 $HOST"
