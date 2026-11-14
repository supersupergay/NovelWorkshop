"""Provider presets, request adapters, and shared response validation."""
from dataclasses import dataclass
from http.client import HTTPException
import json
import ipaddress
import re
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlencode


class AppError(Exception):
    pass


@dataclass(frozen=True)
class Provider:
    label: str
    base: str
    protocol: str = "chat"
    json_mode: bool = False
    model: str = ""


PROVIDERS = {
    "openai": Provider("OpenAI · GPT", "https://api.openai.com/v1", "responses", model="gpt-5-mini"),
    "anthropic": Provider("Anthropic · Claude", "https://api.anthropic.com/v1", "messages"),
    "gemini": Provider("Google AI Studio · Gemini", "https://generativelanguage.googleapis.com/v1beta/openai"),
    "mimo": Provider("小米 · MiMo", "https://api.xiaomimimo.com/v1"),
    "custom_endpoint": Provider("自定义 · 完整 API 地址", ""),
    "deepseek": Provider("DeepSeek", "https://api.deepseek.com", json_mode=True),
    "qwen": Provider("阿里云百炼 · 通义千问（北京）", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    "qwen_sg": Provider("Alibaba Cloud · Qwen（新加坡）", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"),
    "kimi": Provider("月之暗面 · Kimi（国内）", "https://api.moonshot.cn/v1"),
    "kimi_intl": Provider("Moonshot · Kimi（国际）", "https://api.moonshot.ai/v1"),
    "glm": Provider("智谱 · GLM（国内）", "https://open.bigmodel.cn/api/paas/v4"),
    "zai": Provider("Z.ai · GLM（国际）", "https://api.z.ai/api/paas/v4"),
    "ark": Provider("火山方舟 · 豆包", "https://ark.cn-beijing.volces.com/api/v3"),
    "minimax": Provider("MiniMax（国内）", "https://api.minimax.cn/v1"),
    "minimax_intl": Provider("MiniMax（国际）", "https://api.minimax.io/v1"),
    "grok": Provider("xAI · Grok", "https://api.x.ai/v1"),
    "mistral": Provider("Mistral AI", "https://api.mistral.ai/v1"),
    "siliconflow": Provider("硅基流动 · SiliconFlow", "https://api.siliconflow.cn/v1"),
    "openrouter": Provider("OpenRouter", "https://openrouter.ai/api/v1"),
    "lmstudio": Provider("LM Studio · 本地模型", "http://127.0.0.1:1234/v1"),
    "ollama": Provider("Ollama · 本地模型", "http://127.0.0.1:11434/v1"),
    "custom_chat": Provider("自定义 · OpenAI Chat Completions", ""),
    "custom_responses": Provider("自定义 · OpenAI Responses", "", "responses"),
    "custom_messages": Provider("自定义 · Anthropic Messages", "", "messages"),
}
ROLE_NAMES = {"writer": "写手", "reviewer": "审稿", "commentator": "定稿评论"}
SUFFIXES = {"chat": "/chat/completions", "responses": "/responses", "messages": "/messages"}
LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def local_address(host):
    if host in LOOPBACK:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    ranges = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
    return any(address in ipaddress.ip_network(network) for network in ranges)


def normalize_base(base, protocol):
    if not base or any(not char.isascii() or char.isspace() or ord(char) < 33 or char == "\\" for char in base):
        raise AppError("请输入有效的 API 地址。")
    try:
        parsed = urlsplit(base)
        host = parsed.hostname
        parsed.port  # Raises on a malformed port.
    except ValueError:
        raise AppError("API 地址或端口格式无效。") from None
    if not host or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise AppError("API 地址需要主机名，且不能包含用户名、密码、查询参数或片段。")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local_address(host)):
        raise AppError("公网 API 地址需要 HTTPS。本机或内网 IP 地址可以使用 HTTP。")
    path = parsed.path.rstrip("/")
    for other_protocol, suffix in SUFFIXES.items():
        if path.endswith(suffix):
            if protocol != other_protocol:
                raise AppError("API 地址的接口路径与所选协议不一致。")
            path = path[:-len(suffix)]
            break
    return f"{parsed.scheme}://{parsed.netloc.lower()}{path}"


def normalize_endpoint(value):
    if not value or any(c.isspace() or ord(c)<33 or not c.isascii() or c == "\\" for c in value):
        raise AppError("请输入完整的 HTTP 或 HTTPS API 地址；中文路径请使用 URL 编码。")
    try:
        parsed = urlsplit(value)
        parsed.port
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.fragment:
            raise ValueError()
    except ValueError:
        raise AppError("完整 API 地址必须使用 HTTP 或 HTTPS，不能含用户名、密码或片段。") from None
    return value


def endpoint(profile):
    return profile["base"] if profile["provider"] == "custom_endpoint" else profile["base"] + SUFFIXES[PROVIDERS[profile["provider"]].protocol]


def public_presets():
    return {name: {"label": provider.label, "base": provider.base, "model": provider.model,
                   "suffix": "" if name == "custom_endpoint" else SUFFIXES[provider.protocol]} for name, provider in PROVIDERS.items()}


def validate_destination(profile):
    """Named cloud presets cannot accidentally send their key to another host."""
    provider = PROVIDERS[profile["provider"]]
    if profile["provider"] == "custom_endpoint":
        normalize_endpoint(profile["base"])
        if profile.get("protocol", "chat") not in SUFFIXES:
            raise AppError("请选择正确的接口协议。")
        return
    base = normalize_base(profile["base"], provider.protocol)
    if base != profile["base"]:
        raise AppError("API 地址尚未规范化，请重新应用设置。")
    if not profile["provider"].startswith("custom_") and profile["provider"] not in {"ollama", "lmstudio"}:
        expected = urlsplit(provider.base)
        actual = urlsplit(base)
        if (actual.hostname, actual.port or 443) != (expected.hostname, expected.port or 443):
            raise AppError(f"{provider.label} 预设仅允许请求 {expected.hostname}。如使用其他网站，请明确选择自定义接口并填写该网站的密钥。")


def needs_key(base):
    return not local_address(urlsplit(base).hostname)


class RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(url, payload, headers, timeout):
    request = urllib.request.Request(url, data=None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                    headers={"Content-Type": "application/json", **headers}, method="GET" if payload is None else "POST")
    handlers = [RefuseRedirects(), urllib.request.ProxyHandler({})]
    try:
        with urllib.request.build_opener(*handlers).open(request, timeout=timeout) as response:
            output = json.loads(response.read(4_000_001))
    except urllib.error.HTTPError as exc:
        errors = {400: "请核对模型 ID、接口协议或模型上下文限制", 401: "密钥验证失败，请检查账号与接口区域",
                  402: "请检查账户余额", 403: "当前密钥没有权限", 404: "请检查 API 地址和模型 ID",
                  413: "本次输入过长", 429: "请检查额度、限流或稍后重试"}
        detail = ("为保护密钥，程序拒绝跟随重定向" if 300 <= exc.code < 400 else
                  errors.get(exc.code, "服务暂时无法完成请求"))
        error = AppError(f"HTTP {exc.code}。{detail}。")
        if exc.code == 429:
            try:
                error.retry_after = min(10.0, max(1.0, float(exc.headers.get("Retry-After", "2"))))
            except (ValueError, TypeError, AttributeError):
                error.retry_after = 2.0
        raise error from None
    except (OSError, HTTPException):
        raise AppError("连接失败或超时，请检查网络、地址及服务是否已启动。") from None
    except ValueError:
        raise AppError("API 返回的内容无法解析，请检查地址和接口协议。") from None
    if not isinstance(output, dict) or output.get("error"):
        raise AppError("API 返回错误，请检查密钥、模型和账户额度。")
    return output


def list_models(profile):
    validate_destination(profile)
    if profile["provider"] == "custom_endpoint":
        raise AppError("完整地址模式不猜测模型列表地址，请手动填写模型 ID。")
    if needs_key(profile["base"]) and not profile["key"]:
        raise AppError("请填写所选 API 服务商的密钥。")
    protocol = PROVIDERS[profile["provider"]].protocol
    headers = {"Authorization": f"Bearer {profile['key']}"} if profile["key"] else {}
    if protocol == "messages":
        headers = {"anthropic-version": "2023-06-01",
                   **({"x-api-key": profile["key"]} if profile["key"] else {})}
    if profile["provider"] == "mimo" and profile["key"]:
        headers = {"api-key": profile["key"]}
    models = {}
    cursor = ""
    cursors = set()
    for _ in range(10):
        url = profile["base"] + "/models"
        if cursor:
            url += "?" + urlencode({"after_id": cursor})
        try:
            response = request_json(url, None, headers, 30)
        except AppError as exc:
            raise AppError(f"获取模型列表失败。{exc} 可以从服务商控制台复制模型 ID 手动填写。") from None
        entries = response.get("data")
        if not isinstance(entries, list):
            raise AppError("服务商没有返回兼容的模型列表，请手动填写模型 ID。")
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            identifier = entry.get("id")
            if isinstance(identifier, str) and identifier.strip():
                name = entry.get("display_name") or entry.get("name") or identifier
                models[identifier] = {"id": identifier, "name": name if isinstance(name, str) else identifier}
        if not response.get("has_more"):
            return {"models": list(models.values()), "truncated": False}
        cursor = response.get("last_id")
        if protocol != "messages" or not isinstance(cursor, str) or not cursor or cursor in cursors:
            break
        cursors.add(cursor)
    return {"models": list(models.values()), "truncated": True}


def request_spec(profile, instructions, payload, schema, schema_name, tokens):
    validate_destination(profile)
    provider = PROVIDERS[profile["provider"]]
    protocol = profile.get("protocol", "chat") if profile["provider"] == "custom_endpoint" else provider.protocol
    url = endpoint(profile)
    key = profile["key"]
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    if profile["provider"] == "mimo" and key:
        headers = {"api-key": key}
    user_input = json.dumps(payload, ensure_ascii=False)
    body = {"model": profile["model"]}
    if protocol == "responses":
        body.update(instructions=instructions, input=user_input, store=False, max_output_tokens=tokens,
                    text={"format": {"type": "json_schema", "name": schema_name,
                                     "strict": True, "schema": schema}})
    else:
        instructions += ("\n仅返回一个 JSON 对象。不要附带 Markdown、分析或解释。"
                         "\n遵守这个 JSON Schema：\n" + json.dumps(schema, ensure_ascii=False))
        user = {"role": "user", "content": user_input}
        body["max_tokens"] = tokens
        if protocol == "messages":
            headers = {"anthropic-version": "2023-06-01", **({"x-api-key": key} if key else {})}
            body.update(system=instructions, messages=[user])
        else:
            body["messages"] = [{"role": "system", "content": instructions}, user]
            if provider.json_mode:
                body["response_format"] = {"type": "json_object"}
    return protocol, url, body, headers


def extract_text(protocol, response):
    try:
        if protocol == "responses":
            if response.get("status") not in (None, "completed"):
                raise AppError("模型未返回完整结果。")
            parts = [part for item in response["output"] if item.get("type") == "message"
                     for part in item["content"]]
            if any(part.get("type") == "refusal" for part in parts):
                raise AppError("模型拒绝了本次请求。")
            return "".join(part["text"] for part in parts if part.get("type") == "output_text")
        if protocol == "messages":
            if response.get("stop_reason") == "max_tokens":
                raise AppError("模型输出达到长度上限，结果未完成。")
            if response.get("stop_reason") not in (None, "end_turn", "stop_sequence"):
                raise AppError("模型响应中断或拒绝了本次请求。")
            return "".join(part["text"] for part in response["content"] if part.get("type") == "text")
        choice = response["choices"][0]
        if choice.get("finish_reason") not in (None, "stop", "end_turn", "eos_token"):
            raise AppError("模型未完整输出正文或审稿结果。")
        message = choice["message"]
        if message.get("refusal"):
            raise AppError("模型拒绝了本次请求。")
        content = message.get("content")
        if isinstance(content, list):
            return "".join(part["text"] for part in content if part.get("type") in {"text", "output_text"})
        if isinstance(content, str):
            return content
    except (KeyError, IndexError, AttributeError, TypeError):
        raise AppError("响应格式与所选接口协议不匹配。") from None
    raise AppError("模型没有返回文本。")


def matches_schema(value, schema):
    kind = schema["type"]
    if kind == "object":
        properties = schema["properties"]
        return (isinstance(value, dict) and set(value) == set(properties)
                and all(matches_schema(value[name], field) for name, field in properties.items()))
    if kind == "array":
        return isinstance(value, list) and all(matches_schema(item, schema["items"]) for item in value)
    return isinstance(value, str) and ("enum" not in schema or value in schema["enum"])


def normalize_result(value, schema):
    """Tolerate harmless reviewer formatting differences, never invent an assessment."""
    fields = schema.get("properties", {})
    if not {"issues", "logic", "ai_style", "pacing"} <= set(fields) or not isinstance(value, dict):
        return value
    result = {key: value[key] for key in fields if key in value}
    if result.get("notes") is None:
        result["notes"] = ""
    if isinstance(result.get("issues"), list):
        cleaned = []
        for issue in result["issues"]:
            if not isinstance(issue, dict):
                return result
            item = {key: issue[key] for key in fields["issues"]["items"]["properties"] if key in issue}
            for key in ("source_id", "source_quote"):
                if item.get(key) is None:
                    item[key] = ""
            if isinstance(item.get("kind"), str):
                item["kind"] = {"AI 痕迹":"AI痕迹", "疑似AI痕迹":"AI痕迹", "行文节奏":"节奏", "逻辑错误":"内部逻辑", "章内逻辑":"内部逻辑"}.get(item["kind"].strip(), item["kind"].strip())
            if isinstance(item.get("severity"), str):
                item["severity"] = {"high":"高", "medium":"中", "low":"低", "高风险":"高", "中风险":"中", "低风险":"低"}.get(item["severity"].strip().lower(), item["severity"].strip())
            cleaned.append(item)
        result["issues"] = cleaned
    return result


def parse_result(text, schema):
    text = re.sub(r"^\s*<think>.*?</think>\s*", "", text, count=1, flags=re.DOTALL).strip()
    fenced = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n?```", text, flags=re.IGNORECASE | re.DOTALL)
    candidate = fenced[1] if fenced else text
    try:
        result = normalize_result(json.loads(candidate), schema)
    except ValueError:
        # Find complete top-level objects; do not salvage a nested object from
        # malformed JSON, or silently choose between multiple valid reports.
        decoder = json.JSONDecoder()
        candidates = []
        position = 0
        while position < len(candidate):
            start = candidate.find("{", position)
            if start < 0:
                break
            try:
                value, end = decoder.raw_decode(candidate[start:])
            except ValueError:
                raise AppError("模型没有返回有效的 JSON。请重试，或换一个支持指令的文本模型。") from None
            value = normalize_result(value, schema)
            if matches_schema(value, schema):
                candidates.append(value)
            position = start + end
        if len(candidates) != 1:
            raise AppError("模型没有返回有效的 JSON。请重试，或换一个支持指令的文本模型。") from None
        result = candidates[0]
    if not matches_schema(result, schema):
        raise AppError("模型输出字段不符合预期。请重试或更换模型。")
    if {"logic", "ai_style", "pacing"} <= set(schema.get("properties", {})):
        if any(not result[key].strip() for key in ("logic", "ai_style", "pacing")):
            raise AppError("模型审稿字段为空，没有完成三项审查。")
        missing = r"(?:未|没有)(?:收到|提供|看到|读取到)(?:任何|实际|待审|本章|完整|有效)?的?(?:正文|草稿|文本)|请(?:先)?(?:提供|发送|上传|粘贴)(?:待审|本章|完整)?的?(?:正文|草稿)|(?:正文|草稿)(?:内容)?(?:为空|缺失|未提供|未收到)"
        if sum(bool(re.search(missing, result[key])) for key in ("logic", "ai_style", "pacing")) >= 2:
            raise AppError("模型未识别到已发送的完整正文，没有完成审查。")
    return result


def call_model(profile, role, instructions, payload, schema, test=False):
    provider = PROVIDERS[profile["provider"]]
    label = f"{ROLE_NAMES[role]} · {provider.label} · {profile['base']}{SUFFIXES[provider.protocol]}"
    try:
        if not profile["model"]:
            raise AppError("请先在模型设置中填写模型 ID。")
        if profile["provider"] != "custom_endpoint" and not profile["key"] and needs_key(profile["base"]):
            raise AppError("请先在模型设置中填写此角色的 API 密钥。")
        tokens = (1024 if provider.protocol == "responses" else 2048) if test else (16000 if role == "writer" else 8000)
        for attempt in range(2 if role == "reviewer" and not test else 1):
            extra = "" if not attempt else "\n上次返回为空、截断、格式不正确。重新阅读本次完整正文，严格按 JSON Schema 输出，三项评述简洁，最多列 8 项问题。"
            protocol, url, body, headers = request_spec(profile, instructions + extra, payload, schema,
                                                        "connection_check" if test else role, tokens)
            try:
                response = request_json(url, body, headers, timeout=60 if test else 240)
                result = parse_result(extract_text(protocol, response), schema)
                return result
            except AppError as exc:
                retryable = any(term in str(exc) for term in (
                    "JSON", "字段", "文本", "完整", "长度上限",
                    "连接失败或超时", "HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504", "API 返回的内容无法解析"))
                if attempt or role != "reviewer" or test or not retryable or "拒绝" in str(exc):
                    raise
                if any(term in str(exc) for term in ("HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504", "连接失败或超时")):
                    time.sleep(getattr(exc, "retry_after", 1.0))
    except AppError as exc:
        raise AppError(f"{label}：{exc}") from None
