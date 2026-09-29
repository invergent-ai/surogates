#!/usr/bin/env bash
# usage: create.sh <vm-name> <cloud-image-url>
# The base image goes into libvirt's `default` pool through libvirtd, so a member
# of the libvirt group needs no sudo; virt-install creates the overlay disk.
set -euo pipefail
NAME=$1
IMAGE_URL=$2
KEY="${SSH_PUBKEY:-$HOME/.ssh/id_ed25519.pub}"
C=qemu:///system
CACHE="$HOME/.cache/surogate-spike-images"
mkdir -p "$CACHE"
LOCAL="$CACHE/$(basename "$IMAGE_URL")"
[ -f "$LOCAL" ] || curl -fL -o "$LOCAL" "$IMAGE_URL"
BASE_NAME="spike-base-$(basename "$IMAGE_URL" .img).qcow2"
if ! virsh -c "$C" vol-info --pool default "$BASE_NAME" >/dev/null 2>&1; then
  virsh -c "$C" vol-create-as default "$BASE_NAME" "$(stat -c %s "$LOCAL")" --format raw
  virsh -c "$C" vol-upload --pool default "$BASE_NAME" "$LOCAL"
  virsh -c "$C" pool-refresh default
fi
BASE_PATH=$(virsh -c "$C" vol-path --pool default "$BASE_NAME")
# Own network: its dnsmasq forwards to public resolvers instead of reading the
# host's /etc/resolv.conf, and it leaves the host's `default` network untouched.
if ! virsh -c "$C" net-info spike-net >/dev/null 2>&1; then
  NETXML=$(mktemp)
  cat > "$NETXML" <<'EOF'
<network>
  <name>spike-net</name>
  <forward mode='nat'/>
  <bridge name='virbr-spike' stp='on' delay='0'/>
  <dns><forwarder addr='1.1.1.1'/><forwarder addr='8.8.8.8'/></dns>
  <ip address='192.168.150.1' netmask='255.255.255.0'>
    <dhcp><range start='192.168.150.10' end='192.168.150.100'/></dhcp>
  </ip>
</network>
EOF
  virsh -c "$C" net-define "$NETXML"
fi
NETINFO=$(virsh -c "$C" net-info spike-net)   # no pipe into grep -q: pipefail + SIGPIPE
[[ $NETINFO =~ Active:[[:space:]]+yes ]] || virsh -c "$C" net-start spike-net
USERDATA=$(mktemp)
cat > "$USERDATA" <<EOF
#cloud-config
users:
  - name: spike
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    ssh_authorized_keys:
      - $(cat "$KEY")
EOF
virt-install --connect "$C" --name "$NAME" --memory 6144 --vcpus 4 \
  --disk pool=default,size=30,format=qcow2,backing_store="$BASE_PATH",backing_format=qcow2,bus=virtio \
  --import --os-variant linux2022 --network network=spike-net --cloud-init user-data="$USERDATA" \
  --noautoconsole --graphics none
echo "wait ~60s, then: virsh -c $C domifaddr $NAME"
