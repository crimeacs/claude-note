"""Real synthetic source formats; no model calls or private input files."""
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from claude_note import egress


def pdf_bytes():
    """A complete one-page PDF with binary header and correct xref offsets."""
    body = b'%PDF-1.7\n%\xe2\xe3\xcf\xd3\n'
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R >>',
               b'<< /Length 0 >>\nstream\n\nendstream']
    offsets = []
    for number, content in enumerate(objects, 1):
        offsets.append(len(body))
        body += str(number).encode() + b' 0 obj\n' + content + b'\nendobj\n'
    xref = len(body)
    body += b'xref\n0 5\n0000000000 65535 f \n'
    body += b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets)
    return body + b'trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n' + str(xref).encode() + b'\n%%EOF\n'


def write_docx(path):
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr('[Content_Types].xml', '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        package.writestr('_rels/.rels', '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        package.writestr('word/document.xml', '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Public synthetic source.</w:t></w:r></w:p></w:body></w:document>')


class EgressSourceFormats(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def prepare(self, path, *, rules=None):
        return egress.prepare('Public converted source view', audience='model', sources=[str(path)],
                              rules=rules or {'default': ['model']})

    def test_real_pdf_and_docx_keep_source_policy_and_recorded_lineage(self):
        private = self.root / 'private.md'
        private.write_text('---\nprivate: true\n---\nPrivate synthetic evidence.')
        pdf, docx = self.root / 'source.pdf', self.root / 'source.docx'
        pdf.write_bytes(pdf_bytes())
        write_docx(docx)
        for source in (pdf, docx):
            with self.subTest(source=source.suffix):
                self.assertEqual(self.prepare(source), 'Public converted source view')
                with self.assertRaises(egress.EgressDenied):
                    self.prepare(source, rules={'default': ['host']})
                egress.record_derivation(source, '---\nderivation_sources: ' + json.dumps([str(private)])
                                         + '\n---\n', vault_root=self.root)
                with self.assertRaises(egress.EgressDenied):
                    self.prepare(source)

    def test_misleading_extensions_do_not_hide_private_text(self):
        for suffix in ('.pdf', '.docx', '.jsonl'):
            source = self.root / ('private' + suffix)
            source.write_text('---\nprivate: true\n---\nPrivate synthetic facts.')
            with self.subTest(suffix=suffix), self.assertRaises(egress.EgressDenied):
                self.prepare(source)

    def test_unsupported_binary_and_mismatched_magic_fail_closed(self):
        bodies = {'arbitrary.bin': b'\0\xff\xfe', 'not-pdf.pdf': b'\xff\xfe\0',
                  'header-only.pdf': b'%PDF-1.7\n%\xff\xfe\nNo complete PDF',
                  'wrong-kind.docx': pdf_bytes(), 'wrong-kind.bin': pdf_bytes()}
        for name, body in bodies.items():
            source = self.root / name
            source.write_bytes(body)
            with self.subTest(name=name), self.assertRaises(egress.EgressDenied):
                self.prepare(source)
        archive = self.root / 'ordinary-zip.docx'
        with zipfile.ZipFile(archive, 'w') as package:
            package.writestr('ordinary.txt', 'This is not a Word document.')
        with self.assertRaises(egress.EgressDenied):
            self.prepare(archive)

    def test_native_jsonl_above_note_bound_is_strict_utf8_and_inspectable(self):
        source = self.root / 'native-session.jsonl'
        source.write_text(json.dumps({'type': 'user', 'message': {'content': 'x' * (8 * 1024 * 1024)}}) + '\n')
        self.assertGreater(source.stat().st_size, 8 * 1024 * 1024)
        self.assertEqual(self.prepare(source), 'Public converted source view')
        with source.open('ab') as handle:
            handle.write(b'\xff\n')
        with self.assertRaises(egress.EgressDenied):
            self.prepare(source)

    def test_source_size_limits_apply_before_reading_sparse_inputs(self):
        for name, size in (('note.md', 8 * 1024 * 1024 + 1),
                           ('native.jsonl', 256 * 1024 * 1024 + 1),
                           ('binary.pdf', 256 * 1024 * 1024 + 1)):
            source = self.root / name
            with source.open('wb') as handle:
                handle.truncate(size)
            with self.subTest(name=name), self.assertRaises(egress.EgressDenied):
                self.prepare(source)

    def test_final_symlinks_hardlinks_fifo_and_missing_source_fail_closed(self):
        original = self.root / 'original.md'
        original.write_text('Public synthetic source.')
        linked = self.root / 'linked.md'
        linked.symlink_to(original)
        hard = self.root / 'hard.md'
        os.link(original, hard)
        fifo = self.root / 'pipe.md'
        os.mkfifo(fifo)
        for source in (linked, hard, fifo, self.root / 'absent.md'):
            with self.subTest(name=source.name), self.assertRaises(egress.EgressDenied):
                self.prepare(source)

    def test_parent_alias_requires_supplied_and_canonical_policy(self):
        actual = self.root / 'actual'
        actual.mkdir()
        source = actual / 'source.md'
        source.write_text('Public synthetic source.')
        linked = self.root / 'alias'
        linked.symlink_to(actual, target_is_directory=True)
        alias = linked / source.name
        supplied_only = {'rules': [{'source': str(alias), 'audiences': ['model']}]}
        with self.assertRaises(egress.EgressDenied):
            self.prepare(alias, rules=supplied_only)
        both = {'rules': supplied_only['rules'] + [{'source': str(source), 'audiences': ['model']}]}
        self.assertEqual(self.prepare(alias, rules=both), 'Public converted source view')
        canonical_denial = {'default': ['model'], 'rules': [
            {'source': str(source), 'audiences': ['host'], 'restrict': True}]}
        with self.assertRaises(egress.EgressDenied):
            self.prepare(alias, rules=canonical_denial)
