#!/bin/sh
# Copy netmap to a server with scp and (re)start it as a systemd user service.
#
#   deploy.sh HOST CONFIG [FILE ...]
#
# CONFIG is installed as ~/netmap/config.yaml. Each FILE is a path relative to CONFIG's folder
# (e.g. ca/root.crt for ca_file) and lands at the same relative path under ~/netmap.
# One-time on the server: sudo loginctl enable-linger $USER
set -eu
if [ $# -lt 2 ]; then
    echo "usage: $0 HOST CONFIG [FILE ...]" >&2
    exit 2
fi
HOST=$1
CONFIG=$2
shift 2
here=$(cd "$(dirname "$0")" && pwd)
confdir=$(cd "$(dirname "$CONFIG")" && pwd)
stage=$(mktemp -d)
trap 'rm -rf "$stage" "$stage.tgz"' EXIT

(cd "$here" && tar cf - --exclude=__pycache__ netmap requirements.txt run.sh deploy/netmap.service) |
    tar xf - -C "$stage"
cp "$CONFIG" "$stage/config.yaml"
for f in "$@"; do
    (cd "$confdir" && tar cf - "$f") | tar xf - -C "$stage"
done
tar czf "$stage.tgz" -C "$stage" .
scp -q "$stage.tgz" "$HOST:/tmp/netmap-deploy.tgz"

ssh "$HOST" 'set -e
mkdir -p ~/netmap ~/.config/systemd/user
rm -rf ~/netmap/netmap
tar xzf /tmp/netmap-deploy.tgz -C ~/netmap
rm /tmp/netmap-deploy.tgz
chmod 600 ~/netmap/config.yaml
chmod +x ~/netmap/run.sh
cp ~/netmap/deploy/netmap.service ~/.config/systemd/user/netmap.service
systemctl --user daemon-reload
systemctl --user enable --quiet netmap
systemctl --user restart netmap
sleep 3
systemctl --user --no-pager --lines=15 status netmap'
