import { test } from 'node:test';
import assert from 'node:assert/strict';
import { passage } from '../src/claude_note/recall_server.mjs';

test('long notes count prefix lines only for the winning passage', () => {
  const body = 'A separate subject has enough words to be a readable paragraph.\n\n'.repeat(4000);
  const split = String.prototype.split;
  let prefixSplits = 0;
  String.prototype.split = function (separator, ...args) {
    if (separator === '\n') prefixSplits++;
    return split.call(this, separator, ...args);
  };
  let selected;
  try { selected = passage(body, 'hooks knowledge', 260); }
  finally { String.prototype.split = split; }
  assert.ok(prefixSplits <= 2, `Line counting rescanned ${prefixSplits} candidate prefixes`);
  const { start_char: start, end_char: end, start_line, end_line } = selected.anchor;
  assert.equal(start_line, body.slice(0, start).split('\n').length);
  assert.equal(end_line, body.slice(0, end).split('\n').length);
  assert.ok(start > body.length - 100, 'Equal matches still prefer the latest passage');
});

test('line anchors preserve CRLF, Unicode and frontmatter coordinates', () => {
  for (const body of [
    '---\ntype: gotcha\n---\n\nOlder unrelated paragraph has enough characters to be readable.\n\nOAuth permission retry keeps the original token scope across reconnection attempts.\n',
    'Первый раздел содержит достаточно символов для отдельного абзаца.\r\n\r\nOAuth permission retry keeps the original token scope across reconnection attempts.\r\n',
  ]) {
    const selected = passage(body, 'OAuth permission retry', 260);
    assert.match(selected.text, /OAuth permission retry/);
    const anchor = selected.anchor;
    assert.equal(anchor.start_line, body.slice(0, anchor.start_char).split('\n').length);
    assert.equal(anchor.end_line, body.slice(0, anchor.end_char).split('\n').length);
  }
});
