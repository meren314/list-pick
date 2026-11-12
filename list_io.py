"""Read plain-text lists without silently mis-decoding BOM-marked text."""
import codecs
from pathlib import Path


def read_list_file(path: str) -> str:
    data = Path(path).read_bytes()
    if data.startswith((b'PK\x03\x04', b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1')):
        raise ValueError('这个文件是工作簿或压缩文件，不是纯文本清单。请从 Excel/WPS 复制一列，或另存为 TXT 后导入。')
    encoding = None
    # UTF-32LE begins with the UTF-16LE marker, so inspect the longer BOM first.
    if data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        encoding = 'utf-32'
    elif data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encoding = 'utf-16'
    elif data.startswith(codecs.BOM_UTF8):
        encoding = 'utf-8-sig'
    try:
        if encoding:
            text = data.decode(encoding)
        else:
            try:
                text = data.decode('utf-8')
            except UnicodeDecodeError:
                text = data.decode('gb18030')
    except UnicodeDecodeError as exc:
        raise ValueError('无法完整读取文本编码。请用记事本将清单另存为 UTF-8，再重新导入。') from exc
    if '\x00' in text:
        raise ValueError('清单含空字符，可能是未带编码标记的 Unicode 文本或二进制文件。请另存为 UTF-8 TXT 后导入。')
    if any(ord(char) < 32 and char not in '\r\n\t' for char in text):
        raise ValueError('这个文件含有非文本控制字符。请选择纯文本清单，或从 Excel/WPS 复制一列文件名。')
    return text
