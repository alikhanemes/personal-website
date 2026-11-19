import { build } from 'esbuild';

await build({
  entryPoints: ['editor-src/editor.js'],
  outfile: 'static/editor.js',
  bundle: true,
  minify: true,
  legalComments: 'eof',
  format: 'iife',
  target: ['es2020'],
  sourcemap: false,
});
console.log('Built the private editor bundle.');
