const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const DIR = path.join(os.homedir(), 'surogate-spike-results');

function writeResult(question, data) {
  fs.mkdirSync(DIR, { recursive: true });
  const file = path.join(DIR, `${question}-${os.hostname()}.json`);
  const body = { question, host: os.hostname(), at: new Date().toISOString(), ...data };
  fs.writeFileSync(file, JSON.stringify(body, null, 2));
  return file;
}

module.exports = { writeResult, DIR };
