import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, symlink, realpath } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { checkedWorkspacePath, ensureCleanWorkspace } from '../src/host/workspacePaths';

async function fixture() {
  const base = await mkdtemp(path.join(tmpdir(), 'lg-paths-'));
  const root = path.join(base, 'book'), sibling = path.join(base, 'book-other');
  await mkdir(root); await mkdir(sibling);
  const inside = path.join(root, 'chapter.md'), outside = path.join(sibling, 'notes.md');
  await writeFile(inside, '原创测试正文'); await writeFile(outside, '独立文件');
  return { base, root, inside, outside };
}
const document = (file: string, isDirty = true, isUntitled = false) => ({
  isDirty, isUntitled, uri: { scheme: isUntitled ? 'untitled' : 'file', fsPath: file },
});

test('workspace paths reject traversal, sibling prefixes, URLs and outward symlinks', async () => {
  const f = await fixture();
  assert.equal(await checkedWorkspacePath(f.root, 'chapter.md'), await realpath(f.inside));
  for (const file of ['../book-other/notes.md', f.outside, '', '\0', 'file://' + f.inside, 'https://example.test/file']) {
    await assert.rejects(checkedWorkspacePath(f.root, file));
  }
  await symlink(f.outside, path.join(f.root, 'external.md'));
  await assert.rejects(checkedWorkspacePath(f.root, 'external.md'), /不在当前作品/);
});

test('unsaved aliases into the book block writes even when opened outside its lexical path', async () => {
  const f = await fixture(), alias = path.join(f.base, 'alias.md');
  await symlink(f.inside, alias);
  await assert.rejects(ensureCleanWorkspace(f.root, [document(alias)], true), /未保存/);
  await ensureCleanWorkspace(f.root, [document(alias, false)], true);
  await ensureCleanWorkspace(f.root, [document(f.outside)], true);
});

test('symlinked workspace roots protect canonical file paths and direct dirty documents', async () => {
  const f = await fixture(), alias = path.join(f.base, 'workspace-alias');
  await symlink(f.root, alias, 'dir');
  assert.equal(await checkedWorkspacePath(alias, 'chapter.md'), await realpath(f.inside));
  await assert.rejects(ensureCleanWorkspace(alias, [document(f.inside)], true), /未保存/);
  await assert.rejects(ensureCleanWorkspace(f.root, [document(f.inside)], true), /未保存/);
});

test('untrusted, untitled and unresolved dirty files cannot authorize writes', async () => {
  const f = await fixture();
  await assert.rejects(ensureCleanWorkspace(f.root, [], false), /信任/);
  await assert.rejects(ensureCleanWorkspace(f.root, [document('Untitled-1', true, true)], true), /未保存/);
  await assert.rejects(ensureCleanWorkspace(f.root, [document(path.join(f.base, 'missing.md'))], true), /未保存/);
  await ensureCleanWorkspace(f.root, [], true);
});
