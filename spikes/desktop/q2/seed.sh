#!/usr/bin/env bash
set -euo pipefail
mkdir -p ~/Documents ~/.ssh ~/spike-roots/A "$HOME/spike-roots/B My Files ü"
echo private > ~/Documents/private.txt
echo secret > ~/.ssh/spike_secret && chmod 600 ~/.ssh/spike_secret
echo seedA > ~/spike-roots/A/seed.txt
echo seedB > "$HOME/spike-roots/B My Files ü/seed.txt"
sudo mkdir -p /mnt/spike-data
if ! mountpoint -q /mnt/spike-data; then
  truncate -s 512M ~/spike-data.img && mkfs.ext4 -q -F ~/spike-data.img
  sudo mount -o loop ~/spike-data.img /mnt/spike-data
fi
sudo mkdir -p /mnt/spike-data/C && sudo chown "$USER" /mnt/spike-data/C
echo seedC > /mnt/spike-data/C/seed.txt
ln -sfn /mnt/spike-data/C ~/spike-roots/C-link
