"""Verify PE structure, icon resources and the packaged application entry point."""
import hashlib
import io
import marshal
from pathlib import Path
import struct
import sys
import tempfile
from zipfile import ZipFile
from build_windows import read_archive

exe = Path(sys.argv[1])
bootloader, entries, library = read_archive(exe)
pe = struct.unpack_from('<I', bootloader, 0x3c)[0]
assert bootloader[pe:pe+4] == b'PE\0\0'
assert struct.unpack_from('<H', bootloader, pe+4)[0] == 0x8664
assert library == 'python313.dll'
contents = {name: data for name, _, data in entries}
source = Path(__file__).parent
files=('interface.html', 'projects.py', 'providers.py', 'exports.py', 'icon.ico')
for name in files:
    assert contents[name] == (source/name).read_bytes(), name
assert marshal.loads(contents['novel']) == compile((source/'novel.py').read_bytes(), 'novel.py', 'exec')
# Inspect the independently-addressable PE resource tree and compare every icon
# image with the source ICO, instead of only testing the appended Python bundle.
optional=pe+24
section_table=optional+struct.unpack_from('<H',bootloader,pe+20)[0]
n=struct.unpack_from('<H',bootloader,pe+6)[0]
sections=[struct.unpack_from('<8sIIIIIIHHI',bootloader,section_table+i*40) for i in range(n)]
assert all(s[4]+s[3] <= len(bootloader) for s in sections)
resource_rva,resource_size=struct.unpack_from('<II',bootloader,optional+128)
def file_offset(rva):
    for s in sections:
        if s[2] <= rva < s[2]+max(s[1],s[3]):return s[4]+rva-s[2]
    raise AssertionError('unmapped resource RVA')
base=file_offset(resource_rva)
def resources(offset=0):
    named,ids=struct.unpack_from('<HH',bootloader,base+offset+12)
    assert named == 0
    result={}
    for i in range(ids):
        key,target=struct.unpack_from('<II',bootloader,base+offset+16+8*i)
        if target & 0x80000000: result[key]=resources(target & 0x7fffffff)
        else:
            address,size,codepage,reserved=struct.unpack_from('<IIII',bootloader,base+target)
            assert resource_rva <= address < resource_rva+resource_size
            start=file_offset(address);result[key]=bootloader[start:start+size]
    return result
res=resources()
ico=contents['icon.ico'];count=struct.unpack_from('<H',ico,4)[0]
assert set(res)=={3,14} and len(res[3])==count
for i in range(count):
    size,offset=struct.unpack_from('<II',ico,6+i*16+8)
    assert res[3][i+1][0] == ico[offset:offset+size]
assert struct.unpack_from('<HHH',res[14][1][0])==(0,1,count)
with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    for name in files:(root/name).write_bytes(contents[name])
    sys.path.insert(0, str(root))
    namespace = {'__name__':'packaged_novel','__file__':str(root/'novel.py')}
    exec(marshal.loads(contents['novel']), namespace)
    app = namespace['Workshop'](root/'data')
    assert app.state('main')['build_version'] == '2026.11.14.1'
    assert len(app.projects.entries())==1 and not app.state('main')['chapters']
    app.action('main','card',{'number':1,'title':'真实项目','goal':'','tags':'','refs':'','pacing':'先慢后快'})
    def mock_model(profile,role,instructions,payload,schema):
        if role=='writer':return {'body':'修订正文。' if payload['previous_draft'] else '首次正文。'}
        return {'issues':[],'notes':'','logic':'逻辑完整','ai_style':'语言自然','pacing':'节奏平稳'}
    namespace['call_model']=mock_model
    generated=app.action('main','generate',{'number':1})
    revised=app.action('main','revise',{'number':1,'version':generated['version'],'instruction':'修订'})
    app.action('main','review',{'number':1,'version':revised['version']})
    app.action('main','approve',{'number':1,'version':revised['version']})
    assert app.state('main')['can_comment']
    app.action('main','rename-project',{'name':'打包检查'})
    saved=app.action('main','save-project',{})
    assert Path(saved['path']).name=='打包检查.novelproj'
    app.action('main','autosave',{})
    assert len(list((root/'data').glob('*_自动保存.novelproj')))==1
    from exports import export_document
    output,_=export_document('打包检查',[{'number':1,'title':'章','body':'中文正文'}],'docx')
    assert ZipFile(io.BytesIO(output)).testzip() is None
    assert 'DEMO_SOURCE' not in namespace
print('PASS: PE AMD64 and icon resource tree; Python 3.13 runtime; exact packaged source/bytecode; generation, revision, review, finalization, rename, save, autosave and DOCX smoke test. Model output mocked; native Windows execution not tested.')
print('SHA256:',hashlib.sha256(exe.read_bytes()).hexdigest())
