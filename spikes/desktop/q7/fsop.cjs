const fs = require('node:fs');

const C = fs.constants;
const fail = (code, message) => Object.assign(new Error(message), { code });

function checkRegular(fd) {
  const st = fs.fstatSync(fd);
  if (!st.isFile()) throw fail('ENOTREG', 'not a regular file');
  if (st.nlink > 1) throw fail('EMLINK', 'file has more than one hard link');
}

const ops = {
  write(p, data) {
    const fd = fs.openSync(p, C.O_WRONLY | C.O_CREAT | C.O_NOFOLLOW | C.O_NONBLOCK | C.O_CLOEXEC, 0o644);
    try { checkRegular(fd); fs.ftruncateSync(fd, 0); fs.writeSync(fd, data); return { ok: true }; } finally { fs.closeSync(fd); }
  },
  read(p) {
    const fd = fs.openSync(p, C.O_RDONLY | C.O_NOFOLLOW | C.O_NONBLOCK | C.O_CLOEXEC);
    try { checkRegular(fd); return { ok: true, data: fs.readFileSync(fd, 'utf8').slice(0, 200) }; } finally { fs.closeSync(fd); }
  },
};

let buf = '';
process.stdin.on('data', (d) => {
  buf += d;
  let i;
  while ((i = buf.indexOf('\n')) >= 0) {
    const m = JSON.parse(buf.slice(0, i));
    buf = buf.slice(i + 1);
    let r;
    try { r = ops[m.op](m.path, m.data); } catch (e) { r = { ok: false, code: e.code, message: e.message }; }
    process.stdout.write(`${JSON.stringify({ id: m.id, ...r })}\n`);
  }
});
