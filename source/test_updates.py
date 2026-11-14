"""Regression coverage for storage, pacing, reviewer delivery and final commentary."""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import novel
import providers
import projects
from test_workshop import review_fixture


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = novel.Workshop(self.root / 'data')

    def action(self, action, **data):
        return self.app.action('main', action, data)

    def write_and_review(self, command, **fields):
        result = self.action(command, **fields)
        try:
            self.action('review', number=fields['number'], version=result['version'])
        except novel.AppError as error:
            result['warning'] = '草稿已保存。审稿未完成。' + str(error)
        return result

    def card(self, number=1, pacing='开头略写，中段放慢，结尾收住'):
        self.action('card', number=number, title='测试章', goal='秘密章节目标', tags='', refs='', pacing=pacing)

    def final(self, number=1, body='他放下杯子，离开了房间。'):
        self.card(number)
        identifier = self.action('manual', number=number, body=body)['version']
        self.action('approve', number=number, version=identifier, override=True, notes='秘密台账')
        return identifier

    def configure_commentator(self):
        self.action('settings', role='commentator', provider='custom_chat', base='http://127.0.0.1:1234/v1', model='reader', key='')

    def test_move_preserves_versions_and_restart_and_saves_project(self):
        identifier = self.final()
        original = self.app.paths['main']
        directory = self.root / '中文目录 with spaces'
        self.action('save-directory', directory=str(directory))
        self.assertTrue(original.exists())
        self.assertEqual(self.app.paths['main'].parent, directory)
        self.assertEqual(novel.Workshop(self.root / 'data').state('main', 1)['draft']['id'], identifier)
        self.action('review_requirements', review_requirements='不要过早解释主题')
        target = Path(self.action('save-project')['path'])
        self.assertEqual(target.parent, directory)
        exported = json.loads(target.read_text())
        self.assertEqual(exported['review_requirements'], '不要过早解释主题')
        self.assertIn('pacing', exported['chapters'][0])
        imported = self.action('import-project', content=exported)['project']
        self.assertEqual(self.app.state(imported)['review_requirements'], '不要过早解释主题')
        with self.assertRaisesRegex(novel.AppError, '同名'):
            self.action('save-directory', directory=str(original.parent))
        self.assertEqual(self.app.paths['main'].parent, directory)
        with self.assertRaisesRegex(novel.AppError, '绝对路径'):
            self.action('save-directory', directory='relative/path')

    def test_real_old_database_migrates_pacing_without_data_loss(self):
        path = self.root / 'legacy.sqlite3'
        with projects.connection(path) as db:
            db.executescript(novel.SCHEMA)
            db.execute("INSERT INTO chapters VALUES(1,'旧标题','','','',NULL)")
        novel.initialize(path)
        with novel.database(path) as db:
            row = novel.chapter(db, 1)
            self.assertEqual(row['pacing'], '')
            self.assertEqual(row['title'], '旧标题')
        novel.initialize(path)

    def test_pacing_and_requirements_reach_writer_reviewer_and_invalidate(self):
        self.card()
        self.action('review_requirements', review_requirements='特别检查重复解释')
        captured = []
        def model(profile, role, instructions, payload, schema):
            captured.append((role, copy.deepcopy(payload)))
            if role == 'writer':
                return {'body':'完整首段。\n\n完整末段。'}
            return review_fixture(payload['draft'])
        with patch.object(novel, 'call_model', side_effect=model):
            result = self.write_and_review('generate', number=1)
        self.assertNotIn('warning', result)
        for _, payload in captured:
            self.assertEqual(payload['context']['card']['pacing'], '开头略写，中段放慢，结尾收住')
        self.assertEqual(captured[1][1]['draft'], captured[0][1].get('unused', '完整首段。\n\n完整末段。'))
        self.assertEqual(captured[1][1]['context']['review_requirements'], '特别检查重复解释')
        self.assertTrue(self.app.state('main', 1)['draft']['current_review'])
        self.action('review_requirements', review_requirements='改查人物知情')
        self.assertFalse(self.app.state('main', 1)['draft']['current_review'])
        with patch.object(novel, 'call_model', side_effect=model):
            self.action('review', number=1, version=result['version'])
        self.card(pacing='结尾加快')
        self.assertFalse(self.app.state('main', 1)['draft']['current_review'])

    def test_internal_evidence_and_style_without_external_reference(self):
        self.card()
        text = '他先出门。他又说自己一直没有出门。'
        identifier = self.action('manual', number=1, body=text)['version']
        issues = [dict(kind=kind, severity='中', draft_quote='他先出门。', source_id=source,
                       source_quote='他又说自己一直没有出门。' if source else '', reason='具体问题', suggestion='调整')
                  for kind, source in [('内部逻辑','DRAFT'), ('AI痕迹',''), ('节奏','')]]
        with patch.object(novel, 'call_model', return_value=review_fixture(text, issues)):
            self.action('review', number=1, version=identifier)
        found = self.app.state('main', 1)['draft']['report']['issues']
        self.assertEqual([x['kind'] for x in found], ['内部逻辑','AI痕迹','节奏'])
        self.assertTrue(found[0]['source_verified'])

    def test_comment_only_final_content_and_manual_gate_and_stale_persistence(self):
        self.configure_commentator()
        with patch.object(novel, 'call_model') as model:
            with self.assertRaisesRegex(novel.AppError, '全部定稿'):
                self.action('comment')
            self.card()
            with self.assertRaisesRegex(novel.AppError, '全部定稿'):
                self.action('comment')
            self.final()
            self.action('rules', rules='秘密规则')
            self.action('style', style='秘密风格')
            self.action('review_requirements', review_requirements='秘密审稿要求')
            model.assert_not_called()
        captured = []
        def model(profile, role, instructions, payload, schema):
            captured.append((role, payload))
            self.assertEqual(set(payload), {'final_chapters', 'commentator_persona'})
            encoded = json.dumps(payload, ensure_ascii=False)
            self.assertNotIn('秘密', encoded)
            return {'body':'杯子的细节很有效，但离开的动作可以再留一点停顿。'}
        with patch.object(novel, 'call_model', side_effect=model):
            self.action('comment')
        state = self.app.state('main')
        self.assertFalse(state['commentary_stale'])
        self.assertEqual(captured[0][0], 'commentator')
        self.assertIn('杯子', novel.Workshop(self.root / 'data').state('main')['commentary']['body'])
        self.action('manual', number=1, body='尚未定稿的新版本')
        self.assertTrue(self.app.state('main')['commentary_stale'])
        self.assertFalse(self.app.state('main')['can_comment'])
        with self.assertRaisesRegex(novel.AppError, '全部定稿'):
            self.action('comment')
        backup = self.app.backup('main')
        imported = self.action('import-project', content=backup)['project']
        self.assertEqual(self.app.state(imported)['commentary']['body'], state['commentary']['body'])

    def test_optional_commentator_can_clear_and_failure_preserves_comment(self):
        self.final()
        self.assertEqual(self.app.state('main')['settings']['commentator']['model'], '')
        self.configure_commentator()
        with patch.object(novel, 'call_model', return_value={'body':'现有评论'}):
            self.action('comment')
        with patch.object(novel, 'call_model', side_effect=novel.AppError('连接失败')):
            with self.assertRaises(novel.AppError):
                self.action('comment')
        self.assertEqual(self.app.state('main')['commentary']['body'], '现有评论')
        self.action('settings', role='commentator', provider='custom_chat', base='http://127.0.0.1:1234/v1', model='', key='')
        with self.assertRaisesRegex(novel.AppError, '配置'):
            self.action('comment')

    def test_retry_preserves_full_input_for_all_protocols(self):
        manuscript = '首段' + '文' * 20000 + '末段'
        for provider, base, protocol in [('custom_chat','http://127.0.0.1:1234/v1','chat'), ('custom_responses','http://127.0.0.1:1234/v1','responses'), ('custom_messages','http://127.0.0.1:1234/v1','messages')]:
            profile = {'provider':provider,'base':base,'model':'test','key':''}
            payload = {'draft':manuscript, 'draft_receipt':review_fixture(manuscript)['receipt']}
            for error_kind in ('broken','empty','missing_fields','truncated'):
                calls = []
                def request(url, body, headers, timeout):
                    calls.append(copy.deepcopy(body))
                    user = body['input'] if protocol == 'responses' else body['messages'][-1]['content']
                    self.assertEqual(json.loads(user)['draft'], manuscript)
                    result = review_fixture(manuscript)
                    if len(calls) == 1 and error_kind == 'missing_fields': result.pop('logic')
                    output = json.dumps(result)
                    if len(calls) == 1 and error_kind == 'broken': output = '{broken'
                    if len(calls) == 1 and error_kind == 'empty': output = ''
                    truncated = len(calls) == 1 and error_kind == 'truncated'
                    if protocol == 'chat': return {'choices':[{'finish_reason':'length' if truncated else 'stop','message':{'content':output}}]}
                    if protocol == 'responses': return {'status':'incomplete' if truncated else 'completed','output':[{'type':'message','content':[{'type':'output_text','text':output}]}]}
                    return {'stop_reason':'max_tokens' if truncated else 'end_turn','content':[{'type':'text','text':output}]}
                with patch.object(providers, 'request_json', side_effect=request):
                    result = providers.call_model(profile,'reviewer',novel.REVIEWER,payload,novel.REVIEW_SCHEMA)
                self.assertEqual(result['logic'], '逻辑评述')
                self.assertEqual(len(calls), 2)

    def test_review_failure_keeps_body_and_is_never_empty_success(self):
        self.card()
        with patch.object(novel, 'call_model', side_effect=[{'body':'正文确实存在'}, novel.AppError('格式失败')]):
            result = self.write_and_review('generate',number=1)
        self.assertIn('warning', result)
        self.assertEqual(self.app.state('main',1)['draft']['body'], '正文确实存在')
        self.assertIsNone(self.app.state('main',1)['draft']['report'])
        with patch.object(providers, 'request_json', return_value={'choices':[{'message':{'content':'请提供正文'},'finish_reason':'stop'}]}) as request:
            with self.assertRaises(novel.AppError):
                providers.call_model({'provider':'custom_chat','base':'http://127.0.0.1:1234','model':'test','key':''}, 'reviewer', novel.REVIEWER, {'draft':'正文'}, novel.REVIEW_SCHEMA)
            self.assertEqual(request.call_count, 2)

    def test_transient_review_retry_and_hard_failure_no_retry(self):
        profile = {'provider':'custom_chat','base':'http://127.0.0.1:1234/v1','model':'test','key':''}
        payload = {'draft':'正文','draft_receipt':review_fixture('正文')['receipt']}
        good = {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(review_fixture('正文'))}}]}
        for message in ('连接失败或超时', 'HTTP 503。服务暂时无法完成请求。'):
            with patch.object(providers,'request_json',side_effect=[novel.AppError(message),good]) as call:
                self.assertEqual(providers.call_model(profile,'reviewer',novel.REVIEWER,payload,novel.REVIEW_SCHEMA)['logic'],'逻辑评述')
                self.assertEqual(call.call_count,2)
        for message in ('HTTP 401。密钥验证失败。','HTTP 400。请核对模型 ID。'):
            with patch.object(providers,'request_json',side_effect=novel.AppError(message)) as call:
                with self.assertRaises(novel.AppError):
                    providers.call_model(profile,'reviewer',novel.REVIEWER,payload,novel.REVIEW_SCHEMA)
                self.assertEqual(call.call_count,1)

    def test_old_project_and_workspace_import_with_new_fields_defaulted(self):
        self.final()
        content = self.app.backup('main')
        content.pop('review_requirements'); content.pop('commentary')
        for card in content['chapters']: card.pop('pacing')
        content['workspace'] = {'active':1, 'version':content['drafts'][0]['id'], 'number':'1','title':'未保存标题','goal':'','tags':'','refs':'','body':'未保存正文','notes':'','instruction':'','rules':'','style':'','override':False}
        identifier = self.action('import-project',content=content)['project']
        state = self.app.state(identifier)
        self.assertEqual(state['chapters'][0]['pacing'], '')
        self.assertEqual(state['workspace']['pacing'], '')
        self.assertEqual(state['workspace']['body'], '未保存正文')

    def test_progress_remains_accessible_during_model_request(self):
        self.card()
        self.configure_commentator()
        started = threading.Event(); finish = threading.Event()
        self.final()
        server = ThreadingHTTPServer(('127.0.0.1',0),novel.handler(self.app,'test-token'))
        thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        def model(*args, **kwargs):
            started.set();finish.wait(3)
            return {'body':'评论'}
        with patch.object(novel,'call_model',side_effect=model):
            worker = threading.Thread(target=lambda:self.action('comment'));worker.start()
            self.assertTrue(started.wait(2))
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}/test-token/progress?project=main',timeout=1) as response:
                    progress = json.load(response)
                self.assertEqual(progress['stage'],'commentator')
            finally:
                finish.set();worker.join()


if __name__ == '__main__':
    unittest.main()
