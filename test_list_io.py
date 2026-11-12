"""Plain-text import checks for real Windows save formats and wrong files."""
import codecs
from pathlib import Path
import tempfile
import unittest
from list_io import read_list_file


class ListImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='list-pick-text-')
        self.path = Path(self.temporary.name) / 'list.txt'

    def tearDown(self):
        self.temporary.cleanup()

    def test_windows_and_unicode_text_formats_keep_literal_names(self):
        content = '000123\r\n合同.pdf\n leading.txt\n含\u2028分隔字符.txt'
        for encoding in ('utf-8', 'utf-8-sig', 'utf-16', 'utf-32', 'gb18030'):
            with self.subTest(encoding=encoding):
                self.path.write_bytes(content.encode(encoding))
                self.assertEqual(read_list_file(str(self.path)), content)
        for marker, encoding in ((codecs.BOM_UTF16_BE, 'utf-16-be'),
                                 (codecs.BOM_UTF32_BE, 'utf-32-be')):
            with self.subTest(encoding=encoding):
                self.path.write_bytes(marker + content.encode(encoding))
                self.assertEqual(read_list_file(str(self.path)), content)

    def test_incomplete_marked_unicode_does_not_fall_back_to_gbk(self):
        self.path.write_bytes(codecs.BOM_UTF16_LE + b'a')
        with self.assertRaisesRegex(ValueError, '编码'):
            read_list_file(str(self.path))

    def test_workbook_and_binary_bytes_receive_actionable_error(self):
        for data in (b'PK\x03\x04binary', b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1binary', b'name\x00.txt', b'bad\x02name'):
            with self.subTest(data=data):
                self.path.write_bytes(data)
                with self.assertRaises(ValueError):
                    read_list_file(str(self.path))

    def test_tabs_are_preserved_for_explicit_row_validation(self):
        self.path.write_bytes(b'name.txt\tsecond-column\n')
        self.assertEqual(read_list_file(str(self.path)), 'name.txt\tsecond-column\n')

    def test_empty_file_and_nonexistent_file(self):
        self.path.write_bytes(b'')
        self.assertEqual(read_list_file(str(self.path)), '')
        with self.assertRaises(FileNotFoundError):
            read_list_file(str(self.path.parent / 'missing.txt'))


if __name__ == '__main__':
    unittest.main()
