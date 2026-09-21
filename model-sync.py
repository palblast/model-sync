#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 模型自动同步引擎。

从 providers/v1/models 端点拉取各 AI 提供商可用模型列表，
智能合并到 ~/.workbuddy/models.json，让 WorkBuddy 获得
CherryStudio 风格的"一键导入全部模型"能力。

支持的协议：
    - openai:    GET {baseUrl}/models,  Header: Authorization Bearer
    - anthropic: GET {baseUrl}/v1/models, Header: x-api-key
    - gemini:    GET {baseUrl}/v1beta/models?key={apiKey}
    - ollama:    GET {baseUrl}/api/tags

用法：
    python model-sync.py                        # 正常同步（自动合并）
    python model-sync.py --dry-run              # 演练，不写入文件
    python model-sync.py --verbose              # 详细日志
    python model-sync.py --save-list            # 同步后保存全量列表到 latest-models.json
    python model-sync.py --import-selected      # 从 selected-models.json 选择性导入
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Windows: 强制 stdout/stderr 输出 UTF-8，避免 subprocess 调用时编码乱码
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# Windows 用户 home 解析
HOME = Path(os.path.expanduser("~"))
BASE_DIR = Path(__file__).resolve().parent
MODELS_JSON_PATH = HOME / ".workbuddy" / "models.json"
PROVIDERS_JSON_PATH = BASE_DIR / "model-providers.json"
SYNC_STATE_PATH = BASE_DIR / "sync-state.json"
SYNC_REPORT_PATH = BASE_DIR / "sync-report.log"
LATEST_MODELS_PATH = BASE_DIR / "latest-models.json"
SELECTED_MODELS_PATH = BASE_DIR / "selected-models.json"
# [Key 隔离] 密钥单独存放，不进 git。可用环境变量覆盖。
SECRETS_JSON_PATH = Path(os.environ.get("MS_SECRETS", str(BASE_DIR / "secrets.json")))

REQUEST_TIMEOUT = 30  # seconds


# ---------------------------------------------------------------------------
# [Key 隔离] 密钥解析层
# ---------------------------------------------------------------------------
def load_secrets() -> Dict[str, str]:
    """读取 secrets.json（{providerId: apiKey}）。文件缺失返回空 dict。"""
    if not SECRETS_JSON_PATH.exists():
        return {}
    try:
        raw = json.loads(SECRETS_JSON_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            print(f"  ! secrets.json 格式错误（应为对象）: {SECRETS_JSON_PATH}",
                  file=sys.stderr)
            return {}
        return {str(k): str(v) for k, v in raw.items() if v}
    except Exception as e:
        print(f"  ! 读取 secrets.json 失败: {e}", file=sys.stderr)
        return {}


def resolve_api_key(provider: Dict[str, Any], secrets: Optional[Dict[str, str]] = None) -> str:
    """解析 provider 的真实 API Key。

    优先级：
      1. apiKeyRef -> 查 secrets.json
      2. 兼容旧配置：provider 内联的 apiKey
    这样旧配置文件无需立即改造也能继续工作。
    """
    if secrets is None:
        secrets = load_secrets()
    ref = (provider.get("apiKeyRef") or "").strip()
    if ref:
        key = secrets.get(ref, "")
        if key:
            return key
        # ref 存在但查不到：明确报错，避免静默用空 Key 去请求
        print(f"  ! [Key隔离] providerId={provider.get('providerId')} 的 "
              f"apiKeyRef='{ref}' 在 secrets.json 中不存在", file=sys.stderr)
        return ""
    # 回退：旧式内联 apiKey
    return (provider.get("apiKey") or "").strip()

# 浏览器样式的 User-Agent，避免请求被 Cloudflare 等反爬拦截（返回 403 / error 1010）
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


# ---------------------------------------------------------------------------
# 协议适配器
# ---------------------------------------------------------------------------
class ProtocolAdapter:
    """协议适配器基类。"""

    protocol: str = "base"

    # [Key 隔离] 密钥表缓存，由 sync() 在开始时注入，避免每次请求都读文件
    _secrets: Optional[Dict[str, str]] = None

    @classmethod
    def set_secrets(cls, secrets: Dict[str, str]) -> None:
        """由主流程注入已加载的密钥表。"""
        cls._secrets = secrets

    def list_models(self, provider: Dict[str, Any]) -> List[str]:
        """返回该 provider 可用的模型 id 列表。子类必须实现。"""
        raise NotImplementedError

    @staticmethod
    def _get(url: str, headers: Dict[str, str]) -> Any:
        """发起 GET 请求，返回解析后的 JSON。失败抛 urllib.error.HTTPError。"""
        # 添加浏览器 UA，避免被 Cloudflare 等反爬判定为机器人而返回 403/1010
        h = dict(headers)
        h.setdefault("User-Agent", DEFAULT_USER_AGENT)
        req = urllib.request.Request(url, headers=h, method="GET")
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            raw = resp.read().decode(charset, errors="replace")
        return json.loads(raw)


class OpenAIAdapter(ProtocolAdapter):
    """OpenAI 兼容协议。覆盖 DeepSeek/Moonshot/SiliconFlow/智谱/OpenAI/OpenRouter 等。"""

    protocol = "openai"

    def list_models(self, provider: Dict[str, Any]) -> List[str]:
        base = provider["baseUrl"].rstrip("/")
        url = f"{base}/models"
        headers = {"Authorization": f"Bearer {resolve_api_key(provider, self._secrets)}"}
        data = self._get(url, headers)
        items = data.get("data", []) if isinstance(data, dict) else []
        return [m.get("id", "") for m in items if m.get("id")]


class AnthropicAdapter(ProtocolAdapter):
    """Anthropic 原生协议。"""

    protocol = "anthropic"

    def list_models(self, provider: Dict[str, Any]) -> List[str]:
        base = provider["baseUrl"].rstrip("/")
        url = f"{base}/v1/models"
        headers = {
            "x-api-key": resolve_api_key(provider, self._secrets),
            "anthropic-version": "2023-06-01",
        }
        data = self._get(url, headers)
        items = data.get("data", []) if isinstance(data, dict) else []
        return [m.get("id", "") for m in items if m.get("id")]


class GeminiAdapter(ProtocolAdapter):
    """Google Gemini 原生协议。"""

    protocol = "gemini"

    def list_models(self, provider: Dict[str, Any]) -> List[str]:
        base = provider["baseUrl"].rstrip("/")
        url = f"{base}/v1beta/models?key={resolve_api_key(provider, self._secrets)}"
        data = self._get(url, {})
        items = data.get("models", []) if isinstance(data, dict) else []
        out: List[str] = []
        for m in items:
            name = m.get("name", "")
            # "models/gemini-1.5-flash" -> "gemini-1.5-flash"
            out.append(name.split("/", 1)[-1] if name else "")
        return [x for x in out if x]


class CloudflareAdapter(ProtocolAdapter):
    """Cloudflare Workers AI 协议。

    与标准 OpenAI 不同，Cloudflare 使用 REST API：
        baseUrl 形如 https://api.cloudflare.com/client/v4/accounts/{id}/ai/run/
        模型列表接口为 {baseUrl 去掉 /run}/ai/models/search，分页返回。
        返回结构: { "success": true, "result": [{"id": <uuid>, "name": "@cf/..."}] }
    这里把 result[].name 作为模型 id 返回（调用推理接口时使用的模型名）。
    """

    protocol = "cloudflare"

    def list_models(self, provider: Dict[str, Any]) -> List[str]:
        base = provider["baseUrl"].rstrip("/")
        # baseUrl 形如 .../client/v4/accounts/{id}/ai/run/
        # 去掉末尾的 /run 和 /ai，得到账号级根路径 .../accounts/{id}
        for suffix in ("/run", "/ai"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
        headers = {"Authorization": f"Bearer {resolve_api_key(provider, self._secrets)}"}
        per_page = 100
        page = 1
        ids: List[str] = []
        while True:
            url = f"{base}/ai/models/search?per_page={per_page}&page={page}"
            data = self._get(url, headers)
            result = data.get("result", []) if isinstance(data, dict) else []
            for m in result:
                name = m.get("name", "")
                if name:
                    ids.append(name)
            # 如果本页没取满，说明已是最后一页
            if len(result) < per_page:
                break
            page += 1
        return ids


class OllamaAdapter(ProtocolAdapter):
    """Ollama 本地协议。"""

    protocol = "ollama"

    def list_models(self, provider: Dict[str, Any]) -> List[str]:
        base = provider["baseUrl"].rstrip("/")
        url = f"{base}/api/tags"
        data = self._get(url, {})
        items = data.get("models", []) if isinstance(data, dict) else []
        return [m.get("name", "") for m in items if m.get("name")]


ADAPTERS: Dict[str, ProtocolAdapter] = {
    "openai": OpenAIAdapter(),
    "anthropic": AnthropicAdapter(),
    "gemini": GeminiAdapter(),
    "ollama": OllamaAdapter(),
    "cloudflare": CloudflareAdapter(),
}


# ---------------------------------------------------------------------------
# 能力推断
# ---------------------------------------------------------------------------
def _match_any(name_lower: str, patterns: List[str]) -> bool:
    """任一模式匹配返回 True。'*' 表示全部命中。"""
    if not patterns:
        return False
    if "*" in patterns:
        return True
    return any(p and p in name_lower for p in patterns)


# 思考强度的默认档位与默认值。
# 说明：不同服务商官方支持的档位并不一致（详见 README「关于思考模式规则」），
# 因此档位列表与默认值都允许在 provider 的 modelRules 里按服务商覆盖。
# 默认档位取 low/medium/high —— 这三档被各主流服务商普遍接受；
# 默认值取 high —— 多数服务商（DeepSeek、GLM 等）官方推荐的高强度档。
DEFAULT_REASONING_EFFORTS = ["low", "medium", "high"]
DEFAULT_REASONING_EFFORT = "high"


def _normalize_efforts(value: Any) -> List[str]:
    """把配置里的档位列表规整为字符串列表；非法输入回退到默认档位。"""
    if isinstance(value, (list, tuple)):
        out = [str(v).strip() for v in value if str(v).strip()]
        if out:
            return out
    return list(DEFAULT_REASONING_EFFORTS)


def infer_capabilities(
    model_id: str, rules: Dict[str, List[str]]
) -> Dict[str, Any]:
    """按 provider 配置的 modelRules 推断模型能力标记。

    规则字段（均可缺省）：
        reasoningPatterns        正向匹配：命中则标记"支持思考"
        excludeReasoningPatterns 反向排除：命中则不算思考（如 embedding/rerank 等非对话模型）
        visionPatterns           正向匹配：命中则标记"支持视觉"
        toolCallPatterns         正向匹配：命中则标记"支持工具调用"
        excludeToolCallPatterns  反向排除：命中则不标记工具调用
        reasoningEfforts         该服务商官方支持的思考档位列表（覆盖默认值）
        defaultReasoningEffort   默认思考档位（覆盖默认值，默认 high）

    返回 dict 含 supportsToolCall / supportsImages / supportsReasoning
    以及（若 supportsReasoning=True）reasoning.supportedEfforts 与 defaultEffort。
    """
    name_lower = model_id.lower()
    reasoning_p = rules.get("reasoningPatterns", [])
    exclude_reasoning_p = rules.get("excludeReasoningPatterns", [])
    vision_p = rules.get("visionPatterns", [])
    tool_p = rules.get("toolCallPatterns", [])
    exclude_tool_p = rules.get("excludeToolCallPatterns", [])

    # reasoning: 先看正向匹配，再看排除项（排除项优先，避免 embed/rerank 被误标）
    supports_reasoning = _match_any(name_lower, reasoning_p) and not _match_any(
        name_lower, exclude_reasoning_p
    )
    supports_images = _match_any(name_lower, vision_p)

    # tool call: 先看正向匹配，再看排除项
    if _match_any(name_lower, tool_p):
        supports_tool_call = not _match_any(name_lower, exclude_tool_p)
    else:
        supports_tool_call = False

    result: Dict[str, Any] = {
        "supportsToolCall": bool(supports_tool_call),
        "supportsImages": bool(supports_images),
        "supportsReasoning": bool(supports_reasoning),
    }
    if supports_reasoning:
        efforts = _normalize_efforts(rules.get("reasoningEfforts"))
        # 默认档位必须落在可选档位内，否则回退到档位列表中的 high（无 high 则取末项）
        wanted = str(rules.get("defaultReasoningEffort") or DEFAULT_REASONING_EFFORT).strip()
        if wanted not in efforts:
            wanted = "high" if "high" in efforts else efforts[-1]
        result["reasoning"] = {
            "supportedEfforts": efforts,
            "defaultEffort": wanted,
        }
    return result


# ---------------------------------------------------------------------------
# 智能合并
# ---------------------------------------------------------------------------
def _url_matches(url: Optional[str], base_url: str) -> bool:
    """判断 models.json 中的 url 是否归属该 provider 的 baseUrl。

    逐级宽松：完全相等 -> 前缀匹配 -> host 段相等。
    """
    if not url:
        return False
    u = url.rstrip("/").lower()
    b = base_url.rstrip("/").lower()
    if u == b:
        return True
    if u.startswith(b):
        return True
    # host 段匹配（处理 /v1 后缀不同的情况）
    try:
        from urllib.parse import urlparse

        uh = urlparse(u).netloc
        bh = urlparse(b).netloc
        if uh and bh and uh == bh:
            return True
    except Exception:
        pass
    return False


def merge_models(
    existing: List[Dict[str, Any]],
    provider: Dict[str, Any],
    fetched_ids: List[str],
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """把 provider 拉取到的模型合并进 existing 列表。

    返回 (新列表, 变更统计)。
    - 新增: models.json 中没有但 fetched 有
    - 更新: models.json 中已有且 url 匹配本 provider，更新能力标记等字段
    - 移除: models.json 中 url 匹配本 provider，但 fetched 不再含有
    - 保留: models.json 中 url 不匹配本 provider 的（手动添加的），完全不动
    """
    provider_id = provider["providerId"]
    base_url = provider.get("baseUrl", "")
    rules = provider.get("modelRules", {})

    fetched_set = set(fetched_ids)
    result: List[Dict[str, Any]] = []
    existing_managed: Dict[str, Dict[str, Any]] = {}  # id -> entry（本 provider 管的）
    manual: List[Dict[str, Any]] = []

    for entry in existing:
        entry_pid = entry.get("providerId")
        # 若条目已标记归属某个 provider，则只归属到匹配的 provider
        if entry_pid:
            if entry_pid == provider_id:
                existing_managed[entry.get("id", "")] = entry
            else:
                manual.append(entry)
            continue
        # 旧数据无 providerId 标记：按 baseUrl 匹配（兼容处理）
        if _url_matches(entry.get("url"), base_url):
            existing_managed[entry.get("id", "")] = entry
        else:
            manual.append(entry)

    stats = {"added": 0, "updated": 0, "removed": 0, "kept": 0}

    # 1. 被本 provider 管的现有条目: 检查是否仍存在
    provider_has_name_rule = bool(provider.get("namePrefix") or provider.get("nameSuffix"))
    for mid, entry in existing_managed.items():
        if mid in fetched_set:
            new_val = _build_entry(mid, provider, rules)
            # 未配置名称规则时：
            # - 已有 name 等于旧默认值(id) 或未设置 -> 更新为"提供商名/id"
            # - 已有 name 已被用户手动修改 -> 保留，不覆盖
            if not provider_has_name_rule and entry.get("name"):
                default_display = provider.get("name", "Custom") + "/" + mid
                old_name = entry.get("name")
                if old_name != mid and old_name != default_display:
                    new_val.pop("name", None)  # 保留手动修改
            # 更新字段（保留 vendor 显式标注 provider 名）
            entry.update(new_val)
            result.append(entry)
            stats["updated"] += 1
        else:
            # 该 provider 不再提供 -> 移除
            stats["removed"] += 1

    # 2. fetched 中新增的
    for mid in fetched_set:
        if mid in existing_managed:
            continue
        entry = _build_entry(mid, provider, rules)
        result.append(entry)
        stats["added"] += 1

    # 3. 手动添加的: 原封不动 加入结果
    for entry in manual:
        result.append(entry)
        stats["kept"] += 1

    return result, stats


# ---------------------------------------------------------------------------
# 原始厂商映射（从模型 ID 前缀推断）
# ---------------------------------------------------------------------------
ORIGINAL_VENDOR_MAP: Dict[str, str] = {
    # NVIDIA
    "nvidia": "NVIDIA",
    "nv-mistralai": "NVIDIA Mistral",
    "nv-google": "NVIDIA Google",
    # 海外大厂
    "google": "Google",
    "meta": "Meta",
    "microsoft": "Microsoft",
    "openai": "OpenAI",
    "amazon": "Amazon",
    "apple": "Apple",
    "ibm": "IBM",
    # 主流 AI 公司
    "mistralai": "Mistral",
    "anthropic": "Anthropic",
    "cohere": "Cohere",
    "ai21labs": "AI21 Labs",
    "snowflake": "Snowflake",
    "databricks": "Databricks",
    "poolside": "Poolside",
    "adept": "Adept",
    "sarvamai": "Sarvam AI",
    "zyphra": "Zyphra",
    "upstage": "Upstage",
    "abacusai": "Abacus AI",
    "writer": "Writer",
    "thinkingmachines": "Thinking Machines",
    "bigcode": "BigCode",
    "aisingapore": "AI Singapore",
    "snowflake": "Snowflake",
    # 国内厂商
    "deepseek-ai": "DeepSeek",
    "moonshotai": "Moonshot (Kimi)",
    "z-ai": "z-ai (智谱)",
    "qwen": "Qwen (通义千问)",
    "stepfun-ai": "StepFun (阶跃星辰)",
    "minimaxai": "MiniMax",
    "01-ai": "01.AI (零一万物)",
    "baai": "BAAI (智源)",
    "bytedance": "ByteDance (字节跳动)",
    "internlm": "InternLM (书生)",
    "colossal-ai": "Colossal-AI",
    "siliconflow": "SiliconFlow",
    "zhipu": "智谱 GLM",
    # 高校/研究机构
    "stabilityai": "Stability AI",
    "huggingface": "Hugging Face",
    "togethercomputer": "Together Computer",
}


def _infer_original_vendor(model_id: str) -> str:
    """从模型 ID 推断原始开发厂商。

    规则：
    1. 如果模型 ID 包含 '/'，取前缀查映射表
    2. 查不到映射的，把前缀格式化后返回
    3. 没有 '/' 的，返回 'Other'
    """
    if "/" in model_id:
        prefix = model_id.split("/")[0].lower()
        if prefix in ORIGINAL_VENDOR_MAP:
            return ORIGINAL_VENDOR_MAP[prefix]
        # 没有映射：格式化前缀
        return prefix.replace("-", " ").replace("_", " ").title().strip()
    return "Other"


def _build_entry(model_id: str, provider: Dict[str, Any], rules: Dict[str, List[str]]) -> Dict[str, Any]:
    """构造一个 WorkBuddy models.json 条目。

    显示名称(name)生成规则（仅影响界面显示，不影响实际请求）：
    - 若配置了 namePrefix / nameSuffix：name = namePrefix + id + nameSuffix
    - 否则自动用提供商名称：name = 提供商名 + "/" + id
    """
    prefix = provider.get("namePrefix") or ""
    suffix = provider.get("nameSuffix") or ""
    provider_name = provider.get("name", "Custom")
    if prefix or suffix:
        display_name = prefix + model_id + suffix
    else:
        display_name = provider_name + "/" + model_id
    entry: Dict[str, Any] = {
        "id": model_id,
        "name": display_name,
        "vendor": provider_name,
        "originalVendor": _infer_original_vendor(model_id),
        "url": provider.get("baseUrl", ""),
        # [Key 隔离] 这里必须写入【真实 Key】而非 apiKeyRef：
        # latest-models.json 供 Web UI 使用（切换 Key 弹窗依赖 apiKey 字段），
        # 且 import_selected() 会把它直接写入 ~/.workbuddy/models.json，
        # 而 WorkBuddy 要求该文件含真实 apiKey。
        # latest-models.json 已在 .gitignore 中，不进版本库。
        "apiKey": resolve_api_key(provider),
        "useCustomProtocol": False,
        "providerId": provider.get("providerId", ""),
    }
    entry.update(infer_capabilities(model_id, rules))
    return entry


# ---------------------------------------------------------------------------
# 原子写入
# ---------------------------------------------------------------------------
def atomic_write_json(path: Path, data: Any) -> bool:
    """原子写入 JSON 文件：先写 .tmp -> 校验 -> 替换。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        # 校验临时文件可正常加载
        with open(tmp, "r", encoding="utf-8") as f:
            json.load(f)
        os.replace(tmp, path)
        return True
    except Exception as e:
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass
        print(f"  ! 原子写入失败 {path}: {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# 同步状态与日志
# ---------------------------------------------------------------------------
def write_sync_state(status: str, providers_synced: List[str]) -> None:
    """更新 sync-state.json。"""
    state = {
        "last_success_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "last_run_status": status,
        "providers_synced": providers_synced,
    }
    atomic_write_json(SYNC_STATE_PATH, state)


def append_report(line: str) -> None:
    """追加写入 sync-report.log。"""
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    with open(SYNC_REPORT_PATH, "a", encoding="utf-8") as f:
        f.write(f"[{stamp}] {line}\n")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def load_providers(secrets: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """加载并筛选 enabled 且能解析出 API Key 的 providers。

    [Key 隔离] Key 解析顺序: apiKeyRef -> secrets.json -> 回退内联 apiKey。
    """
    if not PROVIDERS_JSON_PATH.exists():
        print(f"  ! 提供商配置不存在: {PROVIDERS_JSON_PATH}", file=sys.stderr)
        return []
    if secrets is None:
        secrets = load_secrets()
    with open(PROVIDERS_JSON_PATH, "r", encoding="utf-8") as f:
        all_providers = json.load(f)
    active: List[Dict[str, Any]] = []
    for p in all_providers:
        if not p.get("enabled", False):
            print(f"  - 跳过 {p.get('providerId')}: 未启用")
            continue
        if not resolve_api_key(p, secrets):
            ref = p.get("apiKeyRef") or ""
            hint = f"apiKeyRef={ref!r} 在 secrets.json 中无对应密钥" if ref else "apiKey 为空"
            print(f"  - 跳过 {p.get('providerId')}: {hint}")
            continue
        if p.get("protocol") not in ADAPTERS:
            print(f"  - 跳过 {p.get('providerId')}: 未知协议 {p.get('protocol')}")
            continue
        active.append(p)
    return active


def load_existing_models() -> List[Dict[str, Any]]:
    """读取现有 models.json。不存在当作空列表。"""
    if not MODELS_JSON_PATH.exists():
        return []
    try:
        with open(MODELS_JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"  ! 读取 models.json 失败，当作空列表处理: {e}", file=sys.stderr)
        return []


def sync(dry_run: bool = False, verbose: bool = False, save_list: bool = False, apply: bool = False) -> int:
    """主同步流程。返回退出码。

    默认行为（apply=False）：
        - 拉取各 provider 的模型列表
        - 合并去重后写入 latest-models.json（供 Web UI 展示和选择性导入）
        - 不写入 models.json（避免把全量模型当作"已导入"）
        - sync-state 的 last_run_status 记为 success/partial/failed

    apply=True 时（保留旧行为）：合并结果直接写入 models.json。
    """
    print(f"== model-sync 开始 == {'[DRY-RUN]' if dry_run else ''}")
    print(f"  models.json: {MODELS_JSON_PATH}")

    # [Key 隔离] 一次性加载密钥表并注入适配器
    secrets = load_secrets()
    if secrets:
        print(f"  密钥表: {SECRETS_JSON_PATH.name} ({len(secrets)} 条)")
    else:
        print(f"  ! 密钥表为空或不存在: {SECRETS_JSON_PATH}（将回退使用配置内联 apiKey）")
    ProtocolAdapter.set_secrets(secrets)

    providers = load_providers(secrets)
    if not providers:
        print("  ! 没有可用的 provider 配置，退出")
        return 1

    existing = load_existing_models()
    if verbose:
        print(f"  现有模型数: {len(existing)}")

    merged: List[Dict[str, Any]] = list(existing)
    success_providers: List[str] = []
    failed_providers: List[str] = []
    total_stats = {"added": 0, "updated": 0, "removed": 0, "kept": 0}

    for provider in providers:
        pid = provider["providerId"]
        protocol = provider["protocol"]
        print(f"\n>> [{pid}] 拉取 models ({protocol}) ...")
        try:
            adapter = ADAPTERS[protocol]
            fetched_ids = adapter.list_models(provider)
            if verbose:
                for mid in fetched_ids[:10]:
                    print(f"     - {mid}")
                if len(fetched_ids) > 10:
                    print(f"     ... 共 {len(fetched_ids)} 个")
            print(f"  [{pid}] 拉取到 {len(fetched_ids)} 个模型")
            merged, stats = merge_models(merged, provider, fetched_ids)
            for k in total_stats:
                total_stats[k] += stats[k]
            success_providers.append(pid)
            print(f"  [{pid}] +{stats['added']} 新增 | ~{stats['updated']} 更新 | -{stats['removed']} 移除")
        except urllib.error.HTTPError as e:
            print(f"  ! [{pid}] HTTP 错误: {e.code} {e.reason}", file=sys.stderr)
            failed_providers.append(pid)
        except urllib.error.URLError as e:
            print(f"  ! [{pid}] 网络错误: {e.reason}", file=sys.stderr)
            failed_providers.append(pid)
        except Exception as e:
            print(f"  ! [{pid}] 未知错误: {e}", file=sys.stderr)
            failed_providers.append(pid)

    # 决定状态
    if success_providers and not failed_providers:
        status = "success"
    elif success_providers and failed_providers:
        status = "partial"
    else:
        status = "failed"

    print("\n== 汇总 ==")
    print(f"  +新增 {total_stats['added']} | ~更新 {total_stats['updated']} | "
          f"-移除 {total_stats['removed']} | 保留 {total_stats['kept']}")
    print(f"  成功 providers: {success_providers}")
    if failed_providers:
        print(f"  失败 providers: {failed_providers}")
    # 写入前按 (providerId, id) 去重：共享 baseUrl 的不同 provider 各自保留模型
    before = len(merged)
    dedup = {}
    for i, m in enumerate(merged):
        key = (m.get("providerId", ""), m.get("id", str(i)))
        dedup.setdefault(key, m)
    merged = list(dedup.values())
    if len(merged) < before:
        print(f"  去重: 移除 {before - len(merged)} 个重复条目")
    print(f"  最终模型数: {len(merged)}")
    print(f"  运行状态: {status}")

    if dry_run:
        print("  [DRY-RUN] 不写入文件")
        return 0

    # 若请求保存全量列表，先写 latest-models.json
    if save_list:
        atomic_write_json(LATEST_MODELS_PATH, merged)
        print(f"  -> {LATEST_MODELS_PATH} 已保存 ({len(merged)} 个模型)")

    # 默认（apply=False）只更新 latest-models.json 与 sync-state，
    # 不写 models.json —— 避免把全量拉取的模型误当作"已导入"。
    # 用户在 Web UI 勾选后通过 --import-selected 流程才会写入 models.json。
    if not apply:
        print("  -> models.json 未改动（未指定 --apply）")
        print(f"  最终模型数（latest-models）: {len(merged)}")
        print(f"  运行状态: {status}")

        write_sync_state(status, success_providers)
        print(f"  -> {SYNC_STATE_PATH} 已更新")

        append_report(
            f"+{total_stats['added']} 新增 | -{total_stats['removed']} 移除 | "
            f"~{total_stats['updated']} 更新 | 共 {len(merged)} 模型 | "
            f"成功 {success_providers} | 失败 {failed_providers} | 状态 {status} | "
            f"{'已应用' if apply else '仅同步未应用'}"
        )
        return 0 if status != "failed" else 2

    # ---- apply=True：保留原行为，将合并结果直接写入 models.json ----

    # 写 models.json
    ok = atomic_write_json(MODELS_JSON_PATH, merged)
    if not ok:
        print("  ! models.json 写入失败，但继续更新 sync-state")
    else:
        print(f"  -> {MODELS_JSON_PATH} 已更新")

    # 写 sync-state.json
    write_sync_state(status, success_providers)
    print(f"  -> {SYNC_STATE_PATH} 已更新")

    # 追加日志
    append_report(
        f"+{total_stats['added']} 新增 | -{total_stats['removed']} 移除 | "
        f"~{total_stats['updated']} 更新 | 共 {len(merged)} 模型 | "
        f"成功 {success_providers} | 失败 {failed_providers} | 状态 {status}"
    )

    return 0 if status != "failed" else 2


def import_selected() -> int:
    """从 selected-models.json 读取选中的模型 ID，从 latest-models.json 中筛选后写入 models.json。

    保留不属于任何已配置 provider 的手动模型。

    对已经导入过的模型：刷新其能力标记（思考/视觉/工具调用），使提供商
    规则调整后重新导入即可生效，无需先删除；用户自己设置的内容
    （显示名称、已选思考强度、密钥、地址）保持不变。
    """
    if not SELECTED_MODELS_PATH.exists():
        print("  ! selected-models.json 不存在，请先同步并选择模型", file=sys.stderr)
        return 1
    if not LATEST_MODELS_PATH.exists():
        print("  ! latest-models.json 不存在，请先运行一次同步", file=sys.stderr)
        return 1

    with open(SELECTED_MODELS_PATH, "r", encoding="utf-8") as f:
        selected_ids: List[str] = json.load(f)
    if not isinstance(selected_ids, list) or not selected_ids:
        print("  ! selected-models.json 为空或格式错误，没有选中任何模型", file=sys.stderr)
        return 1

    with open(LATEST_MODELS_PATH, "r", encoding="utf-8") as f:
        all_models: List[Dict[str, Any]] = json.load(f)

    # 建立 (providerId, id) -> entry 映射，支持共享 baseUrl/id 的不同提供商
    model_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for m in all_models:
        if m.get("id"):
            model_map[(m.get("providerId", ""), m.get("id", ""))] = m
    print(f"  latest-models.json 中共 {len(model_map)} 个模型")

    # 选中的条目可能是字符串 id，也可能是 {id, providerId} 对象
    selected_entries: List[Dict[str, Any]] = []
    skipped = 0
    for item in selected_ids:
        if isinstance(item, dict):
            mid = item.get("id", "")
            mpid = item.get("providerId", "")
        else:
            mid = item
            mpid = ""
        entry = model_map.get((mpid, mid))
        if entry is not None:
            selected_entries.append(entry)
        else:
            # 回退：若未指定 providerId，按 id 查找（兼容旧数据）
            fallback = None
            for (kpid, kid), m in model_map.items():
                if kid == mid:
                    fallback = m
                    break
            if fallback is not None:
                selected_entries.append(fallback)
            else:
                skipped += 1

    print(f"  选中 {len(selected_entries)} 个模型导入")
    if skipped:
        print(f"  跳过 {skipped} 个不在 latest-models.json 中的 ID")

    # 读取现有模型，与新增选中项合并（保留所有已导入的模型，避免被替换删除）
    existing = load_existing_models()
    # [Key 隔离] 注入密钥表，与 sync() 保持一致
    secrets = load_secrets()
    ProtocolAdapter.set_secrets(secrets)
    providers = load_providers(secrets)
    active_base_urls = {p.get("baseUrl", "").rstrip("/").lower() for p in providers if p.get("enabled")}
    manual_count = sum(1 for e in existing if not any(
        _url_matches(e.get("url"), b) for b in active_base_urls
    ))
    print(f"  保留 {manual_count} 个手动添加的模型")

    # 合并现有 + 新增，按 (providerId, id) 去重
    # - 新模型：直接加入
    # - 已导入的模型：刷新"能力标记"（思考/视觉/工具调用），使提供商规则调整后
    #   （例如补上思考匹配规则）无需删除重导即可生效；
    #   同时保留用户自己的设置：显示名称、已选思考强度、密钥、地址等一律不动。
    merged_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for e in existing:
        if e.get("id"):
            merged_map[(e.get("providerId", ""), e["id"])] = e
    added = 0
    refreshed = 0
    for e in selected_entries:
        if not e.get("id"):
            continue
        key = (e.get("providerId", ""), e["id"])
        old = merged_map.get(key)
        if old is None:
            merged_map[key] = e
            added += 1
            continue

        # 已有条目：仅刷新能力标记
        old_reasoning = old.get("reasoning") if isinstance(old.get("reasoning"), dict) else {}
        changed = False
        for field in ("supportsToolCall", "supportsImages", "supportsReasoning"):
            new_val = bool(e.get(field, False))
            if old.get(field) != new_val:
                changed = True
            old[field] = new_val
        # reasoning 单独处理：用新规则生成的可选强度，但保留用户已选的思考强度
        new_reasoning = e.get("reasoning") if isinstance(e.get("reasoning"), dict) else {}
        if old.get("supportsReasoning"):
            merged_reasoning = dict(new_reasoning)
            if old_reasoning.get("defaultEffort"):
                merged_reasoning["defaultEffort"] = old_reasoning["defaultEffort"]
            if old.get("reasoning") != merged_reasoning:
                changed = True
            old["reasoning"] = merged_reasoning
        elif old.get("reasoning") is not None:
            # 规则改为不支持思考：移除遗留的 reasoning 字段，保持数据一致
            old.pop("reasoning", None)
            changed = True

        if changed:
            refreshed += 1

    final = list(merged_map.values())
    print(f"  新增 {added} 个模型 | 刷新能力标记 {refreshed} 个 | "
          f"保留已有 {len(existing)} 个 | 最终写入 {len(final)} 个模型")

    ok = atomic_write_json(MODELS_JSON_PATH, final)
    if not ok:
        print("  ! models.json 写入失败", file=sys.stderr)
        return 2

    write_sync_state("imported", [])
    append_report(f"选择性导入 {len(selected_entries)} 个模型 | 新增 {added} 个 | "
                  f"刷新能力 {refreshed} 个 | 保留已有 {len(existing)} | 共 {len(final)}")
    print(f"  -> {MODELS_JSON_PATH} 已更新")
    print(f"  -> {SYNC_STATE_PATH} 已更新")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="WorkBuddy 模型自动同步引擎",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python model-sync.py                     # 正常同步（仅写 latest-models.json）\n"
               "  python model-sync.py --dry-run           # 演练\n"
               "  python model-sync.py --verbose           # 详细日志\n"
               "  python model-sync.py --save-list         # 同步后保存全量列表到 latest-models.json\n"
               "  python model-sync.py --apply            # 同步后直接写入 models.json（一键全部导入）\n"
               "  python model-sync.py --import-selected   # 选择性导入\n",
    )
    parser.add_argument("--dry-run", action="store_true", help="演练模式，不写入文件")
    parser.add_argument("--verbose", action="store_true", help="详细日志输出")
    parser.add_argument("--save-list", action="store_true", help="同步后保存全量模型列表到 latest-models.json")
    parser.add_argument("--apply", action="store_true", help="将合并结果直接写入 models.json（默认不写，仅写 latest-models.json）")
    parser.add_argument("--import-selected", action="store_true", help="从 selected-models.json 选择性导入")
    args = parser.parse_args()

    if args.import_selected:
        return import_selected()

    return sync(dry_run=args.dry_run, verbose=args.verbose, save_list=args.save_list, apply=args.apply)


if __name__ == "__main__":
    sys.exit(main())
