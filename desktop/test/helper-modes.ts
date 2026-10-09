// The modes of the helper pkexec runs that each of its two readers is asked about: the install
// script (roots_program, in release/install.sh), which takes the release keys of no other helper,
// and the app (rootsOwn, in src/vm/image.ts), which offers an update only by a helper the script
// takes. One list: an app that takes a helper the script refuses offers an update that cannot be
// applied, and one that refuses a helper the script takes offers none.
//
// The rule is who may write the helper, and whether it is a program. A set-id or a sticky bit
// changes neither: the kernel runs no script as its file's owner or group, pkexec runs this one as
// root whatever its bits, and no one but root may write it. So every mode here is asked with each
// of those bits and with all of them, and answers as it does without.

// What both readers answer for a helper that is root's own file at a mode: taken; refused as one
// that another than root may write; or refused as no program.
export type HelperAnswer = "taken" | "written" | "no program";
const PLAIN: Array<[mode: number, answer: HelperAnswer]> = [
  // As an install leaves it; read-only; root's alone; and one that only its group, or only
  // others, may run: root, whom pkexec runs it as, runs what anyone may.
  [0o755, "taken"], [0o555, "taken"], [0o700, "taken"], [0o500, "taken"], [0o744, "taken"], [0o711, "taken"], [0o511, "taken"], [0o610, "taken"], [0o601, "taken"],
  // Its group may write it; anyone may; both.
  [0o775, "written"], [0o757, "written"], [0o777, "written"], [0o720, "written"], [0o702, "written"],
  // Written by its group or by others, and no program either: the first is what is said.
  [0o664, "written"], [0o602, "written"],
  // Root's alone to write, and no one may run it.
  [0o644, "no program"], [0o444, "no program"], [0o600, "no program"], [0o400, "no program"],
];
// Set-user-id, set-group-id, sticky, and the three at once.
const BITS = [0, 0o4000, 0o2000, 0o1000, 0o7000];
export const HELPER_MODES: Array<[mode: number, answer: HelperAnswer]> = BITS.flatMap((bits) => PLAIN.map(([mode, answer]): [number, HelperAnswer] => [bits | mode, answer]));
