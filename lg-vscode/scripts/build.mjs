import * as esbuild from 'esbuild';
import postcss from 'postcss';
import tailwind from 'tailwindcss';
import autoprefixer from 'autoprefixer';
import { readFile, writeFile } from 'node:fs/promises';
import './notices.mjs';

const css = { name: 'lg-css', setup(build) { build.onEnd(async result => {
  if (result.errors.length) return;
  const text = await readFile('dist/webview.css', 'utf8');
  const processed = await postcss([tailwind({ content: ['./webview/**/*.{ts,tsx}'], theme: { extend: {} }, plugins: [] }), autoprefixer()]).process(text, { from: 'dist/webview.css' });
  await writeFile('dist/webview.css', processed.css);
}); } };
const options = { bundle: true, sourcemap: false, logLevel: 'info', minify: !process.argv.includes('--watch') };
const builds = [
  { ...options, entryPoints: ['src/extension.ts'], outfile: 'dist/extension.cjs', platform: 'node', format: 'cjs', external: ['vscode'] },
  { ...options, entryPoints: ['webview/index.tsx'], outfile: 'dist/webview.js', platform: 'browser', format: 'iife', jsx: 'automatic', plugins: [css] },
];
if (process.argv.includes('--watch')) {
  const contexts = await Promise.all(builds.map(build => esbuild.context(build)));
  await Promise.all(contexts.map(c => c.watch()));
  console.log('LG watch ready');
} else {
  for (const build of builds) await esbuild.build(build);
}
