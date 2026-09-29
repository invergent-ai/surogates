#!/usr/bin/env bash
set -euo pipefail
sudo apt-get update
sudo apt-get install -y bubblewrap socat ripgrep xvfb xauth curl git jq rsync python3-venv \
  libnss3-tools libgtk-3-0t64 libnss3 libxss1 libasound2t64 libgbm1 dbus-user-session
curl -fsSL -o /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo apt-get install -y /tmp/chrome.deb
curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | sudo gpg --dearmor -o /usr/share/keyrings/microsoft-edge.gpg
echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft-edge.gpg] https://packages.microsoft.com/repos/edge stable main" \
  | sudo tee /etc/apt/sources.list.d/microsoft-edge.list
sudo apt-get update && sudo apt-get install -y microsoft-edge-stable
sudo snap install chromium   # Ubuntu's Chromium is a snap: used to check it is reported unsupported
curl -fsSL -o- https://raw.githubusercontent.com/nvm-sh/nvm/master/install.sh | bash
bash -lic 'nvm install 22 && node --version'
sysctl kernel.apparmor_restrict_unprivileged_userns
