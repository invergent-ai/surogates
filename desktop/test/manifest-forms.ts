// The forms of a release's manifest that each of its readers and writers is asked about: the app
// and its root helper (updates-helper-parity.test.ts), and the release job's two steps beside the
// helper (release-publish.test.ts). One list, so that none is asked of one and not of another.

export const SHA = "a".repeat(64);
// A manifest's fields, in the release job's order, with *fields* in place of its own.
export const fields = (changed: Record<string, unknown> = {}, version = "1.2.4") => ({
  version, channel: "stable", platform: "linux", arch: "x64", url: `releases/${version}/surogate-desktop-${version}-linux-x64.tar.gz`, sha256: SHA, size: 171199516, stateSchema: 1, ...changed,
});
// As publish.sh sign writes one: one line, with its newline.
export const line = (changed: Record<string, unknown> = {}, version?: string) => `${JSON.stringify(fields(changed, version))}\n`;
// The same line with *written* where a field's value is, as no JSON.stringify writes a number.
export const written = (field: "size" | "stateSchema", number: string) => line({ [field]: "@" }).replace('"@"', number);
// The same line with a field of its own, which is no release's: *text* as it is where its value goes.
export const own = (text: string) => line({ note: "@" }).replace('"@"', text);
// A number *steps* fields and places down from the manifest, in a field of its own: inside lists,
// or inside objects, or inside each by turns.
export const down = (steps: number, ...around: Array<[open: string, close: string]>) => {
  const inside = Array.from({ length: steps - 1 }, (_, at) => around[at % around.length]!);
  return own(`${inside.map(([open]) => open).join("")}1${inside.map(([, close]) => close).reverse().join("")}`);
};
export const LIST: [string, string] = ["[", "]"];
export const OBJECT: [string, string] = ['{"n":', "}"];
// The line without its closing brace, to write more fields behind in bytes.
export const begun = () => Buffer.from(line().trimEnd().slice(0, -1));

export const TAKEN = `1.2.4 ${SHA} 171199516`;
// Each form, and whether the helper takes it and whether the app does: the same of each, on
// whichever jq this computer has.
export type Form = [name: string, manifest: string | Buffer, helper: boolean, app: boolean];
export const FORMS: Form[] = [
  ["as the release job writes it", line(), true, true],
  ["with spaces around it", ` ${line().trimEnd()} \n`, true, true],
  ["with a carriage return before its newline", `${line().trimEnd()}\r\n`, true, true],
  ["of 4096 bytes", `${line().trimEnd()}${" ".repeat(4095 - line().trimEnd().length)}\n`, true, true],
  ["of 4097 bytes", `${line().trimEnd()}${" ".repeat(4096 - line().trimEnd().length)}\n`, false, false],
  ["without its newline", line().trimEnd(), false, false],
  ["over two lines", line().replace(",", ",\n"), false, false],
  ["as jq writes an object, a field a line", `${JSON.stringify(fields(), null, 2)}\n`, false, false],
  ["with an empty line after it", `${line()}\n`, false, false],
  ["with an empty line before it", `\n${line()}`, false, false],
  ["with a space after its newline", `${line()} `, false, false],
  ["twice on one line", `${line().trimEnd()}${line()}`, false, false],
  ["with a second document after it", `${line().trimEnd()} 1\n`, false, false],
  ["as a list of one", `[${line().trimEnd()}]\n`, false, false],
  ["null", "null\n", false, false],
  ["a number", "7\n", false, false],
  ["a word", '"1.2.4"\n', false, false],
  ["no JSON", "<html>\n", false, false],
  ["an empty object", "{}\n", false, false],
  ["nothing", "", false, false],
  ["a newline alone", "\n", false, false],
  // In JSON's own spellings and no other, though jq reads more of them: no byte order mark, no
  // byte that is no UTF-8, which jq reads as a replacement character, and so no such character.
  ["with a byte order mark before it", `\uFEFF${line()}`, false, false],
  ["with a byte order mark inside a field of its own", own('"a\uFEFFb"'), true, true],
  // A NUL where the release's own fields refuse nothing: only the reading of the NUL itself does.
  ["with a NUL in a field of its own", line({ note: "@" }).replace("@", "a\0b"), false, false],
  ["with a NUL after it", `${line().trimEnd()}\0\n`, false, false],
  ["with a NUL and then a second release", `${line({}, "9.9.9").trimEnd()}\0${line()}`, false, false],
  ["with a byte that is no UTF-8 in a field of its own", Buffer.concat([begun(), Buffer.from(',"note":"'), Buffer.from([0xff]), Buffer.from('"}\n')]), false, false],
  ["with a byte that is no UTF-8 in the name of a field of its own", Buffer.concat([begun(), Buffer.from(',"no'), Buffer.from([0xff]), Buffer.from('te":1}\n')]), false, false],
  ["with the bytes of half a pair in a field of its own", Buffer.concat([begun(), Buffer.from(',"note":"'), Buffer.from([0xed, 0xa0, 0x80]), Buffer.from('"}\n')]), false, false],
  ["with a replacement character in a field of its own", own('"a\uFFFDb"'), false, false],
  ["with a replacement character's escape in a field of its own", own('"a\\ufffdb"'), true, true],
  // A number as JSON writes one, in 17 digits at most: jq rounds a longer one to 17 digits before
  // it makes a number of it, and what it rounds to is then not what the digits say.
  ...["+1", "01", "1.", ".5", "-", "nan", "NaN", "infinity", "-infinity", "Inf", "sNaN", "0x10", "1_000"].map((number): Form => [`with a number of its own written ${number}`, own(number), false, false]),
  ...["-0", "0.5", "1E-5", "1e+5", "1e999", "12345678901234567", "1.2345678901234567e-300"].map((number): Form => [`with a number of its own written ${number}`, own(number), true, true]),
  ["with a number of its own in 18 digits", own("123456789012345678"), false, false],
  ["with a number of its own in 18 digits about its point", own("0.12345678901234567"), false, false],
  ["with a number of its own in 3000 digits", own(`1${"0".repeat(2999)}`), false, false],
  ["with 18 digits in a word of its own", own('"123456789012345678"'), true, true],
  // Lists and objects: a value 64 fields and places down at most. How deep jq reads at all is its
  // own, and changes with jq.
  ["with a number of its own 64 down, in lists", down(64, LIST), true, true],
  ["with a number of its own 65 down, in lists", down(65, LIST), false, false],
  ["with a number of its own 64 down, in objects", down(64, OBJECT), true, true],
  ["with a number of its own 65 down, in objects", down(65, OBJECT), false, false],
  ["with a number of its own 64 down, in lists and objects by turns", down(64, LIST, OBJECT), true, true],
  ["with a number of its own 65 down, in lists and objects by turns", down(65, LIST, OBJECT), false, false],
  ["with an empty list of its own 64 down", own(`${"[".repeat(64)}${"]".repeat(64)}`), true, true],
  ["with an empty list of its own 65 down", own(`${"[".repeat(65)}${"]".repeat(65)}`), false, false],
  ["with a number of its own 128 down, in objects", down(128, OBJECT), false, false],
  ["with a number of its own 255 down, in lists", down(255, LIST), false, false],
  ["with a number of its own 300 down, in lists", down(300, LIST), false, false],
  ["with a number of its own 300 down, in objects", down(300, OBJECT), false, false],
  // The escape of a pair's first half, with no escape of its second half behind it: jq refuses the text.
  ["with the first half of a pair escaped alone", own('"\\ud800"'), false, false],
  ["with the first half of a pair escaped alone, in capitals", own('"\\uDBFF"'), false, false],
  ["with the first half of a pair escaped last", own('"a\\udbff"'), false, false],
  ["with the first half of a pair and then a letter", own('"\\ud800a"'), false, false],
  ["with the first half of a pair twice", own('"\\ud800\\ud800"'), false, false],
  ["with the first half of a pair and then a letter's escape", own('"\\ud800\\u0041"'), false, false],
  ["with a pair's halves the other way about", own('"\\udc00\\ud800"'), false, false],
  ["with a pair and then a first half", own('"\\ud83d\\ude00\\ud800"'), false, false],
  ["with the first half of a pair alone in a field's name", own('{"\\ud800":1}'), false, false],
  ["with the first half of a pair alone in a field named twice", line({ note: "x" }).replace('"note"', '"note":"\\ud800","note"'), false, false],
  ["with the second half of a pair escaped alone", own('"\\udc00"'), true, true],
  ["with a pair escaped", own('"\\ud83d\\ude00"'), true, true],
  ["with a pair escaped in capitals", own('"\\uD83D\\uDE00"'), true, true],
  ["with a backslash's escape before ud800", own('"\\\\ud800"'), true, true],
  // Each end of the first halves and of the second, alone, with what is no half on either side of
  // them; and the first pair there is, and the last.
  ...["d800", "d9ab", "daff", "dbff"].map((half): Form => [`with the first half ${half} escaped alone`, own(`"\\u${half}"`), false, false]),
  ...["d7ff", "dc00", "ddab", "deff", "dfff", "e000"].map((other): Form => [`with ${other} escaped alone`, own(`"\\u${other}"`), true, true]),
  ["with the first pair there is, escaped", own('"\\ud800\\udc00"'), true, true],
  ["with the last pair there is, escaped", own('"\\udbff\\udfff"'), true, true],
  ["with the first half of a pair and then what is just below a second half", own('"\\ud800\\udbff"'), false, false],
  ["with the first half of a pair and then what is just above a second half", own('"\\ud800\\ue000"'), false, false],
  // A version: x.y.z in the ten digits, no part with a zero before it.
  ["of version 0.0.1", line({}, "0.0.1"), true, true],
  ["of version 10.20.30", line({}, "10.20.30"), true, true],
  ["with a zero before a version's last part", line({}, "1.2.04"), false, false],
  ["with a zero before a version's first part", line({}, "01.2.4"), false, false],
  ["with a zero before a version's middle part", line({}, "1.02.4"), false, false],
  ["of version 00.0.1", line({}, "00.0.1"), false, false],
  ["of a version of two parts", line({}, "1.2"), false, false],
  ["of a version with a suffix", line({}, "1.2.4-beta"), false, false],
  ["of a version in other digits", line({}, "1.2.\u0664"), false, false],
  ["of a version that climbs", line({}, "../../etc"), false, false],
  ["of a version that is a number", line({ version: 124 }), false, false],
  ["of a version written with an escape", line().replace('"version":"1.2.4"', '"version":"1.2.\\u0034"'), true, true],
  ["with its version named twice, the release's last", line().replace('{"version"', '{"version":"9.9.9","version"'), true, true],
  // Its other fields.
  ["of another channel", line({ channel: "beta" }), false, false],
  ["of another platform", line({ platform: "darwin" }), false, false],
  ["of another architecture", line({ arch: "arm64" }), false, false],
  ["with a url elsewhere", line({ url: "https://elsewhere.example/surogate.tar.gz" }), false, false],
  ["with another version's url", line({ url: "releases/1.2.5/surogate-desktop-1.2.5-linux-x64.tar.gz" }), false, false],
  ["with a hash in capitals", line({ sha256: "A".repeat(64) }), false, false],
  ["with a hash of 63 letters", line({ sha256: "a".repeat(63) }), false, false],
  // Its size and its state schema: whole numbers, the size above nothing, the schema from 1, each below 10^15.
  ["with a size written 1e3", written("size", "1e3").replace(String(171199516), "1000"), true, true],
  ["with a size written 171199516.0", written("size", "171199516.0"), true, true],
  ["with a size of nothing", line({ size: 0 }), false, false],
  ["with a size below nothing", line({ size: -1 }), false, false],
  ["with a size of one and a half", line({ size: 1.5 }), false, false],
  ["with a size in a word", line({ size: "171199516" }), false, false],
  ["with a size of 10^15", written("size", "1000000000000000"), false, false],
  ["with a size one below 10^15", written("size", "999999999999999"), true, true],
  // A number is what it rounds to: 999999999999999.99 is 10^15, and 1e-400 is nothing.
  ["with a size written just below 10^15", written("size", "999999999999999.99"), false, false],
  ["with a size written just above nothing", written("size", "1e-400"), false, false],
  ["with a size past every number", written("size", "1e400"), false, false],
  ["with a size written 0171199516", written("size", "0171199516"), false, false],
  ["with a size written +171199516", written("size", "+171199516"), false, false],
  ["with a size written 171199516.", written("size", "171199516."), false, false],
  ["with a size in 17 digits", written("size", "171199516.00000000"), true, true],
  ["with a size in 18 digits", written("size", "171199516.000000000"), false, false],
  // Of these 18 digits jq makes a number that is not whole, and JSON.parse the whole one below it.
  ["with a size in 18 digits that is whole to one reader alone", written("size", "137438953472.000015"), false, false],
  ["with a state schema written 1.0", written("stateSchema", "1.0"), true, true],
  ["with a state schema that rounds to 1", written("stateSchema", "99999999999999999e-17"), true, true],
  ["with a state schema that rounds to 1, in 20 digits", written("stateSchema", "0.9999999999999999999"), false, false],
  ["with a state schema written +1", written("stateSchema", "+1"), false, false],
  ["with a state schema written 01", written("stateSchema", "01"), false, false],
  ["with a state schema of nothing", line({ stateSchema: 0 }), false, false],
  ["with a state schema of one and a half", line({ stateSchema: 1.5 }), false, false],
  ["with a state schema in a word", line({ stateSchema: "1" }), false, false],
  ["with a state schema of 10^15", written("stateSchema", "1000000000000000"), false, false],
  ["with a state schema written just below 10^15", written("stateSchema", "999999999999999.99"), false, false],
  ["without a state schema", line({ stateSchema: undefined }), false, false],
];
