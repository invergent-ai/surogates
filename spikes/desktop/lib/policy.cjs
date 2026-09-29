const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

// System runtime paths installed tools need. Q2 records what breaks without each.
const SYSTEM_READ = ['/usr', '/bin', '/sbin', '/lib', '/lib64', '/etc', '/opt', '/proc', '/sys', '/dev', '/run'];

// Specific toolchain folders only; never a whole tree like ~/.cargo that can hold credentials.
function toolchainDirs() {
  const h = os.homedir();
  return ['.nvm', '.pyenv', '.rustup', '.cargo/bin', '.local/bin', '.local/lib', 'go', '.bun', '.deno', '.sdkman']
    .map((d) => path.join(h, d))
    .filter((p) => fs.existsSync(p));
}

// variant 'root-deny': deny every read, then re-admit. 'home-deny': deny the home
// folder and other user-data mounts only. Q2 records which one srt accepts.
function buildPolicy({ folder, tmp, allowedDomains, variant = 'root-deny', extraRead = [] }) {
  const root = fs.realpathSync(folder);
  // srt runs its bundled apply-seccomp helper inside the sandbox, so the app's
  // install folder (read-only, no secrets) must stay visible there.
  const appDir = path.dirname(process.execPath);
  const denyRead = variant === 'root-deny'
    ? ['/']
    : [os.homedir(), '/mnt', '/media', '/srv', '/tmp', '/var/tmp'];
  const ownBwrap = path.join(appDir, 'bin/bwrap');
  return {
    ...(process.env.SPIKE_SYSTEM_BWRAP || !fs.existsSync(ownBwrap) ? {} : { bwrapPath: ownBwrap }),
    network: { allowedDomains, deniedDomains: [], allowLocalBinding: true },
    filesystem: {
      denyRead,
      allowRead: [...SYSTEM_READ, appDir, root, tmp, ...toolchainDirs(), ...extraRead],
      allowWrite: [root, tmp],
      denyWrite: [],
    },
  };
}

module.exports = { buildPolicy, SYSTEM_READ, toolchainDirs };
