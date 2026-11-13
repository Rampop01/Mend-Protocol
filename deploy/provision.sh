#!/usr/bin/env bash
# Sets up Mend on a fresh Ubuntu 24.04 server. Run as root:
#   bash provision.sh <public-hostname>
# Expects /root/mend.env and /root/testnet.json to have been copied over first.
set -euo pipefail
HOST="${1:?usage: provision.sh <public-hostname>}"

export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q python3-venv python3-pip git docker.io debian-keyring debian-archive-keyring apt-transport-https curl gnupg
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -q && apt-get install -y -q caddy
fi
systemctl enable --now docker

id mend >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin mend
usermod -aG docker mend

if [ -d /opt/mend/.git ]; then
  git -C /opt/mend pull --ff-only
else
  git clone https://github.com/Rampop01/Mend-Protocol.git /opt/mend
fi
install -d -o mend -g mend -m 700 /opt/mend/.mend /opt/mend/data
install -o mend -g mend -m 600 /root/mend.env /opt/mend/.env
[ -f /opt/mend/.mend/testnet.json ] || install -o mend -g mend -m 600 /root/testnet.json /opt/mend/.mend/testnet.json
chown -R mend:mend /opt/mend

sudo -u mend python3 -m venv /opt/mend/.venv
sudo -u mend /opt/mend/.venv/bin/pip install -q --upgrade pip
sudo -u mend /opt/mend/.venv/bin/pip install -q -e /opt/mend

docker build -q -t mend-sandbox:latest -f /opt/mend/deploy/sandbox.Dockerfile /opt/mend/deploy

cat > /etc/caddy/Caddyfile <<CADDY
$HOST {
    encode gzip
    reverse_proxy 127.0.0.1:8770
}
CADDY
systemctl reload caddy || systemctl restart caddy

install -m 644 /opt/mend/deploy/mend.service /etc/systemd/system/mend.service
systemctl daemon-reload
systemctl enable mend
systemctl restart mend
sleep 3
systemctl --no-pager status mend | head -5
echo "Mend is running at https://$HOST"
