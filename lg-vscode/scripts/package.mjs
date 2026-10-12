import { createVSIX } from '@vscode/vsce';
await createVSIX({ cwd: process.cwd(), packagePath: 'literarygiant-vscode-0.1.0.vsix', dependencies: false, allowMissingRepository: true, skipLicense: true, rewriteRelativeLinks: false });
