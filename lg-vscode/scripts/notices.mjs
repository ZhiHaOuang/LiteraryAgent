import {readFile, readdir, writeFile} from 'node:fs/promises';

const lock=JSON.parse(await readFile('package-lock.json','utf8'));
const sections=['# Third-party notices\n\nProduction dependencies are bundled into this extension. These license texts come from the installed versions recorded in package-lock.json.\n'];
for(const [path,pkg] of Object.entries(lock.packages).sort(([a],[b])=>a.localeCompare(b))){
  if(!path||pkg.dev)continue;
  const files=(await readdir(path)).filter(name=>/^(licen[sc]e|copying|notice)([._-]|$)/i.test(name)).sort();
  if(!files.length)throw Error(`Missing license text for ${path}`);
  sections.push(`## ${path.replace(/^node_modules\//,'')} ${pkg.version}\n\nLicense: ${pkg.license??'see text below'}\n`);
  for(const file of files)sections.push(`### ${file}\n\n${(await readFile(`${path}/${file}`,'utf8')).trim()}\n`);
}
await writeFile('THIRD_PARTY_NOTICES.md',sections.join('\n'));
