# Surogate Desktop: troubleshooting

## When Surogate Desktop says to remove it and install it again

An update, the install script and its `--version` can each stop with a line that ends "remove
Surogate Desktop with --uninstall, and install it again". The app shows the same line after
"Surogate could not install its update:". It is about two files that only root writes, and the
folder of the release they belong to:

    /opt/surogate/bin/surogate-apply-update    the helper that an update runs as root, which lists the release keys this computer trusts
    /opt/surogate/bin/release.json             its mark: the manifest of the release that the helper is of
    /opt/surogate/versions/<version>/          that release's folder, with a copy of the helper and of the manifest in it

The line means that one of them is not as an install or an update leaves it. No install, update
or rollback leaves them so, wherever it is stopped. The states below need root's own hand on
those files, a damaged disk, an install script from before the mark was written, or a removal
that was stopped before its end. Nothing short of a removal mends them, because what is damaged
is what says which release keys this computer trusts. Do what the line says:

    curl -fsSL https://surogate.ai/desktop/install.sh | bash -s -- --uninstall
    curl -fsSL https://surogate.ai/desktop/install.sh | bash

The removal takes the app away for every user of the computer. It asks before it deletes your
sign-in, your device token and your browser profiles, and chat folders stay.

The script has four such lines. These are the states each is known to be said of; the list is
of what was measured, and the script may refuse others in the same words. The first:

    /opt/surogate/bin/surogate-apply-update is not as Surogate Desktop's install leaves it: remove Surogate Desktop with --uninstall, and install it again

- the helper is gone, and a version is installed, as a removal leaves it that was stopped before
  its last step;
- the helper is a link, or a folder stands in its place;
- the helper is not root's own program that every user may read: it is another user's, its
  group or others may write it, no one may run it, or others may not read it. The app reads its
  release keys from it as its user, so one closed to others is none to the app, and the script
  takes it for none either. At any other mode it is taken, read-only too;
- the folder of the release that the mark names has lost its own copy of the helper,
  `bin/surogate-apply-update`, and the install is otherwise whole;
- a link is where that copy was;
- the same loss after a rollback, where that folder is not the running version's;
- an update was stopped half way, between the mark's writing and the helper's, and the update's
  folder has lost its program, `surogate`;
- an update was stopped so, and the `release.json` in its folder is not root's own file at mode
  0644;
- an update was stopped so, and its own copy of the helper is another user's, or one that its
  group or others may write, or has a set-id bit;
- an update was stopped so, and its folder has lost its own copy of the helper;
- the mark names an older release than the helper is of.

The second:

    /opt/surogate/bin/surogate-apply-update lists no release key: remove Surogate Desktop with --uninstall, and install it again

- the helper lists no release key, or its list is not written in the one form that a list has
  (the form is said at the list, in `release/install.sh`). A release job writes and signs no
  manifest for such a script.

The third:

    /opt/surogate/bin/release.json does not say which release /opt/surogate/bin/surogate-apply-update is of: remove Surogate Desktop with --uninstall, and install it again

- the mark is gone;
- the mark is not root's own file at mode 0644, or names no release.

The fourth, where nothing is installed yet and something that is no file, a link or a folder,
stands where the mark goes:

    /opt/surogate/bin/release.json is not as Surogate Desktop's install leaves it: remove Surogate Desktop with --uninstall, and install it again

One state needs a removal and is told nothing of one: a folder where the link
`/opt/surogate/current` should be. An update, and the install script, then end "stopped, as this
step failed: mv -T ...", with the new version's helper and mark already in place. Remove
Surogate Desktop and install it again there too.

An update that was only stopped half way is none of these: the next update, or the install
script, finishes it. And a line that ends "run Surogate Desktop's install script again" means
what it says, where the base's newest release is the installed one or a newer: the install
script then unpacks again a version's folder that has lost its program or its `release.json`.
Where the installed version is newer than the base's newest, the script keeps it as it is
("kept the installed ..., newer than the server's ..."), and mends nothing of it.

A different line is said of a release that no release key of this computer's has signed. The
install script says it, of the base's newest release:

    <base>/desktop/latest.json is not signed by a release key this computer trusts: nothing was installed, and Surogate Desktop stays at its version. If Surogate's release key has changed since this computer's last update, run Surogate Desktop's install script with --version of the release that brought the new key

and the app says it in the same words, in its sidebar: of the newest release, once that has stood
for a day of failed checks, and of an update that its own helper refuses for it:

    Surogate's newest release is not signed by a release key this computer trusts: nothing was installed, and Surogate Desktop stays at its version. If Surogate's release key has changed since this computer's last update, run Surogate Desktop's install script with --version of the release that brought the new key
    Surogate could not install its update: the release's manifest is not signed by a release key this computer trusts: nothing was installed, and Surogate Desktop stays at its version. If Surogate's release key has changed since this computer's last update, run Surogate Desktop's install script with --version of the release that brought the new key

Nothing was installed, and Surogate goes on at the version it has. The line cannot tell why the
release is not signed: a release from after a change of the release key, on a computer that took
no update while the key changed, reads the same as one that someone else put on the server.

A key is changed over two releases: one that the old key signs and that lists the new key beside
it, and then the first that the new key alone signs. A computer that never installed the first
of the two knows the old key alone, and takes nothing that the new one signs. What mends it is
that first release, which this computer's own key signed, and then an update as ever:

    curl -fsSL https://surogate.ai/desktop/install.sh | bash -s -- --version <that release>
    curl -fsSL https://surogate.ai/desktop/install.sh | bash

Which release that is, the line cannot say: it is the one whose notes say that the release key
changes. That way is safe whatever the cause, since this computer's own keys check what it
installs.

Removing Surogate Desktop and installing it again also ends the line, and is not safe whatever
the cause: a removal forgets the keys this computer trusts, and the install after it trusts the
keys that the server's install script lists. Do it only once Surogate's release notes say that the
release key has changed, read somewhere other than the server the line names.

A rollback (`--version`) to a release that is not signed is told so plainly, with no way on. And
a release that a key signed which Surogate has since retired is told as that: "... is signed by a
release key that Surogate has retired, which this computer no longer trusts".

