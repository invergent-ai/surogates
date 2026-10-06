# Surogate Desktop

The desktop app that lets an agent's file, terminal and browser tools work on a
folder of your computer, while the agent's reasoning stays on the server.

The device-link protocol is the module docstring of `surogates/devices/link.py`.

    npm install
    npm run electron:install   # Electron 44 ships no postinstall: this fetches its binary
    npm test
    npm run typecheck

The shell, in development:

    npm start                  # builds, then runs Electron with ELECTRON_RUN_AS_NODE cleared
    npm run test:e2e           # the shell end to end, under xvfb-run, with its sandbox on

Its state lives under `$XDG_DATA_HOME/surogate` (`~/.local/share/surogate`):
Electron's own data, the agent, the device's credential and journal, and the
window's place. No chat's folder may hold it.
