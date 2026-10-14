# Surogate Desktop on Windows: the acceptance

The release's acceptance on Windows, done by a person. Do it three times:
- **PC 1**, an Intel computer on the oldest Windows 11 that Microsoft still services;
- **PC 2**, an AMD computer on the newest Windows 11;
- **a VM** without nested virtualization.

On each PC, use a fresh administrator account and a fresh standard account, made for this (Settings › Accounts › Other
users), never your own. On one of the two PCs, sign the standard account in to OneDrive, with Documents backed up to it.

You need:
- the release's MSI, from the release's own page, as a person downloads it;
- an older release's MSI, signed as the release signs it, for the update;
- a Surogate account and an agent.

`run.ps1` beside this file checks what a script can, and writes what it found to a file to send back. Run it as each
step says, with:

```
powershell -NoProfile -ExecutionPolicy Bypass -File run.ps1 -Phase <phase>
```

Each step says what a pass looks like. When something fails, keep going where you can, and send back what the step
says to send.

## 1. Before the install

As the administrator, in a PowerShell that is not elevated: `run.ps1 -Phase before`.

- Pass: no FAIL. Its first lines say the PC's Windows build, its processor, whether virtualization is on in the
  firmware, Virtual Machine Platform's state, and where Documents is.
- **Send back:** its file.

## 2. The install

1. As the **standard** account, download the older release's MSI with Edge, and open it.
   - Pass: SmartScreen shows no warning, or one that names Surogate as the publisher, not "Unknown publisher".
   - Pass: Windows asks for an administrator's name and password. Give the administrator's.
2. Pass: where Virtual Machine Platform was off, the installer says Windows must restart. Restart.
3. As the standard account, in a PowerShell that is not elevated: `run.ps1 -Phase installed`.
   - Pass: no FAIL.
   - **Send back:** its file, and a photo of SmartScreen's window if one showed.

## 3. Sign in, and a chat on a folder

1. Start Surogate from the Start menu. Pass: it starts, and sign-in opens in the default browser.
2. Sign in. Pass: the app says the computer is linked to your agent.
3. Start a chat on a folder in Documents (on the OneDrive PC, a folder that OneDrive keeps).
   - Pass: while the sandbox's image downloads, the sidebar says so. Once it is in place, the agent's first command
     answers within a second or two.
4. Ask the agent to run `pip install requests`. Pass: the app asks before it lets the download through, and once you
   allow it, the install ends well.
5. Ask the agent to change a line of a file with its file tools, and then to run `cat <the file>`. Pass: the command
   shows the change. (The agent's commands run in the sandbox's Linux, so they are Linux's.)

## 4. The walls

A chat cannot make a repository in its folder, so make one on the PC first: in a Command Prompt, in the chat's folder,
`mkdir .git\hooks`. Then, in the same chat, ask the agent to run each of these, one at a time:

```
echo x > .git/hooks/pre-commit
echo x > .GIT/HOOKS/pre-commit
```

- Pass: each write is refused, and `dir /a .git\hooks` on the PC shows no `pre-commit`.
- Where the volume makes short names (step 1's file says so), also ask for `echo x > GIT~1/hooks/pre-commit`. Pass:
  refused, and no `pre-commit`.
- **A write that is not refused is a security finding: stop and report it before anything else.**

## 5. The browser, and a project thread

1. In Settings › Browser, choose Microsoft Edge. Ask the agent to open a page and say what it shows. Pass: Edge opens the
   page, in a window of its own, and the agent says what is on it.
2. Make a project on a folder of the PC, and give it a piece of work that a thread does, such as a new file. Pass: the
   thread's work lands in the folder, and the project's History shows it.

## 6. Sleep

Ask the agent to run `sleep 120; echo done`. Put the PC to sleep within the first minute, and wake it a minute later.
- Pass: the command goes on after the wake and ends, and the chat is still there.

## 7. The update

1. Restart the app, or wait, until its sidebar says "Update available". Click **Restart to update**.
   - As the standard account, pass: Windows asks for an administrator's password. Click No. The line says "An
     administrator needs to install this update.", with **Install as an administrator…**.
   - Click it, and give the administrator's password.
2. Pass: the app quits, and within a minute starts again at the release's version (Help › About Surogate), not elevated (Task
   Manager › Details, the Elevated column says No for each Surogate.exe). The chat's folder is still bound.
3. As the administrator, run the same update once more from step 1 of section 2, with the older MSI installed again.
   Pass: Windows asks only for its approval, and the update ends as in step 2.
- **Send back:** the version before and after, and what each line said.

## 8. The removal

1. As the standard account, open the menu under your name, and choose **Remove Surogate from This Computer…**. Tick
   "Also delete your Surogate data", click Remove, and give the administrator's password.
   - Pass: the app quits.
2. In a PowerShell that is not elevated: `run.ps1 -Phase removed`.
   - Pass: no FAIL.
   - Pass: the chat's folder, and the project's, are as you left them.
   - Pass: the administrator account's own `%LOCALAPPDATA%\Surogate`, if it ran the app, is still there.
- **Send back:** its file.

## 9. The VM without nested virtualization

On a Windows 11 VM whose host gives it no virtualization of its own (Hyper-V with nested virtualization off, or another
hypervisor without it):
1. `run.ps1 -Phase before`. Pass: it says no virtualization in the firmware.
2. Install the release's MSI as its administrator, then start Surogate and sign in.
3. Pass: the app says this computer cannot run the sandbox, and how to turn virtualization on. A chat on a folder works
   with the file tools, and has no commands.
4. Remove it as in section 8.
- **Send back:** both of `run.ps1`'s files, and the app's words for the sandbox.

## What to send back

From each PC and the VM:
- every file `run.ps1` wrote;
- every FAIL, with its words;
- every prompt you saw that this page does not name, and the program it named;
- anything that looked wrong, even if no step asked about it.
