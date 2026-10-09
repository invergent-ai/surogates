// The modes of the helper pkexec runs that each of its two readers is asked about: the install
// script (roots_program, in release/install.sh), which takes the release keys of no other helper,
// and the app (rootsOwn, in src/vm/image.ts), which offers an update only by a helper the script
// takes. One list: an app that takes a helper the script refuses offers an update that cannot be
// applied, and one that refuses a helper the script takes offers none.
//
// The rule is who may write the helper, whether it is a program, and whether an app can read it:
// the app runs as its user, who is neither root nor in a group of root's, and reads the helper's
// list of release keys from the file. A helper that its user cannot read is one the app can
// offer no update by, so the script takes it for none either: the two answer alike, and an
// install puts one back as it leaves one. A set-id or a sticky bit changes none of the three:
// the kernel runs no script as its file's owner or group, pkexec runs this one as root whatever
// its bits, and no one but root may write it. So every mode here is asked with each of those
// bits and with all of them, and answers as it does without.

// What both readers answer for a helper that is root's own file at a mode: taken; or refused, as
// one that another than root may write, as no program, or as one closed to the app's user. Where
// more than one is so, the first of those is what the app says; the script says one thing of all.
export type HelperAnswer = "taken" | "written" | "no program" | "closed";
const PLAIN: Array<[mode: number, answer: HelperAnswer]> = [
  // As an install leaves it; read-only; and one that only its owner, or only others, may run:
  // root, whom pkexec runs it as, runs what anyone may.
  [0o755, "taken"], [0o555, "taken"], [0o744, "taken"], [0o544, "taken"], [0o745, "taken"], [0o705, "taken"], [0o605, "taken"], [0o614, "taken"],
  // Its group may write it; anyone may; both.
  [0o775, "written"], [0o757, "written"], [0o777, "written"], [0o720, "written"], [0o702, "written"],
  // Written by its group or by others, and no program either: the first is what is said.
  [0o664, "written"], [0o602, "written"],
  // Root's alone to write, and no one may run it.
  [0o644, "no program"], [0o444, "no program"], [0o600, "no program"], [0o400, "no program"],
  // Root's alone to write, a program, and not for others to read: closed to them altogether, to
  // all but its owner's reading, or open to its group's reading alone.
  [0o700, "closed"], [0o500, "closed"], [0o711, "closed"], [0o511, "closed"], [0o610, "closed"], [0o601, "closed"], [0o750, "closed"], [0o741, "closed"],
];
// Set-user-id, set-group-id, sticky, and the three at once.
const BITS = [0, 0o4000, 0o2000, 0o1000, 0o7000];
export const HELPER_MODES: Array<[mode: number, answer: HelperAnswer]> = BITS.flatMap((bits) => PLAIN.map(([mode, answer]): [number, HelperAnswer] => [bits | mode, answer]));
