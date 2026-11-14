"""Synthetic fixtures used only by tests; not bundled in the executable."""
from novel import *
DEMO_SOURCE = "沈岚看见周砚把黄铜钥匙藏进抽屉。隔着玻璃，她没听见周砚对电话那头说了什么。"
DEMO_WRONG = "“你把钥匙藏起来，是为了替梁叔隐瞒证据。”沈岚对周砚说。"
DEMO_FIXED = "“你把钥匙藏起来，是为了替梁叔隐瞒证据？”沈岚问。她盯着他按在抽屉上的手，等他说点什么。"
def write_body(demo, settings, context, base=None, instruction=""):
    if demo:
        if context["card"]["number"] != 2:
            raise AppError("内置写手只生成示例第 2 章。可在其他章节手动存稿。")
        return ("楼道灯亮了。沈岚没有进去。\n\n" +
                (DEMO_FIXED if base else DEMO_WRONG) +
                "\n\n周砚把抽屉合上。灯灭之前，两人都没再开口。")
    payload = {"context": context, "previous_draft": base or "", "revision_instruction": instruction}
    value = call_model(settings["writer"], "writer", WRITER, payload, WRITE_SCHEMA)["body"]
    return clean_text(value, "生成正文", 80000, required=True)


def review_body(demo, settings, context, body):
    if demo:
        source = next((item for item in context["sources"] if DEMO_SOURCE in item["body"]), None)
        issues = []
        if DEMO_WRONG in body and source:
            issues = [{"kind": "铺垫不足", "severity": "高", "draft_quote": DEMO_WRONG,
                       "source_id": source["id"], "source_quote": DEMO_SOURCE,
                       "reason": "沈岚看到了钥匙，却没有听见电话，也没有其他渠道得知藏钥匙的动机。",
                       "suggestion": "把断言改成试探性的提问，或先安排沈岚获得证据。"}]
        raw = {"issues": issues, "notes": "人物知情｜沈岚仍未确认周砚藏钥匙的动机。"}
    else:
        body = clean_text(body, "待审正文", 80000, True)
        payload = {"task": "审查下方完整 draft 正文，完成逻辑、AI痕迹、行文节奏三项审查", "draft": body,
                   "draft_characters": str(len(body)), "context": context}
        raw = call_model(settings["reviewer"], "reviewer", REVIEWER, payload, REVIEW_SCHEMA)
    if not isinstance(raw["issues"], list):
        raise AppError("审稿问题清单格式异常。")
    sources = {item["id"]: item["body"] for item in context["sources"]}
    sources["RULES"] = context["rules"]
    sources["DRAFT"] = body
    for issue in raw["issues"]:
        if (not isinstance(issue, dict) or set(issue) != set(ISSUE_FIELDS)
                or any(not isinstance(value, str) for value in issue.values())
                or issue["kind"] not in ISSUE_FIELDS["kind"]["enum"]
                or issue["severity"] not in ISSUE_FIELDS["severity"]["enum"]):
            raise AppError("审稿问题字段格式异常。")
        issue["draft_verified"] = contains_quote(body, issue["draft_quote"])
        issue["source_verified"] = contains_quote(sources.get(issue["source_id"], ""), issue["source_quote"])
        if not issue["draft_verified"] or (not issue["source_verified"] and not (issue["kind"] in {"AI痕迹", "节奏", "内部逻辑"} and not issue["source_id"] and not issue["source_quote"])):
            issue["kind"] = "待核实"
    for key in ("logic", "ai_style", "pacing"):
        raw[key] = clean_text(raw.get(key, "内置演示不模拟此项文学评价。" if demo else ""), "审稿评述", 16000)
    raw.pop("receipt", None)
    raw["notes"] = clean_text(raw["notes"], "台账建议", 16000)
    raw["basis"] = context["basis"]
    raw["draft_characters"] = str(len(body))
    raw["draft_sha256"] = hashlib.sha256(body.encode()).hexdigest()
    raw["mode"] = "demo" if demo else "api"
    if not demo:
        raw["provider"] = settings["reviewer"]["provider"]
        raw["model"] = settings["reviewer"]["model"]
    return raw



def seed_fixture(app):
    identifier = app.projects.create('测试项目')
    with database(app.projects.catalog) as db:
        db.execute("UPDATE projects SET id='fixture' WHERE id=?", (identifier,))
    app.paths = app.projects.paths()
    app.action('fixture','rules',{'rules':'沈岚只能凭亲眼见到或亲耳听到的信息推理。'})
    app.action('fixture','card',{'number':1,'title':'窗外','goal':'沈岚目睹藏钥匙','tags':'沈岚，周砚，钥匙','refs':''})
    version = app.action('fixture','manual',{'number':1,'body':DEMO_SOURCE})['version']
    app.action('fixture','approve',{'number':1,'version':version,'override':True})
    app.action('fixture','card',{'number':2,'title':'试探','goal':'沈岚试探周砚藏钥匙的原因','tags':'沈岚，周砚，钥匙','refs':'1'})
