# Surogate Desktop

**Your Surogate agent, working on the files on your computer.**

Surogate Desktop connects your Surogate agent to your own machine. The agent keeps thinking in
the cloud, and its hands work locally: it reads and edits the files in folders you choose, runs
commands, and browses the web in your own browser. You watch it work and step in whenever you want.

## What you can do with it

- **Work on your real files.** Point a chat at a folder, and the agent reads, writes and organizes
  what is in it. That can be documents, spreadsheets, code or data. No uploads and no syncing:
  the work happens where your files already are.
- **Let it run commands, safely.** Builds, scripts, data processing, file conversions: the agent
  runs them in a sandbox on your computer, not on your computer itself.
- **Browse with your own browser.** The agent drives Chrome, Edge, Brave or Vivaldi, already
  installed on your computer, in a profile of its own. Downloads land in the chat's folder.
  Take the browser over at any moment, and hand it back when you are done.
- **Preview what it builds.** When the agent starts a web app in its sandbox, it can open it in
  the browser for you to see, once you allow it.
- **Run several jobs at once.** In a project, each thread works in its own copy of your folders,
  so threads never trip over each other. When a thread's turn ends, its changes land in your
  folder. If someone changed the same file meanwhile, the thread asks you. Press Stop, and the
  turn's changes are taken back.

## Safe by design

- **Nothing runs directly on your computer.** Every command runs in a lightweight virtual machine
  that Surogate Desktop starts for you. A chat's folder joins it only while that chat is
  working, and files you protect stay read-only there.
- **The agent sees only what you give it.** The file tools see the chat's folder and nothing
  else of yours.
- **You decide what reaches the network.** Commands reach the package registries they need,
  and your computer's own services stay out of reach. For any other site, your local network
  included, the agent asks you first. A chat can also be set to work freely.
- **You stay in control of the browser.** In a chat that asks every time, you approve each
  download and each file the agent gives a page. Taking the browser over pauses the agent
  until you hand it back.
- **Signed updates.** Every release is signed, and your computer installs only releases signed
  by Surogate's key.

## Requirements

- Ubuntu 24.04 LTS or later, on a 64-bit Intel or AMD computer.
- Hardware virtualization (Intel VT-x or AMD-V), turned on in the firmware settings. Without it
  Surogate Desktop still works, but runs the agent's commands several times slower.
- For browsing: Google Chrome, Microsoft Edge, Brave or Vivaldi. The Snap build of Chromium is
  not supported.
- A Surogate account.

## Install

    curl -fsSL https://surogate.ai/desktop/install.sh | bash

Then open Surogate Desktop from your applications, and sign in.

Surogate Desktop keeps itself up to date. When a new release is out, it says "Update available",
and **Restart to update** installs it. To go back to an earlier release:

    curl -fsSL https://surogate.ai/desktop/install.sh | bash -s -- --version <release>

To remove it:

    curl -fsSL https://surogate.ai/desktop/install.sh | bash -s -- --uninstall

Removing it asks before it deletes your sign-in and your browser profiles. Your folders are
never touched.

### On a company network

If your network inspects TLS traffic, or your servers use a certificate authority of their own,
give the installer your company's CA certificate:

    curl --cacert company-ca.pem -fsSL https://surogate.ai/desktop/install.sh | bash -s -- --ca-cert company-ca.pem

Surogate Desktop, your Chrome, Edge and Brave, and the commands the agent runs then all trust it.

## Good to know

- **Free-working chats don't ask about files.** In a chat set to work freely, a page's
  downloads are saved and the agent's uploads are made without a prompt. Set the chat to ask
  every time, and you approve each one.
- **A project thread works without your secrets and build output.** A thread's copy of a folder
  leaves out `.env` files, dependency folders such as `node_modules/` and `.venv/`, build
  output, caches and editors' temporary files, and none of them lands from the thread either.
  For work that needs them, such as running the project's own install or tests that read its
  `.env`, use a chat on the folder.
- **Java does not trust your company's CA.** Programs in the sandbox use the system's
  certificates, which include it, but Java keeps its own. Add the CA to Java's store yourself if
  the agent's Java programs need it.
- **A proxy your computer must name is not used for the connection to Surogate.** The app's
  window goes through it. If that proxy is your only way out, the computer cannot be linked.
- **A site can open a preview you allowed.** A link or a redirect on another site can open
  your allowed preview in a tab, as any site can open any address. Nothing else from that site
  reaches your preview.

## If it asks you to reinstall

Rarely, an update or the installer stops with a message that ends "remove Surogate Desktop with
--uninstall, and install it again". It means the files that check updates on your computer are
not as an install leaves them. Do what it says:

    curl -fsSL https://surogate.ai/desktop/install.sh | bash -s -- --uninstall
    curl -fsSL https://surogate.ai/desktop/install.sh | bash

Your folders stay as they are.

[TROUBLESHOOTING.md](TROUBLESHOOTING.md) lists each such message, and what an update says of a release this computer cannot trust.

## What it writes on Windows

On Windows, Surogate Desktop is installed for every user of the computer by its MSI, and writes
these places, and no others:

- **`%ProgramFiles%\Surogate\`**: the app itself, which only administrators can change.
- **`%ProgramData%\Surogate\`**: the install's record of where updates come from, their channel
  and the version installed (`install.json`), and `apply\`, where an update is checked and
  installed from. Only administrators can change either.
- **The registry, for every user:** `HKLM\Software\Surogate`, what the install was given, kept
  for each later install; and `HKLM\Software\Classes\surogate`, which opens `surogate://` links in
  the app.
- **The Start menu's shortcut**, two Windows Firewall rules that keep the file tools off the
  network, and the Surogate VM service, which runs the sandbox for each user who asks for it.
- **`%LOCALAPPDATA%\Surogate\`, for each user**: everything the app keeps for you, your
  sign-in, your settings, the sandbox's disks, your folders' histories, the agent's browser
  profiles, and the updates it downloads.
- **Start at login**, when you turn it on: Windows's list of the apps that start when you sign in.
- **`%SystemRoot%\Temp\`**: Windows Installer's logs of an update (`surogate-update-<version>.log`)
  and of a removal (`surogate-remove.log`).

**Restart to update** asks Windows for an administrator, then quits the app, installs the
update and starts the app again. **Remove Surogate from This Computer…**, in the menu under your
name, removes the app for every user once an administrator approves it, and asks before it
deletes your own `%LOCALAPPDATA%\Surogate\`. It never touches the folders your chats work on, or
another user's data.

## Building from source

    ../images/guest/build.sh     # the sandbox's image, once
    npm install
    npm run electron:install
    npm start
