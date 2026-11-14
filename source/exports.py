"""Portable editable manuscript exports. No optional runtime dependencies."""
from html import escape
from io import BytesIO
import re
from zipfile import ZipFile, ZIP_DEFLATED
from providers import AppError


def xml_text(text):
    # XML 1.0 cannot represent control characters; retain tabs and line breaks.
    return escape(re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]', '', text))


def export_document(name, rows, fmt):
    if fmt not in {"txt", "md", "docx", "html", "rtf"}:
        raise AppError("请选择 TXT、Markdown、Word DOCX、HTML 或 RTF 格式。")
    if not rows:
        raise AppError("还没有已定稿章节，暂时无法导出。")
    sections = [(f"第 {row['number']} 章 {row['title']}", row['body']) for row in rows]
    plain = name + "\n\n" + "\n\n".join(title + "\n\n" + body for title, body in sections) + "\n"
    if fmt == "txt":
        return plain.encode("utf-8-sig"), "text/plain; charset=utf-8"
    if fmt == "md":
        return ("# " + name + "\n\n" + "\n\n".join("## " + title + "\n\n" + body for title, body in sections) + "\n").encode(), "text/markdown; charset=utf-8"
    if fmt == "html":
        body = ''.join('<section><h2>' + escape(title) + '</h2><div class="prose">' + escape(text) + '</div></section>' for title,text in sections)
        return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>' + escape(name) + '</title><style>body{max-width:44em;margin:3em auto;padding:0 1.5em;color:#222;background:white;font:18px/1.8 SimSun,serif}h1{font-size:1.8em}h2{font-size:1.3em}section{margin-top:2em}.prose{white-space:pre-wrap;overflow-wrap:anywhere}</style><h1>' + escape(name) + '</h1>' + body + '</html>').encode(), "text/html; charset=utf-8"
    if fmt == "rtf":
        def rtf(text):
            parts = []
            encoded = text.encode('utf-16-le')
            for i in range(0,len(encoded),2):
                code = int.from_bytes(encoded[i:i+2], 'little')
                if code == 10: parts.append(r'\par ' + '\n')
                elif code == 9: parts.append(r'\tab ')
                elif code == 13: continue
                elif code in (92,123,125): parts.append('\\' + chr(code))
                elif 32 <= code < 127: parts.append(chr(code))
                else: parts.append(r'\u' + str(code if code < 32768 else code - 65536) + '?')
            return ''.join(parts)
        return (r'{\rtf1\ansi\deff0{\fonttbl{\f0 SimSun;}}\uc1\f0\fs24 ' + rtf(plain) + '}').encode('ascii'), "application/rtf"
    ns = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
    def paragraph(text, style='Normal'):
        return '<w:p><w:pPr><w:pStyle w:val="' + style + '"/></w:pPr><w:r><w:rPr><w:rFonts w:ascii="SimSun" w:hAnsi="SimSun" w:eastAsia="SimSun"/><w:lang w:val="zh-CN" w:eastAsia="zh-CN"/></w:rPr><w:t xml:space="preserve">' + xml_text(text) + '</w:t></w:r></w:p>'
    body = paragraph(name, 'Title')
    for title, text in sections:
        body += paragraph(title, 'Heading1')
        body += ''.join(paragraph(line) for line in text.replace('\r\n','\n').replace('\r','\n').split('\n'))
    document = f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{ns}"><w:body>' + body + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr></w:body></w:document>'
    styles = f'''<?xml version="1.0" encoding="UTF-8"?><w:styles xmlns:w="{ns}"><w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:eastAsia="宋体"/><w:sz w:val="24"/><w:color w:val="000000"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="360" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults><w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style><w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:after="360"/></w:pPr><w:rPr><w:b/><w:sz w:val="40"/></w:rPr></w:style><w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:before="280" w:after="200"/><w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/><w:sz w:val="30"/></w:rPr></w:style></w:styles>'''
    output = BytesIO()
    with ZipFile(output,'w', ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/></Types>')
        z.writestr('_rels/.rels', '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr('word/_rels/document.xml.rels', '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        z.writestr('word/document.xml',document)
        z.writestr('word/styles.xml',styles)
    return output.getvalue(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
