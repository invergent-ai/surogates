const { utilityProcess } = require('electron');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { writeResult } = require('../lib/results.cjs');

exports.run = async () => {
  const folder = fs.mkdtempSync(path.join(os.homedir(), 'spike-q4-'));
  const hostProc = utilityProcess.fork(path.join(__dirname, 'browser-host.cjs'), [], { serviceName: 'q4-browser', stdio: 'inherit' });
  const results = await new Promise((resolve) => { hostProc.once('message', resolve); hostProc.postMessage({ folder }); });
  hostProc.kill();
  const outside = path.join(os.homedir(), 'escape.txt');
  console.log('q4 result:', writeResult('q4', { folder, escapedFileExists: fs.existsSync(outside), results }));
};
