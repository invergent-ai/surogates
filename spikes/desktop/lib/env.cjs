const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

let cachedPath;

// PATH from the user's interactive login shell, so nvm and pyenv shims resolve.
function loginPath() {
  if (cachedPath) return cachedPath;
  const out = execFileSync(process.env.SHELL || '/bin/bash', ['-lic', 'printf "__P__%s__P__" "$PATH"'], {
    encoding: 'utf8', timeout: 10000, stdio: ['ignore', 'pipe', 'ignore'],
  });
  cachedPath = /__P__(.*)__P__/.exec(out)[1];
  return cachedPath;
}

// Per-session temp folder in the design's location.
function sessionTmp(name) {
  const dir = path.join(os.homedir(), '.local/share/surogate/tmp', `${name}-${process.pid}`);
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

function buildEnv({ tmp }) {
  for (const d of ['cache', 'npm', 'pip', 'uv']) fs.mkdirSync(path.join(tmp, d), { recursive: true });
  return {
    HOME: os.homedir(),
    LANG: process.env.LANG || 'C.UTF-8',
    TMPDIR: tmp,
    XDG_CACHE_HOME: path.join(tmp, 'cache'),
    npm_config_cache: path.join(tmp, 'npm'),
    PIP_CACHE_DIR: path.join(tmp, 'pip'),
    UV_CACHE_DIR: path.join(tmp, 'uv'),
    PATH: loginPath(),
  };
}

module.exports = { buildEnv, loginPath, sessionTmp };
