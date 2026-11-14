"""November 13: decoupled writing/review and persistent commentary dialogue."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import novel
import providers
import projects
from test_workshop import review_fixture


class RevisionFeatureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.app = novel.Workshop(self.path)

    def action(self, name, **data):
        return self.app.action('main', name, data)

    def card(self, number=1):
        self.action('card', number=number, title='一章', goal='不应送给评论者的章节目标', tags='', refs='', pacing='由慢到快')

    def setup_comment(self):
        self.card()
        identifier = self.action('manual', number=1, body='定稿里的杯子没有把手。')['version']
        self.action('approve', number=1, version=identifier, override=True, notes='不应送给评论者的台账')
        self.action('settings', role='commentator', provider='custom_chat', base='http://127.0.0.1:1234', model='reader', key='')
        self.action('commentator-persona', persona='一位直率的老编辑，口语自然')
        with patch.object(novel, 'call_model', return_value={'body':'杯子的细节值得留下。'}):
            return self.action('comment')

    def test_auto_review_default_isolated_persisted_exported(self):
        self.assertTrue(self.app.state('main')['auto_review'])
        self.action('auto-review', enabled=False)
        self.assertFalse(novel.Workshop(self.path).state('main')['auto_review'])
        other = self.app.action('main','new-project',{'name':'独立项目'})['project']
        self.assertTrue(self.app.state(other)['auto_review'])
        exported = self.app.backup('main')
        self.assertIs(exported['auto_review'],False)
        imported = self.action('import-project', content=exported)['project']
        self.assertFalse(self.app.state(imported)['auto_review'])
        for invalid in ('false', 0, None):
            with self.assertRaises(novel.AppError):
                self.action('auto-review', enabled=invalid)
        old = copy.deepcopy(exported);old.pop('auto_review');old.pop('commentator_persona')
        old_imported = self.action('import-project',content=old)['project']
        self.assertTrue(self.app.state(old_imported)['auto_review'])

    def test_generation_and_revision_return_before_any_review_request(self):
        self.card()
        calls=[]
        def model(profile,role,instructions,payload,schema):
            calls.append((role,copy.deepcopy(payload)))
            self.assertEqual(role,'writer')
            return {'body':'第一次正文' if not payload['previous_draft'] else '修改后的正文'}
        with patch.object(novel,'call_model',side_effect=model):
            first=self.action('generate',number=1)
            self.assertIsNone(self.app.state('main',1)['draft']['report'])
            self.assertEqual(self.app.state('main',1)['draft']['body'],'第一次正文')
            second=self.action('revise',number=1,version=first['version'],instruction='修改这一句')
        self.assertEqual([role for role,_ in calls],['writer','writer'])
        self.assertEqual(calls[1][1]['previous_draft'],'第一次正文')
        self.assertEqual(self.app.state('main',1,first['version'])['draft']['body'],'第一次正文')
        self.assertEqual(self.app.state('main',1,second['version'])['draft']['body'],'修改后的正文')
        with patch.object(novel,'call_model',side_effect=novel.AppError('审稿超时')):
            with self.assertRaisesRegex(novel.AppError,'超时'):
                self.action('review',number=1,version=second['version'])
        self.assertEqual(self.app.state('main',1)['draft']['body'],'修改后的正文')
        self.assertIsNone(self.app.state('main',1)['draft']['report'])

    def test_review_binds_to_exact_saved_version_and_internal_hash(self):
        self.card()
        original='首段\n'+'正文'*10000+'\n最后一句。'
        first=self.action('manual',number=1,body=original)['version']
        second=self.action('manual',number=1,body='另一个版本')['version']
        def model(profile,role,instructions,payload,schema):
            self.assertEqual(payload['draft'],original)
            self.assertNotIn('draft_receipt',payload)
            return review_fixture(original)
        with patch.object(novel,'call_model',side_effect=model):
            self.action('review',number=1,version=first)
        report=self.app.state('main',1,first)['draft']['report']
        self.assertEqual(report['draft_sha256'],hashlib.sha256(original.encode()).hexdigest())
        self.assertEqual(report['draft_characters'],str(len(original)))
        self.assertIsNone(self.app.state('main',1,second)['draft']['report'])
        with patch.object(novel,'call_model',side_effect=novel.AppError('格式失败')):
            with self.assertRaises(novel.AppError):self.action('review',number=1,version=first)
        self.assertEqual(self.app.state('main',1,first)['draft']['report'],report)

    def test_reviewer_ignores_checksum_and_harmless_optional_differences(self):
        value=review_fixture('一段正文')
        value['receipt']='抄错了的旧校验码'
        value['extra']='额外输出'
        value.pop('notes')
        value['issues']=[{'kind':'AI 痕迹','severity':'medium','draft_quote':'一段正文','reason':'表达重复','suggestion':'删掉重复表述','source_id':None,'extra':'ignore'}]
        result=providers.parse_result('以下是审稿报告：\n```json\n'+json.dumps(value)+'\n```',novel.REVIEW_SCHEMA)
        self.assertNotIn('receipt',result)
        self.assertEqual(result['notes'],'')
        self.assertEqual(result['issues'][0]['kind'],'AI痕迹')
        self.assertEqual(result['issues'][0]['severity'],'中')
        self.assertEqual(result['issues'][0]['source_quote'],'')
        for key in ('logic','ai_style','pacing'):
            bad=copy.deepcopy(result);bad.pop(key)
            with self.assertRaises(novel.AppError):providers.parse_result(json.dumps(bad),novel.REVIEW_SCHEMA)
            bad[key]=''
            with self.assertRaises(novel.AppError):providers.parse_result(json.dumps(bad),novel.REVIEW_SCHEMA)
        valid=json.dumps(result)
        for text in (valid+valid,'{"broken":'+valid, '请提供待审正文'):
            with self.assertRaises(novel.AppError):providers.parse_result(text,novel.REVIEW_SCHEMA)

    def test_structured_missing_text_claim_is_not_accepted_as_review(self):
        value=review_fixture('正文')
        value.update(logic='未提供正文，无法审查',ai_style='请提供待审正文',pacing='正文缺失')
        with self.assertRaisesRegex(novel.AppError,'完整正文'):
            providers.parse_result(json.dumps(value),novel.REVIEW_SCHEMA)
        value.update(logic='没有发现正文内部逻辑错误',ai_style='表达自然',pacing='段落推进自然')
        self.assertEqual(providers.parse_result(json.dumps(value),novel.REVIEW_SCHEMA)['issues'],[])

    def test_persona_and_reply_history_are_bound_to_final_manuscript(self):
        initial=self.setup_comment()
        self.action('commentator-persona',persona='新的设定，仅对下次新评论生效')
        captured=[]
        def model(profile,role,instructions,payload,schema):
            captured.append(copy.deepcopy(payload))
            self.assertEqual(role,'commentator')
            self.assertEqual(set(payload),{'final_chapters','commentator_persona','discussion'})
            self.assertEqual(payload['commentator_persona'],'一位直率的老编辑，口语自然')
            self.assertNotIn('不应送给',json.dumps(payload,ensure_ascii=False))
            return {'body':'这个细节让读者感到杯子用起来不方便。'}
        with patch.object(novel,'call_model',side_effect=model):
            first=self.action('comment-reply',reply='为什么值得留下？',commentary_token=initial['commentary_token'])
            second=self.action('comment-reply',reply='能说得再具体一些吗？',commentary_token=first['commentary_token'])
        self.assertEqual(captured[0]['discussion'][0],{'role':'assistant','content':'杯子的细节值得留下。'})
        self.assertEqual([m['role'] for m in captured[1]['discussion']],['assistant','user','assistant','user'])
        self.assertEqual(len(second['commentary']['messages']),4)
        self.assertEqual(novel.Workshop(self.path).state('main')['commentary'],second['commentary'])
        imported=self.action('import-project',content=self.app.backup('main'))['project']
        self.assertEqual(self.app.state(imported)['commentary'],second['commentary'])
        self.assertEqual(self.app.state(imported)['commentator_persona'],'新的设定，仅对下次新评论生效')

    def test_initial_persona_reaches_comment_model_and_new_comment_resets_thread(self):
        first=self.setup_comment()
        with patch.object(novel,'call_model',return_value={'body':'回答'}):
            self.action('comment-reply',reply='问题',commentary_token=first['commentary_token'])
        self.action('commentator-persona',persona='说话温和，重视节奏的读者')
        with patch.object(novel,'call_model',return_value={'body':'重新评价'}) as call:
            result=self.action('comment')
            self.assertEqual(call.call_args.args[3]['commentator_persona'],'说话温和，重视节奏的读者')
            self.assertNotIn('discussion',call.call_args.args[3])
        self.assertEqual(result['commentary']['messages'],[])
        self.assertEqual(result['commentary']['persona'],'说话温和，重视节奏的读者')

    def test_failed_reply_and_stale_token_never_overwrite_existing_discussion(self):
        first=self.setup_comment()
        with patch.object(novel,'call_model',side_effect=novel.AppError('连接失败')):
            with self.assertRaises(novel.AppError):
                self.action('comment-reply',reply='这一段呢？',commentary_token=first['commentary_token'])
        self.assertEqual(self.app.state('main')['commentary'],first['commentary'])
        with patch.object(novel,'call_model',return_value={'body':'回答'}):
            second=self.action('comment-reply',reply='问题',commentary_token=first['commentary_token'])
        with patch.object(novel,'call_model') as model:
            with self.assertRaisesRegex(novel.AppError,'更新'):
                self.action('comment-reply',reply='重复请求',commentary_token=first['commentary_token'])
            model.assert_not_called()
        self.assertEqual(self.app.state('main')['commentary'],second['commentary'])

    def test_reply_requires_existing_current_comment_and_nonempty_input(self):
        first=self.setup_comment()
        with self.assertRaisesRegex(novel.AppError,'回复'):
            self.action('comment-reply',reply=' ',commentary_token=first['commentary_token'])
        version=self.action('manual',number=1,body='已改变的正文')['version']
        with self.assertRaisesRegex(novel.AppError,'全部定稿'):
            self.action('comment-reply',reply='问题',commentary_token=first['commentary_token'])
        self.action('approve',number=1,version=version,override=True)
        with self.assertRaisesRegex(novel.AppError,'变化'):
            self.action('comment-reply',reply='问题',commentary_token=first['commentary_token'])
        self.action('delete-comment',confirmed=True,commentary_token=first['commentary_token'])
        with self.assertRaisesRegex(novel.AppError,'先生成'):
            self.action('comment-reply',reply='问题',commentary_token='')

    def test_delete_requires_confirmation_and_current_token_preserves_novel_and_persona(self):
        first=self.setup_comment()
        with patch.object(novel,'call_model',return_value={'body':'回答'}):
            latest=self.action('comment-reply',reply='问题',commentary_token=first['commentary_token'])
        before=self.app.backup('main')
        for data in ({'commentary_token':latest['commentary_token']},{'confirmed':False,'commentary_token':latest['commentary_token']},{'confirmed':True,'commentary_token':first['commentary_token']}):
            with self.assertRaises(novel.AppError):self.action('delete-comment',**data)
            self.assertEqual(self.app.state('main')['commentary'],latest['commentary'])
        self.action('delete-comment',confirmed=True,commentary_token=latest['commentary_token'])
        after=self.app.backup('main')
        self.assertIsNone(after['commentary'])
        self.assertEqual(after['chapters'],before['chapters'])
        self.assertEqual(after['drafts'],before['drafts'])
        self.assertEqual(after['commentator_persona'],before['commentator_persona'])
        self.assertIsNone(novel.Workshop(self.path).state('main')['commentary'])

    def test_legacy_comment_roundtrip_and_bad_discussion_validation(self):
        first=self.setup_comment()
        content=self.app.backup('main')
        content['commentary'].pop('messages');content['commentary'].pop('persona')
        imported=self.action('import-project',content=content)['project']
        self.assertEqual(self.app.state(imported)['commentary']['body'],first['commentary']['body'])
        for messages in ([{'role':'system','content':'bad'},{'role':'assistant','content':'bad'}], [{'role':'user','content':'unpaired'}], 'not list'):
            bad=copy.deepcopy(content);bad['commentary']['messages']=messages
            with self.assertRaises(novel.AppError):projects.validate_project(bad)


if __name__=='__main__':unittest.main()
