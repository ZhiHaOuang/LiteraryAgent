import type { Item } from '../../shared/protocol';

export function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
export const text = (value: unknown) => typeof value === 'string' ? value : '';
export const array = (value: unknown): unknown[] => Array.isArray(value) ? value : [];

export function errorCategory(value: unknown): string {
  const message = text(record(value).message);
  const jsonFailure = /upstream tool input is not valid JSON; json_error=(missing_comma|missing_colon|invalid_property|missing_value|unterminated_string|invalid_escape|control_character|extra_data|invalid_json), offset=([0-9]{1,8}), chars=([0-9]{1,8})(?![0-9])/.exec(message);
  if (jsonFailure && Number(jsonFailure[2]) <= Number(jsonFailure[3])) {
    return `bridgeInvalidToolJson / ${jsonFailure[1]} / ${jsonFailure[2]}:${jsonFailure[3]}`;
  }
  const bridgeErrors: [string, string][] = [
    ['upstream output token limit reached before completion', 'bridgeOutputLimit'],
    ['upstream tool input is not valid JSON', 'bridgeInvalidToolJson'],
    ['upstream tool input must be an object', 'bridgeInvalidToolArguments'],
    ['upstream returned an unknown tool', 'bridgeUnknownTool'],
    ['upstream stream ended before message_stop', 'bridgeIncompleteStream'],
    ['upstream returned a streaming error', 'bridgeStreamError'],
    ['insufficient balance or usable quota', 'providerQuotaUnavailable'],
    ['Provider upstream request failed', 'providerRequestFailed'],
  ];
  for (const [phrase, category] of bridgeErrors) if (message.includes(phrase)) return category;
  const info = record(value).codexErrorInfo;
  const known = ['contextWindowExceeded','sessionBudgetExceeded','usageLimitExceeded','rateLimitExceeded','serverOverloaded',
    'cyberPolicy','misalignmentPolicyViolation','internalServerError','unauthorized','badRequest','threadRollbackFailed','sandboxError','other'];
  if (typeof info === 'string' && known.includes(info)) return info;
  for (const key of ['httpConnectionFailed','responseStreamConnectionFailed','responseStreamDisconnected','responseTooManyFailedAttempts']) {
    const status = record(record(info)[key]).httpStatusCode;
    if (key in record(info)) return key + (typeof status === 'number' ? ` / HTTP ${status}` : '');
  }
  return 'unknown';
}

export function normalizeItem(value: unknown, runId: string, done: boolean): Item | undefined {
  const item = record(value), id = text(item.id), kind = text(item.type);
  if (!id) return;
  const state: Item['state'] = item.status === 'failed' ? 'failed' : item.status === 'declined' ? 'cancelled' : done ? 'completed' : 'running';
  const base: Item = { id, runId, kind: 'notice', title: kind || '未知输出', text: '', state };
  if (kind === 'userMessage') return { ...base, id: 'user:' + runId, kind: 'user', title: '作者', text: array(item.content).map(c => text(record(c).text)).join('\n'), state: 'completed' };
  if (kind === 'agentMessage') return { ...base, kind: 'assistant', title: 'LG', text: text(item.text) };
  if (kind === 'reasoning') return; // Private reasoning is never sent to the webview.
  if (kind === 'plan') return { ...base, title: '计划', text: text(item.text) };
  if (kind === 'mcpToolCall') {
    const result = record(item.result);
    let structured = result.structuredContent;
    // FastMCP may serialize the same tool result as one JSON text block.
    const content = array(result.content).map(record);
    if (structured == null && content.length === 1 && content[0].type === 'text') {
      try { structured = JSON.parse(text(content[0].text)); } catch { /* Plain text remains displayable. */ }
    }
    const details = record(structured), diff = record(details.diff);
    return { ...base, kind: 'tool', title: `${text(item.server)} · ${text(item.tool)}`,
      text: item.error ? '工具失败，请查看已保存状态后再决定是否重试。' : array(result.content).map(c => text(record(c).text)).filter(Boolean).join('\n'),
      structured: { arguments: item.arguments, result: structured ?? result.content ?? null },
      ...(typeof details.artifact === 'string' ? { files: [details.artifact] } : {}),
      ...(typeof diff.path === 'string' && typeof diff.before === 'string' && typeof diff.after === 'string' && typeof diff.applied === 'boolean'
        ? { diff: { path: diff.path, before: diff.before, after: diff.after, applied: diff.applied } } : {}) };
  }
  if (kind === 'commandExecution') return { ...base, kind: 'tool', title: text(item.command) || '命令', text: text(item.aggregatedOutput), structured: { cwd: item.cwd, exitCode: item.exitCode } };
  if (kind === 'fileChange') return { ...base, kind: 'tool', title: '文件修改', text: '以下是后端报告的差异；界面不会再次应用补丁。', structured: item.changes };
  return { ...base, text: '此输出类型暂以只读数据展示。', structured: item };
}
