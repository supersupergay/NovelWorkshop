import json
import hashlib
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Thread
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import novel
import providers
import test_support


def review_fixture(body, issues=None, notes=""):
    return {"issues": issues or [], "notes": notes, "logic": "逻辑评述", "ai_style": "语言评述", "pacing": "节奏评述",
            "receipt": hashlib.sha256(body.encode()).hexdigest()[:16] + ":" + str(len(body))}


class WorkshopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.app = novel.Workshop(self.path)
        test_support.seed_fixture(self.app)
        original_write, original_review = novel.write_body, novel.review_body
        def writer(settings, context, base=None, instruction=""):
            return test_support.write_body(True, settings, context, base, instruction) if context['card']['title'] == '试探' else original_write(settings,context,base,instruction)
        def reviewer(settings, context, body):
            return test_support.review_body(True, settings, context, body) if context['card']['title'] == '试探' else original_review(settings,context,body)
        for name, func in [('write_body',writer),('review_body',reviewer)]:
            mock = patch.object(novel,name,side_effect=func)
            mock.start()
            self.addCleanup(mock.stop)

    def action(self, command, project="fixture", **fields):
        return self.app.action(project, command, fields)

    def write_and_review(self, command, project="fixture", **fields):
        # The browser now performs these as two independent HTTP operations.
        result = self.action(command, project=project, **fields)
        try:
            self.action("review", project=project, number=fields["number"], version=result["version"])
        except novel.AppError as error:
            result["warning"] = "草稿已保存。审稿未完成。" + str(error)
        return result

    def test_demo_revise_publish_and_persist(self):
        generated = self.write_and_review("generate", number=2)["version"]
        original = self.app.state("fixture", 2)["draft"]
        issue, = original["report"]["issues"]
        self.assertTrue(issue["draft_verified"] and issue["source_verified"])
        self.assertIn("1", original["writing_basis"]["versions"])
        self.assertIn("梁叔", original["body"])
        with self.assertRaisesRegex(novel.AppError, "仍有问题"):
            self.action("approve", number=2, version=generated)
        revised = self.write_and_review("revise", number=2, version=generated, instruction=issue["suggestion"])["version"]
        self.assertNotEqual(generated, revised)
        selected = self.app.state("fixture", 2)["draft"]
        self.assertEqual(selected["report"]["issues"], [])
        self.action("approve", number=2, version=revised, notes=selected["report"]["notes"])
        self.assertIn(test_support.DEMO_FIXED, self.app.export("fixture"))
        self.assertNotIn(test_support.DEMO_WRONG, self.app.export("fixture"))
        reopened = novel.Workshop(self.path)
        self.assertEqual(reopened.state("fixture", 2)["chapters"][1]["canon"], revised)
        self.assertEqual(reopened.state("fixture", 2, generated)["draft"]["body"], original["body"])
        self.assertEqual(reopened.state("main")["chapters"], [])

    def test_changed_canon_flags_dependent_and_old_report(self):
        first = self.write_and_review("generate", number=2)["version"]
        revision = self.write_and_review("revise", number=2, version=first, instruction="改成提问")["version"]
        self.action("approve", number=2, version=revision, notes="还在试探")
        source = self.action("manual", number=1, body=test_support.DEMO_SOURCE + "窗户没有关。")["version"]
        self.action("approve", number=1, version=source, notes="窗户没关", override=True)
        state = self.app.state("fixture", 2)
        self.assertTrue(state["chapters"][1]["stale"])
        self.assertFalse(state["draft"]["current_review"])
        with self.assertRaisesRegex(novel.AppError, "过期"):
            self.action("approve", number=2, version=revision, notes="还在试探")

    def test_unverified_claim_and_interrupted_review(self):
        self.action("card", project="main", number=1, title="新篇", goal="", tags="", refs="")
        fabricated = {"kind": "冲突", "severity": "高", "draft_quote": "这里是正文",
                      "source_id": "C99V50", "source_quote": "虚构引文", "reason": "示例", "suggestion": ""}
        with patch.object(novel, "call_model", side_effect=[{"body": "这里是正文"}, novel.AppError("暂时中断")]):
            result = self.write_and_review("generate", project="main", number=1)
        self.assertIn("草稿已保存", result["warning"])
        self.assertIsNone(self.app.state("main", 1)["draft"]["report"])
        with patch.object(novel, "call_model", return_value=review_fixture("这里是正文", [fabricated])):
            self.action("review", project="main", number=1, version=result["version"])
        issue, = self.app.state("main", 1)["draft"]["report"]["issues"]
        self.assertEqual(issue["kind"], "待核实")
        self.assertTrue(issue["draft_verified"])
        self.assertFalse(issue["source_verified"])

    def test_official_api_request_shape(self):
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
            def read(self, _limit):
                return json.dumps({"status":"completed", "output":[{"type":"message", "content":[
                    {"type":"output_text","text":json.dumps({"body":"正文"})}]}]})

        with patch.object(providers.urllib.request, "build_opener") as builder:
            builder.return_value.open.return_value = Response()
            profile = {"provider":"openai", "base":providers.PROVIDERS["openai"].base, "key":"test-local", "model":"test-model"}
            result = providers.call_model(profile, "writer", novel.WRITER, {"x":"数据"}, novel.WRITE_SCHEMA)
        request = builder.return_value.open.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(result["body"], "正文")
        self.assertEqual(request.full_url, "https://api.openai.com/v1/responses")
        self.assertFalse(payload["store"])
        self.assertEqual(payload["text"]["format"]["schema"], novel.WRITE_SCHEMA)

    def test_profile_keys_are_separate_and_only_metadata_persists(self):
        self.action("settings", role="writer", provider="deepseek", model="author", base="", key="writer-key")
        self.action("settings", role="reviewer", provider="anthropic", model="editor", base="", key="editor-key")
        self.action("settings", role="writer", provider="deepseek", model="new-author", base="", key="")
        self.assertEqual(self.app.settings["writer"]["key"], "writer-key")
        self.assertEqual(self.app.settings["reviewer"]["key"], "editor-key")
        profile = self.action("settings", role="writer", provider="custom_chat", model="author",
                              base="https://api.example.com/v1/chat/completions", key="")
        self.assertEqual(profile["base"], "https://api.example.com/v1")
        self.assertFalse(profile["has_key"])
        self.assertTrue(self.app.state("main")["settings"]["reviewer"]["has_key"])
        self.assertNotIn("editor-key", json.dumps(self.app.state("main")))
        self.assertNotIn(b"editor-key", (self.path / "novel.sqlite3").read_bytes())
        reopened = novel.Workshop(self.path)
        self.assertEqual(reopened.settings["reviewer"]["model"], "editor")
        self.assertEqual(reopened.settings["reviewer"]["key"], "")
        with self.assertRaisesRegex(novel.AppError, "HTTPS"):
            self.action("settings", role="writer", provider="custom_chat", model="author",
                        base="http://api.example.com/v1", key="writer-key")
        with self.assertRaisesRegex(novel.AppError, "接口路径"):
            self.action("settings", role="writer", provider="custom_chat", model="author",
                        base="https://api.example.com/v1/responses", key="writer-key")

    def test_actual_http_mixed_protocol_generation_and_review(self):
        requests = []

        class ModelHandler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append((self.path, dict(self.headers), body))
                if self.path == "/writer/chat/completions":
                    manuscript = {"body": "第一段正文。"}
                    if "ping" in body["messages"][1]["content"]:
                        manuscript = {"body": "ping"}
                    response = {"choices": [{"finish_reason": "stop", "message": {
                        "content": "```json\n" + json.dumps(manuscript) + "\n```"}}]}
                elif self.path == "/reviewer/messages":
                    issue = {"kind":"冲突", "severity":"高", "draft_quote":"第一段正文。", "source_id":"RULES",
                             "source_quote":"必须采用第一人称。", "reason":"视角未体现。", "suggestion":"调整叙事视角。"}
                    result = review_fixture(json.loads(body["messages"][0]["content"])["draft"], [issue], "视角待修")
                    response = {"stop_reason":"end_turn", "content":[{"type":"text", "text":json.dumps(result)}]}
                else:
                    self.send_response(302)
                    self.send_header("Location", "/would-leak")
                    self.end_headers()
                    return
                data = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        self.action("settings", role="writer", provider="custom_chat", model="author",
                    base=base + "/writer", key="writer-key")
        self.action("settings", role="reviewer", provider="custom_messages", model="editor",
                    base=base + "/reviewer", key="editor-key")
        self.assertTrue(self.action("test-provider", role="writer")["tested"])
        self.action("rules", project="main", rules="必须采用第一人称。")
        self.action("card", project="main", number=1, title="新篇", goal="", tags="", refs="")
        result = self.write_and_review("generate", project="main", number=1)
        self.assertNotIn("warning", result)
        draft = self.app.state("main", 1)["draft"]
        self.assertEqual(draft["report"]["model"], "editor")
        self.assertTrue(draft["report"]["issues"][0]["source_verified"])
        self.assertEqual([entry[0] for entry in requests], [
            "/writer/chat/completions", "/writer/chat/completions", "/reviewer/messages"])
        writer_headers = {key.lower(): value for key, value in requests[1][1].items()}
        reviewer_headers = {key.lower(): value for key, value in requests[2][1].items()}
        self.assertEqual(writer_headers["authorization"], "Bearer writer-key")
        self.assertNotIn("x-api-key", writer_headers)
        self.assertEqual(reviewer_headers["x-api-key"], "editor-key")
        self.assertNotIn("authorization", reviewer_headers)
        self.action("settings", role="writer", provider="custom_chat", model="author",
                    base=base + "/redirect", key="writer-key")
        with self.assertRaisesRegex(novel.AppError, "拒绝跟随重定向"):
            self.action("test-provider", role="writer")
        self.assertEqual(requests[-1][0], "/redirect/chat/completions")
        self.assertEqual(len(requests), 4)

    def test_discovery_without_model_and_without_third_party_requests(self):
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                calls.append((self.path, dict(self.headers)))
                if self.path.endswith("/models"):
                    payload = {"data":[{"id":"local-author", "display_name":"本地写手"}]}
                    data = json.dumps(payload).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self.send_response(404)
                    self.end_headers()
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}/v1"
        with patch.dict("os.environ", {"HTTP_PROXY":"http://127.0.0.1:1",
                                     "HTTPS_PROXY":"http://127.0.0.1:1", "NO_PROXY":""}):
            result = self.action("list-models", role="writer", provider="custom_chat",
                                 base=base, key="", model="")
        self.assertEqual(result["models"], [{"id":"local-author", "name":"本地写手"}])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "/v1/models")
        self.assertNotIn("Authorization", calls[0][1])
        self.assertEqual(providers.normalize_base("http://192.168.1.8:1234/v1", "chat"),
                         "http://192.168.1.8:1234/v1")
        self.assertFalse(providers.needs_key("http://192.168.1.8:1234/v1"))
        for bad in ("http://8.8.8.8/v1", "http://169.254.169.254/v1"):
            with self.assertRaises(novel.AppError):
                providers.normalize_base(bad, "chat")

    def test_discovery_pagination_stays_at_selected_provider(self):
        profile = {"provider":"custom_messages","base":"https://example.test/v1","key":"test","model":""}
        responses = [
            {"data":[{"id":"one"}],"has_more":True,"last_id":"one"},
            {"data":[{"id":"two"}],"has_more":False},
        ]
        with patch.object(providers, "request_json", side_effect=copy.deepcopy(responses)) as request:
            result = providers.list_models(profile)
        self.assertEqual([m["id"] for m in result["models"]], ["one","two"])
        self.assertEqual(request.call_args_list[1].args[0], "https://example.test/v1/models?after_id=one")
        self.assertEqual(request.call_args.args[2]["x-api-key"], "test")

    def test_invalid_schema_and_truncation_are_not_success(self):
        with self.assertRaisesRegex(novel.AppError, "字段"):
            providers.parse_result('{"issues":[{"kind":"bad"}],"notes":""}', novel.REVIEW_SCHEMA)
        with self.assertRaisesRegex(novel.AppError, "未完整"):
            providers.extract_text("chat", {"choices":[{"finish_reason":"length", "message":{"content":"{}"}}]})

    def test_deepseek_only_endpoint_and_no_fallback(self):
        self.action("settings", role="writer", provider="deepseek", model="test-model",
                    base="https://api.deepseek.com/v1", key="fake-deepseek-test-key")
        profile = self.app.settings["writer"]
        seen = []
        class Response:
            def __init__(self, data):
                self.data = data
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
            def read(self, _):
                return json.dumps(self.data).encode()
        def capture(request, **_):
            seen.append(request)
            if request.get_method() == "GET":
                return Response({"data":[{"id":"test-model"}]})
            return Response({"choices":[{"finish_reason":"stop", "message":{"content":'{"body":"ping"}'}}]})
        with patch.object(providers.urllib.request, "build_opener") as builder:
            builder.return_value.open.side_effect = capture
            self.assertEqual(providers.list_models(profile)["models"][0]["id"], "test-model")
            result = self.action("test-provider", role="writer")
        self.assertEqual([r.full_url for r in seen], ["https://api.deepseek.com/v1/models",
                                                   "https://api.deepseek.com/v1/chat/completions"])
        self.assertEqual(result["endpoint"], seen[-1].full_url)
        self.assertEqual(seen[-1].get_header("Authorization"), "Bearer fake-deepseek-test-key")
        with patch.object(providers.urllib.request, "build_opener") as builder:
            builder.return_value.open.side_effect = OSError("mock timeout")
            with self.assertRaisesRegex(novel.AppError, "DeepSeek.*api.deepseek.com.*连接失败"):
                self.action("test-provider", role="writer")
            self.assertEqual(builder.return_value.open.call_count, 1)
            self.assertEqual(builder.return_value.open.call_args.args[0].full_url, result["endpoint"])
            handlers = builder.call_args.args
            self.assertTrue(any(isinstance(h, providers.RefuseRedirects) for h in handlers))
            self.assertTrue(any(isinstance(h, urllib.request.ProxyHandler) and not h.proxies for h in handlers))
        for bad in ("https://api.openai.com/v1", "https://api.deepseek.com.example.test", "https://api.deepseek.com:8443"):
            with patch.object(providers, "request_json") as request:
                with self.assertRaisesRegex(novel.AppError, "预设仅允许"):
                    providers.list_models({**profile, "base":bad})
                with self.assertRaisesRegex(novel.AppError, "预设仅允许"):
                    providers.call_model({**profile, "base":bad}, "writer", "", {}, novel.WRITE_SCHEMA)
                request.assert_not_called()

    def test_environment_never_selects_provider_or_imports_keys(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"OPENAI_API_KEY":"fake-environment-key"}):
            app = novel.Workshop(Path(directory))
        for profile in app.settings.values():
            self.assertEqual(profile["provider"], "deepseek")
            self.assertEqual(profile["key"], "")
        self.assertEqual(self.app.state("main")["build_version"], novel.BUILD_VERSION)

    def test_style_reaches_generation_and_revision_per_project(self):
        self.action("style", project="main", style="短句，克制，少比喻。")
        self.action("card", project="main", number=1, title="风格测试", goal="", tags="", refs="")
        responses = [{"body":"第一次正文。"}, review_fixture("第一次正文。")]
        with patch.object(novel, "call_model", side_effect=copy.deepcopy(responses)) as call:
            generated = self.write_and_review("generate", project="main", number=1)
            self.assertEqual(call.call_args_list[0].args[3]["context"]["writing_style"], "短句，克制，少比喻。")
            self.assertIn("context.writing_style", call.call_args_list[0].args[2])
        self.action("style", project="main", style="舒缓的长句。")
        with patch.object(novel, "call_model", side_effect=copy.deepcopy(responses)) as call:
            self.write_and_review("revise", project="main", number=1, version=generated["version"], instruction="重写开头")
            self.assertEqual(call.call_args_list[0].args[3]["context"]["writing_style"], "舒缓的长句。")
        other = self.action("new-project", name="另一本")["project"]
        self.assertEqual(self.app.state(other)["style"], "")
        self.assertEqual(novel.Workshop(self.path).state("main")["style"], "舒缓的长句。")

    def test_project_roundtrip_all_versions_and_isolation(self):
        generated = self.write_and_review("generate", number=2)["version"]
        revised = self.write_and_review("revise", number=2, version=generated, instruction="提问")["version"]
        self.action("approve", number=2, version=revised, notes="已确认")
        self.action("style", style="安静的叙述。")
        self.action("settings", role="writer", provider="deepseek", model="author", base="", key="never-export-this-key")
        data = self.app.backup("fixture")
        self.assertNotIn("never-export-this-key", json.dumps(data))
        self.assertNotIn("provider_writer", json.dumps(data))
        imported = self.action("import-project", content=data)["project"]
        restored = self.app.backup(imported)
        self.assertEqual(restored["drafts"], data["drafts"])
        self.assertEqual(restored["chapters"], data["chapters"])
        self.assertEqual(restored["style"], "安静的叙述。")
        self.assertEqual(self.app.export(imported), self.app.export("fixture"))
        self.action("style", project=imported, style="另一风格")
        self.assertEqual(self.app.state("fixture")["style"], "安静的叙述。")
        name = self.app.state(imported)["project_name"]
        with self.assertRaises(novel.AppError):
            self.action("delete-project", project=imported, confirm_name="错误名称")
        self.action("delete-project", project=imported, confirm_name=name)
        with self.assertRaises(novel.AppError):
            self.action("style", project=imported, style="不能复活")
        reopened = novel.Workshop(self.path)
        self.assertNotIn(imported, reopened.paths)
        self.assertEqual(reopened.export("fixture"), self.app.export("fixture"))
        restored_again = self.action("import-project", content=data)["project"]
        self.assertNotEqual(restored_again, imported)

    def test_legacy_migration_and_deleting_original_project(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            old = directory / "novel.sqlite3"
            novel.initialize(old)
            with novel.database(old) as db:
                db.execute("INSERT INTO chapters(number,title,goal,tags,refs,canon) VALUES (1,'旧稿','','','',NULL)")
                db.execute("INSERT INTO settings VALUES ('provider_writer',?)", (json.dumps({"provider":"deepseek","base":"https://api.deepseek.com","model":"old-model"}),))
            app = novel.Workshop(directory)
            self.assertEqual(app.state("main")["chapters"][0]["title"], "旧稿")
            self.assertEqual(app.settings["writer"]["model"], "old-model")
            app.action("main", "delete-project", {"confirm_name":"我的小说"})
            reopened = novel.Workshop(directory)
            self.assertNotIn("main", reopened.paths)
            self.assertEqual(reopened.settings["writer"]["model"], "old-model")
            with self.assertRaises(novel.AppError):
                reopened.action("", "new-project", {"name":"../../新小说"})
            new = reopened.action("", "new-project", {"name":"新小说"})["project"]
            self.assertEqual(reopened.paths[new].parent, directory)

    def test_malformed_project_never_changes_existing_data(self):
        original = self.app.backup("fixture")
        count = len(self.app.projects.entries())
        bad = copy.deepcopy(original)
        bad["chapters"][0]["canon"] = 99999
        bad2 = copy.deepcopy(original)
        bad2["drafts"][0]["report"] = {"basis":{},"issues":[]}
        for data in ({"format":"bad"}, bad, bad2, {**original, "version":True}, {**original,"filename":"../../bad"}):
            with self.assertRaises(novel.AppError):
                self.action("import-project", content=data)
        self.assertEqual(len(self.app.projects.entries()), count)
        self.assertEqual(self.app.backup("fixture"), original)

    def test_http_security_and_demo(self):
        command = [sys.executable, str(Path(novel.__file__)), "--no-browser", "--data-dir", str(self.path)]
        child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(child.stdout.close)
        self.addCleanup(child.stderr.close)
        def stop():
            child.terminate()
            child.wait(timeout=5)
        self.addCleanup(stop)
        child.stdout.readline()
        url = child.stdout.readline().strip()
        self.assertTrue(url.startswith("http://127.0.0.1:"))
        with urllib.request.urlopen(url, timeout=5) as response:
            self.assertIn("长篇工坊", response.read().decode())
        origin = url.rsplit("/", 2)[0]
        data = json.dumps({"number":2,"body":"手工正文"}).encode()
        invalid = urllib.request.Request(url+"action/manual?project=fixture", data=data,
                                         headers={"Content-Type":"application/json", "Origin":"https://invalid.test"})
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(invalid, timeout=5)
        self.assertEqual(error.exception.code, 403)
        valid = urllib.request.Request(url+"action/manual?project=fixture", data=data,
                                       headers={"Content-Type":"application/json", "Origin":origin})
        with urllib.request.urlopen(valid, timeout=5) as response:
            self.assertIn("version", json.load(response))
        content = {"format":"novel-workshop-project", "version":1, "name":"五十万字往返测试", "rules":"规则", "style":"克制", "chapters":[], "drafts":[]}
        for number in range(1, 11):
            content["chapters"].append({"number":number,"title":str(number),"goal":"","tags":"","refs":"","canon":number})
            content["drafts"].append({"id":number,"chapter":number,"body":"文" * 50000,"notes":"","report":None,"writing_basis":None,"approved":1,"created":"2026-11-08 00:00:00"})
        payload = json.dumps({"content":content}, ensure_ascii=False).encode()
        self.assertGreater(len(payload), 600_000)
        request = urllib.request.Request(url+"action/import-project?project=fixture", data=payload,
                                         headers={"Content-Type":"application/json", "Origin":origin})
        with urllib.request.urlopen(request, timeout=10) as response:
            project = json.load(response)["project"]
        with urllib.request.urlopen(url+"backup?project="+project, timeout=10) as response:
            restored = json.load(response)
        self.assertEqual(sum(len(d["body"]) for d in restored["drafts"]), 500000)
        self.assertEqual(restored["drafts"], content["drafts"])


if __name__ == "__main__":
    unittest.main()
