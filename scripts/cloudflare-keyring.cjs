// The administration secret stays in the OS credential store. This helper's
// stdout is captured only by cloudflare-manage.py, never displayed or logged.
const { Entry } = require('@napi-rs/keyring');
const fs = require('node:fs');
try {
  const [action, host] = process.argv.slice(2);
  if (!/^[a-z0-9.-]+$/.test(host || '') || !['get', 'set'].includes(action)) throw new Error();
  const entry = new Entry('AlikhanBlogAdministration', host);
  if (action === 'get') {
    const value = entry.getPassword();
    if (value) process.stdout.write(value);
  } else {
    const value = fs.readFileSync(0, 'utf8').trim();
    if (!/^[A-Za-z0-9_-]{64}$/.test(value)) throw new Error();
    entry.setPassword(value);
  }
} catch (error) {
  if (process.argv[2] !== 'get') {
    process.stderr.write('OS credential-store operation failed.');
    process.exitCode = 1;
  }
}
