const { app } = require('electron');

const arg = process.argv.find((a) => /^q\d(-[a-z]+)?$/.test(a));

app.whenReady().then(async () => {
  try {
    if (!arg) throw new Error('usage: surogate q1|q2|…|q8[-mode]');
    await require(`./${arg.split('-')[0]}/main.cjs`).run({ arg, argv: process.argv });
  } catch (e) {
    console.error(e);
    process.exitCode = 1;
  } finally {
    app.quit();   // q8's start mode never resolves, so it stays alive until killed
  }
});
