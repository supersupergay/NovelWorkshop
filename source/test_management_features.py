"""November 14: user-facing project management and editable exports."""
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from zipfile import ZipFile
from xml.etree import ElementTree as ET
import novel
import providers
from projects import connection
from exports import export_document


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.app = novel.Workshop(self.root)

    def action(self, command, **fields):
        return self.app.action('main', command, fields)

    def test_no_demo_and_empty_catalog_remains_usable(self):
        self.assertEqual([p['id'] for p in self.app.projects.entries()], ['main'])
        self.assertEqual(self.app.state('main')['chapters'], [])
        self.assertEqual(self.action('delete-project',confirm_name='我的小说')['project'], '')
        restarted = novel.Workshop(self.root)
        self.assertEqual(restarted.state('')['projects'], [])
        new = restarted.action('', 'new-project', {'name':'重开'})['project']
        self.assertEqual(restarted.state(new)['project_name'],'重开')

    def test_old_demo_hidden_without_erasing_its_database(self):
        old = self.root/'demo.sqlite3'
        old.write_bytes(b'legacy data')
        with connection(self.app.projects.catalog) as db:
            db.execute("INSERT INTO projects VALUES('demo','内置演示','demo.sqlite3',0)")
        restarted = novel.Workshop(self.root)
        self.assertNotIn('demo',restarted.paths)
        self.assertEqual(old.read_bytes(),b'legacy data')

    def test_rename_named_saves_persistence_and_collision(self):
        target = Path(self.action('save-project')['path'])
        self.assertEqual(target.name,'我的小说.novelproj')
        self.action('rename-project',name='新的书名')
        renamed = target.with_name('新的书名.novelproj')
        self.assertTrue(renamed.exists())
        self.assertFalse(target.exists())
        self.assertEqual(Path(self.action('save-project')['path']),renamed)
        self.assertEqual(json.loads(renamed.read_text())['name'],'新的书名')
        self.assertEqual(novel.Workshop(self.root).state('main')['project_name'],'新的书名')
        target.write_text('unrelated')
        with self.assertRaises(providers.AppError):
            self.action('rename-project',name='我的小说')
        self.assertEqual(target.read_text(),'unrelated')
        self.assertEqual(self.app.state('main')['project_name'],'新的书名')
        for bad in ('../x','CON','LPT1.txt','abc.','a:b','x/y',''):
            with self.assertRaises(providers.AppError):self.action('rename-project',name=bad)

    def test_save_does_not_overwrite_unrelated_file(self):
        target=self.root/'我的小说.novelproj'
        target.write_text('other data')
        with self.assertRaises(providers.AppError):self.action('save-project')
        self.assertEqual(target.read_text(),'other data')
        target.unlink()
        self.action('save-project')
        target.write_text('replaced outside app')
        with self.assertRaises(providers.AppError):self.action('save-project')
        self.assertEqual(target.read_text(),'replaced outside app')

    def test_autosave_keeps_two_and_manual_save(self):
        manual=Path(self.action('save-project')['path'])
        saved=[]
        for i in range(4):
            with patch('time.time',return_value=100000+i*1801):
                saved.append(Path(self.action('autosave')['path']))
        self.assertEqual([p.exists() for p in saved],[False,False,True,True])
        self.assertTrue(manual.exists())
        self.assertEqual(len(list(self.root.glob('*_自动保存.novelproj'))),2)
        with patch('time.time',return_value=100000+3*1801+10):
            self.assertTrue(self.action('autosave')['skipped'])
        self.action('rename-project',name='轮换小说')
        self.assertEqual(len(list(self.root.glob('轮换小说*_自动保存.novelproj'))),2)
        self.assertEqual(len(list(self.root.glob('我的小说*.novelproj'))),0)

    def test_autosave_failure_retains_latest_copy(self):
        with patch('time.time',return_value=100000):
            first=Path(self.action('autosave')['path'])
        with patch('time.time',return_value=102000):
            second=Path(self.action('autosave')['path'])
        with patch('time.time',return_value=104000),patch('projects.os.replace',side_effect=OSError('disk')):
            with self.assertRaises(providers.AppError):self.action('autosave')
        self.assertTrue(second.exists())
        self.assertEqual(json.loads(second.read_text())['name'],'我的小说')
        self.assertFalse(list(self.root.glob('.save-*')))
        with patch('time.time',return_value=106000):self.action('autosave')
        self.assertEqual(len(list(self.root.glob('*_自动保存.novelproj'))),2)

    def test_autosave_only_deletes_registered_owned_snapshots(self):
        unrelated=self.root/'我的小说_19990101_自动保存.novelproj'
        unrelated.write_text('user backup')
        for i in range(3):
            with patch('time.time',return_value=100000+i*1801):self.action('autosave')
        self.assertEqual(unrelated.read_text(),'user backup')

    def test_autosave_is_serialized_after_writing(self):
        self.action('card',number=1,title='章',goal='',tags='',refs='')
        entered, release, saved = threading.Event(), threading.Event(), threading.Event()
        errors=[]
        def model(*args,**kw):
            entered.set();release.wait(3);return {'body':'完成后正文'}
        def generate():
            try:self.action('generate',number=1)
            except Exception as e:errors.append(e)
        def autosave():
            try:self.action('autosave');saved.set()
            except Exception as e:errors.append(e)
        with patch.object(novel,'call_model',side_effect=model):
            t=threading.Thread(target=generate);t.start();self.assertTrue(entered.wait(2))
            a=threading.Thread(target=autosave);a.start()
            self.assertFalse(saved.wait(.05));release.set();t.join(3);a.join(3)
        self.assertFalse(errors)
        self.assertTrue(saved.is_set())
        snapshot=json.loads(next(self.root.glob('*_自动保存.novelproj')).read_text())
        self.assertEqual(snapshot['drafts'][0]['body'],'完成后正文')

    def test_custom_full_endpoints_preserved_for_every_protocol(self):
        for protocol,url in [('chat','http://intranet-ai:7890/arbitrary?tenant=1'),('responses','http://127.0.0.1:9000/custom/path/'),('messages','https://example.test/message?version=2026')]:
            profile=self.app.input_profile('writer',{'provider':'custom_endpoint','protocol':protocol,'base':url,'model':'model','key':''})
            self.assertTrue(self.app.profile_state(profile)['configured'])
            spec=providers.request_spec(profile,'instruction',{'draft':'正文'},novel.WRITE_SCHEMA,'test',1024)
            self.assertEqual(spec[0],protocol);self.assertEqual(spec[1],url)
        for bad in ('file:///x','ftp://host/path','http://u:p@host/a','http://host/a#frag','http://host:bad/a'):
            with self.assertRaises(providers.AppError):providers.normalize_endpoint(bad)

    def test_mimo_request_and_theme_persistence(self):
        p=self.app.input_profile('writer',{'provider':'mimo','base':'','model':'manual-model','key':'mimo-key'})
        _,url,body,headers=providers.request_spec(p,'',{'draft':'正文'},novel.WRITE_SCHEMA,'test',100)
        self.assertEqual(url,'https://api.xiaomimimo.com/v1/chat/completions')
        self.assertEqual(headers,{'api-key':'mimo-key'})
        self.assertEqual(body['model'],'manual-model')
        self.action('theme',night=True)
        self.assertTrue(novel.Workshop(self.root).state('main')['night_mode'])
        self.action('theme',night=False)
        self.assertFalse(self.app.state('main')['night_mode'])

    def test_editable_exports_roundtrip_unicode_and_escape(self):
        rows=[{'number':1,'title':'标题 & <x>','body':'第一段。\n\n中文😀\\{内容}<script>\n末行。'}]
        for fmt in ('txt','md','html','rtf','docx'):
            data,mime=export_document('书名',rows,fmt)
            self.assertGreater(len(data),20)
            if fmt=='docx':
                with ZipFile(io.BytesIO(data)) as z:
                    for n in z.namelist():ET.fromstring(z.read(n))
                    root=ET.fromstring(z.read('word/document.xml'))
                    text=''.join(root.itertext())
                    self.assertIn(rows[0]['title'],text)
                    self.assertIn('中文😀\\{内容}<script>',text)
            elif fmt=='html':self.assertIn('&lt;script&gt;',data.decode())
            elif fmt=='rtf':
                self.assertIn(br'\u-10179?\u-8704?',data)
                self.assertIn(br'\\\{',data)
            else:self.assertIn(rows[0]['body'],data.decode('utf-8-sig'))
        with self.assertRaises(providers.AppError):export_document('书',[],'txt')
        with self.assertRaises(providers.AppError):export_document('书',rows,'pdf')

    def test_long_rtf_is_linear_and_retains_ending(self):
        start=time.monotonic()
        data,_=export_document('长篇',[{'number':1,'title':'章','body':'文'*500000+'终'}],'rtf')
        self.assertLess(time.monotonic()-start,5)
        self.assertIn(br'\u32456?',data)

    def test_http_export_names_formats_and_icon(self):
        import urllib.request
        import urllib.error
        from http.server import ThreadingHTTPServer
        self.action('card',number=1,title='正文标题',goal='',tags='',refs='')
        v=self.action('manual',number=1,body='中文正文 < & >')['version']
        self.action('approve',number=1,version=v,override=True)
        self.action('rename-project',name='可编辑小说')
        server=ThreadingHTTPServer(('127.0.0.1',0),novel.handler(self.app,'test'))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            url=f'http://127.0.0.1:{server.server_port}/test/'
            for fmt in ('docx','txt','md','rtf','html'):
                with urllib.request.urlopen(url+'export?project=main&format='+fmt) as r:
                    self.assertIn("filename*=UTF-8''",r.headers['Content-Disposition'])
                    from urllib.parse import unquote
                    self.assertIn('可编辑小说.'+fmt,unquote(r.headers['Content-Disposition']))
                    self.assertGreater(len(r.read()),10)
            with urllib.request.urlopen(url+'icon.ico') as r:
                self.assertEqual(r.read(),novel.icon_path().read_bytes())
            with self.assertRaises(urllib.error.HTTPError):urllib.request.urlopen(url+'export?format=invalid')
        finally:
            server.shutdown();server.server_close();thread.join()
