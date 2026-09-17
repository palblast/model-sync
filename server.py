#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy model-sync 后端服务。

基于 Python 标准库 http.server 实现，提供：
    - GET  /                       返回 index.html
    - GET  /api/state               返回 sync-state.json
    - GET  /api/models              返回 models.json（已导入的模型）
    - GET  /api/latest-models       返回 latest-models.json（全量列表）
    - GET  /api/providers           返回 model-providers.json
    - POST /api/providers           保存 model-providers.json
    - POST /api/sync                触发同步（可选 ?dry-run=1）
    - POST /api/import              导入选中的模型 {selected: [id1, id2]}
    - GET  /api/report              返回最近的 sync-report.log

启动：
    python server.py                # 默认 127.0.0.1:7788
    python server.py --port 8080
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE_DIR = Path(__file__).resolve().parent
HOME = Path(os.path.expanduser("~"))
MODELS_JSON_PATH = HOME / ".workbuddy" / "models.json"
PROVIDERS_JSON_PATH = BASE_DIR / "model-providers.json"
SYNC_STATE_PATH = BASE_DIR / "sync-state.json"
SYNC_REPORT_PATH = BASE_DIR / "sync-report.log"
LATEST_MODELS_PATH = BASE_DIR / "latest-models.json"
SELECTED_MODELS_PATH = BASE_DIR / "selected-models.json"
INDEX_HTML = BASE_DIR / "index.html"
# [Key 隔离] 密钥单独存放，不进 git
SECRETS_JSON_PATH = Path(os.environ.get("MS_SECRETS", str(BASE_DIR / "secrets.json")))

# [跨平台] 使用当前正在运行本服务的 Python 解释器来执行子脚本，
# 避免硬编码本机安装路径（不同用户/系统的 Python 位置不同）。
PYTHON_EXE = sys.executable
MODEL_SYNC_PY = str(BASE_DIR / "model-sync.py")

# CORS / 安全：仅本机访问
ALLOWED_ORIGIN = "*"


class Handler(BaseHTTPRequestHandler):
    """处理 API 路由。"""

    server_version = "model-sync/1.0"

    # --- [Key 隔离] 密钥读写 -------------------------------------------
    @staticmethod
    def _load_secrets() -> dict:
        """读取 secrets.json（{providerId: apiKey}）。缺失/损坏返回空 dict。"""
        if not SECRETS_JSON_PATH.exists():
            return {}
        try:
            raw = json.loads(SECRETS_JSON_PATH.read_text(encoding="utf-8"))
            return {str(k): str(v) for k, v in raw.items() if v} if isinstance(raw, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _save_secrets(secrets: dict) -> None:
        """原子写入 secrets.json，并限制文件权限（仅当前用户可读写）。"""
        tmp = SECRETS_JSON_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(secrets, ensure_ascii=False, indent=2), encoding="utf-8")
        json.loads(tmp.read_text(encoding="utf-8"))  # 校验
        os.replace(tmp, SECRETS_JSON_PATH)
        try:
            os.chmod(SECRETS_JSON_PATH, 0o600)
        except Exception:
            pass  # Windows 上 chmod 语义有限，失败不影响功能

    @classmethod
    def _providers_with_keys(cls) -> list:
        """读取 model-providers.json，并把真实 Key 从 secrets.json 回填到 apiKey。

        这样前端无需知道 secrets.json 的存在：它照常读写 apiKey 字段。
        """
        providers = cls._read_json(PROVIDERS_JSON_PATH, [])
        secrets = cls._load_secrets()
        if not isinstance(providers, list):
            return []
        for p in providers:
            if not isinstance(p, dict):
                continue
            ref = (p.get("apiKeyRef") or "").strip() or p.get("providerId", "")
            p["apiKey"] = secrets.get(ref, "") if ref else ""
        return providers

    @classmethod
    def _split_providers_secrets(cls, providers: list, mode: str) -> None:
        """把前端传来的 providers 列表拆成 model-providers.json + secrets.json。

        mode="replace"（全量保存）：secrets 用本次提交内容【整体覆盖】，
            使「删除提供商」或「清空 Key」能正确清理密钥。
        mode="merge"（单个新增）：只【增量】写入本次这一个 provider 的 Key，
            因为入参只有 1 个提供商，覆盖会误删其余密钥。
        """
        secrets = cls._load_secrets() if mode == "merge" else {}
        cleaned = []
        for p in providers:
            if not isinstance(p, dict):
                continue
            q = dict(p)
            pid = (q.get("providerId") or "").strip()
            key = (q.get("apiKey") or "").strip()
            q.pop("apiKey", None)
            if key and pid:
                secrets[pid] = key
                q["apiKeyRef"] = pid
            else:
                # 无 Key：清掉引用，避免指向不存在的密钥
                q.pop("apiKeyRef", None)
            cleaned.append(q)

        # 先写 secrets，再写 providers；若中途失败，providers 仍保留旧 apiKey 可回退
        cls._save_secrets(secrets)

        tmp = PROVIDERS_JSON_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            json.loads(tmp.read_text(encoding="utf-8"))
        except Exception as e:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"validate failed: {e}")
        os.replace(tmp, PROVIDERS_JSON_PATH)

    def end_headers(self):
        # 禁用缓存，确保编辑 index.html / server.py 后浏览器能立即拿到最新版本
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    # --- 通用工具 --------------------------------------------------------
    def _send_json(self, data, status=HTTPStatus.OK):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGIN)
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, path: Path, status=HTTPStatus.OK):
        if not path.exists():
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGIN)
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length > 0 else b""

    # --- GET -------------------------------------------------------------
    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        qs = parse_qs(url.query)

        if path == "/" or path == "/index.html":
            self._send_html(INDEX_HTML)
            return
        if path == "/api/state":
            self._send_json(self._read_json(SYNC_STATE_PATH, {}))
            return
        if path == "/api/models":
            self._send_json(self._read_json(MODELS_JSON_PATH, []))
            return
        if path == "/api/latest-models":
            self._send_json(self._read_json(LATEST_MODELS_PATH, []))
            return
        if path == "/api/providers":
            # [Key 隔离] 回填真实 Key，前端照常显示/编辑 apiKey
            self._send_json(self._providers_with_keys())
            return
        if path == "/api/report":
            self._send_text_file(SYNC_REPORT_PATH)
            return

        self._send_json({"error": "unknown path", "path": path}, HTTPStatus.NOT_FOUND)

    # --- POST ------------------------------------------------------------
    def do_POST(self):
        url = urlparse(self.path)
        path = url.path

        if path == "/api/sync":
            self._handle_sync(parse_qs(url.query))
            return
        if path == "/api/import":
            self._handle_import()
            return
        if path == "/api/test-model":
            self._handle_test_model()
            return
        if path == "/api/providers":
            self._save_providers()
            return
        if path == "/api/providers/add":
            self._add_provider()
            return
        if path == "/api/models/remove":
            self._remove_model()
            return
        if path == "/api/models/remove-batch":
            self._remove_models_batch()
            return
        if path == "/api/models/update":
            self._update_model()
            return
        if path == "/api/models/switch-key":
            self._switch_model_key()
            return
        if path == "/api/models/switch-effort":
            self._switch_model_effort()
            return

        self._send_json({"error": "unknown path", "path": path}, HTTPStatus.NOT_FOUND)

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGIN)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # --- 业务逻辑 -------------------------------------------------------
    def _handle_sync(self, qs: dict):
        dry_run = qs.get("dry-run", ["0"])[0] in ("1", "true", "True")
        args = [PYTHON_EXE, MODEL_SYNC_PY]
        if dry_run:
            args.append("--dry-run")
        else:
            args.append("--save-list")
        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=120,
                encoding="utf-8",
                errors="replace",
            )
            self._send_json({
                "returncode": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "dry-run": dry_run,
            })
        except Exception as e:
            self._send_json({"error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_import(self):
        """处理用户选中模型后的导入请求。"""
        body = self._read_body().decode("utf-8", errors="replace")
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            self._send_json({"error": f"bad JSON: {e}"}, HTTPStatus.BAD_REQUEST)
            return

        selected = data.get("selected", [])
        if not isinstance(selected, list) or not selected:
            self._send_json({"error": "selected 必须是数组且不能为空"}, HTTPStatus.BAD_REQUEST)
            return

        # 保存选中的模型 ID 列表到 selected-models.json
        try:
            tmp = SELECTED_MODELS_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(selected, ensure_ascii=False, indent=2), encoding="utf-8")
            json.loads(tmp.read_text(encoding="utf-8"))  # 校验
            os.replace(tmp, SELECTED_MODELS_PATH)
        except Exception as e:
            self._send_json({"error": f"保存 selected-models.json 失败: {e}"},
                          HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        # 调用 model-sync.py --import-selected
        args = [PYTHON_EXE, MODEL_SYNC_PY, "--import-selected"]
        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=60,
                encoding="utf-8",
                errors="replace",
            )
            self._send_json({
                "returncode": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "selected_count": len(selected),
            })
        except Exception as e:
            self._send_json({"error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_test_model(self):
        """测试某个模型是否可用，发送一条简单消息并检查响应。"""
        body = self._read_body().decode("utf-8", errors="replace")
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            self._send_json({"error": f"bad JSON: {e}"}, HTTPStatus.BAD_REQUEST)
            return

        model_id = data.get("id", "")
        if not model_id:
            self._send_json({"error": "缺少 id 参数"}, HTTPStatus.BAD_REQUEST)
            return

        # 在 models.json 中查找模型
        models = self._read_json(MODELS_JSON_PATH, [])
        model = None
        for m in models:
            if m.get("id") == model_id:
                model = m
                break

        if not model:
            self._send_json({"error": f"未找到模型: {model_id}"}, HTTPStatus.NOT_FOUND)
            return

        base_url = model.get("url", "").rstrip("/")
        api_key = model.get("apiKey", "")
        if not base_url:
            self._send_json({"error": "模型没有配置 API URL"}, HTTPStatus.BAD_REQUEST)
            return
        if not api_key:
            self._send_json({"error": "模型没有配置 API Key"}, HTTPStatus.BAD_REQUEST)
            return

        # 构造 OpenAI 兼容的 chat completions 请求
        request_body = json.dumps({
            "model": model_id,
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 100,
            "temperature": 0.0,
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=request_body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )

        start = time.time()
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                elapsed = time.time() - start
                result = json.loads(resp.read().decode("utf-8"))
                choices = result.get("choices", [])
                content = ""
                reasoning = ""
                if choices:
                    msg = choices[0].get("message", {})
                    raw = msg.get("content")
                    reasoning_raw = msg.get("reasoning_content")
                    content = raw if raw is not None else ""
                    reasoning = reasoning_raw if reasoning_raw is not None else ""
                finish_reason = choices[0].get("finish_reason", "") if choices else ""
                # 优先展示 content；如果为空且存在 reasoning_content，说明模型正在思考
                display = reasoning if not content and reasoning else content
                self._send_json({
                    "ok": True,
                    "model": model_id,
                    "elapsed": round(elapsed, 2),
                    "status_code": resp.status,
                    "content": content[:200].strip(),
                    "reasoning": reasoning[:200].strip(),
                    "display": display[:200].strip(),
                    "finish_reason": finish_reason,
                })
        except urllib.error.HTTPError as e:
            elapsed = time.time() - start
            body_text = e.read().decode("utf-8", errors="replace")[:500]
            self._send_json({
                "ok": False,
                "model": model_id,
                "elapsed": round(elapsed, 2),
                "error": f"HTTP {e.code}: {body_text}",
                "status_code": e.code,
            })
        except urllib.error.URLError as e:
            elapsed = time.time() - start
            reason = str(e.reason) if hasattr(e, "reason") else str(e)
            self._send_json({
                "ok": False,
                "model": model_id,
                "elapsed": round(elapsed, 2),
                "error": f"请求失败: {reason}",
            })
        except Exception as e:
            elapsed = time.time() - start
            self._send_json({
                "ok": False,
                "model": model_id,
                "elapsed": round(elapsed, 2),
                "error": str(e),
            })

    def _save_providers(self):
        body = self._read_body().decode("utf-8", errors="replace")
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            self._send_json({"error": f"bad JSON: {e}"}, HTTPStatus.BAD_REQUEST)
            return
        if not isinstance(data, list):
            self._send_json({"error": "providers 必须是数组"}, HTTPStatus.BAD_REQUEST)
            return
        # [Key 隔离] 拆分为 providers(仅引用) + secrets(真 Key)。
        # mode="replace"：整体覆盖 secrets，使删除提供商/清空 Key 能同步清理。
        try:
            self._split_providers_secrets(data, mode="replace")
        except Exception as e:
            self._send_json({"error": f"保存失败: {e}"}, HTTPStatus.BAD_REQUEST)
            return
        self._send_json({"ok": True, "count": len(data)})

    def _add_provider(self):
        """添加新的AI提供商配置。"""
        body = self._read_body().decode("utf-8", errors="replace")
        try:
            new_provider = json.loads(body)
        except json.JSONDecodeError as e:
            self._send_json({"error": f"bad JSON: {e}"}, HTTPStatus.BAD_REQUEST)
            return

        # 验证必需字段
        required_fields = ["providerId", "name", "protocol", "baseUrl"]
        for field in required_fields:
            if not new_provider.get(field):
                self._send_json({"error": f"缺少必需字段: {field}"}, HTTPStatus.BAD_REQUEST)
                return

        # 验证协议类型
        valid_protocols = ["openai", "anthropic", "gemini", "ollama", "cloudflare"]
        if new_provider["protocol"] not in valid_protocols:
            self._send_json({"error": f"不支持的协议类型: {new_provider['protocol']}，支持的类型: {', '.join(valid_protocols)}"},
                          HTTPStatus.BAD_REQUEST)
            return

        # 读取现有提供商列表
        providers = self._read_json(PROVIDERS_JSON_PATH, [])

        # 检查providerId是否已存在
        for p in providers:
            if p.get("providerId") == new_provider["providerId"]:
                self._send_json({"error": f"提供商ID已存在: {new_provider['providerId']}"},
                              HTTPStatus.CONFLICT)
                return

        # 设置默认值
        if "enabled" not in new_provider:
            new_provider["enabled"] = False
        if "apiKey" not in new_provider:
            new_provider["apiKey"] = ""
        if "namePrefix" not in new_provider:
            new_provider["namePrefix"] = ""
        if "nameSuffix" not in new_provider:
            new_provider["nameSuffix"] = ""
        if "modelRules" not in new_provider:
            new_provider["modelRules"] = {
                "reasoningPatterns": [],
                "visionPatterns": [],
                "toolCallPatterns": ["*"],
                "excludeToolCallPatterns": ["embed"]
            }

        # 添加到列表（列表本身只保留不含 Key 的形式）
        providers.append(new_provider)

        # [Key 隔离] mode="merge"：只增量写入本次这一个 provider 的 Key。
        # 注意不能用 "replace"——此处入参仅 1 个提供商，覆盖会把其余密钥全删掉。
        try:
            self._split_providers_secrets(providers, mode="merge")
        except Exception as e:
            self._send_json({"error": f"保存失败: {e}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        # 响应中回显 Key，前端依赖 apiKey 字段继续渲染
        new_provider.pop("apiKeyRef", None)
        self._send_json({
            "ok": True,
            "message": f"提供商 {new_provider['name']} 添加成功",
            "provider": new_provider
        })

    def _remove_model(self):
        """从 models.json 中删除指定模型。"""
        body = self._read_body().decode("utf-8", errors="replace")
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            self._send_json({"error": f"bad JSON: {e}"}, HTTPStatus.BAD_REQUEST)
            return

        model_id = data.get("id", "")
        if not model_id:
            self._send_json({"error": "缺少 id 参数"}, HTTPStatus.BAD_REQUEST)
            return

        # 读取现有模型列表
        models = self._read_json(MODELS_JSON_PATH, [])
        original_count = len(models)

        # 过滤掉要删除的模型
        models = [m for m in models if m.get("id") != model_id]

        if len(models) == original_count:
            self._send_json({"error": f"未找到模型: {model_id}"}, HTTPStatus.NOT_FOUND)
            return

        # 原子写入
        tmp = MODELS_JSON_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(models, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            json.loads(tmp.read_text(encoding="utf-8"))
        except Exception as e:
            tmp.unlink(missing_ok=True)
            self._send_json({"error": f"validate failed: {e}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        os.replace(tmp, MODELS_JSON_PATH)

        self._send_json({
            "ok": True,
            "message": f"已删除模型: {model_id}",
            "remaining": len(models)
        })

    def _remove_models_batch(self):
        """从 models.json 中批量删除多个指定模型。"""
        body = self._read_body().decode("utf-8", errors="replace")
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            self._send_json({"error": f"bad JSON: {e}"}, HTTPStatus.BAD_REQUEST)
            return

        ids = data.get("ids", [])
        if not isinstance(ids, list) or not ids:
            self._send_json({"error": "ids 必须是数组且不能为空"}, HTTPStatus.BAD_REQUEST)
            return

        id_set = set(ids)

        # 读取现有模型列表
        models = self._read_json(MODELS_JSON_PATH, [])
        original_count = len(models)

        # 过滤掉要删除的模型
        kept = [m for m in models if m.get("id") not in id_set]
        removed_count = original_count - len(kept)

        if removed_count == 0:
            self._send_json({"error": "未找到要删除的模型"}, HTTPStatus.NOT_FOUND)
            return

        # 原子写入
        tmp = MODELS_JSON_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            json.loads(tmp.read_text(encoding="utf-8"))
        except Exception as e:
            tmp.unlink(missing_ok=True)
            self._send_json({"error": f"validate failed: {e}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        os.replace(tmp, MODELS_JSON_PATH)

        self._send_json({
            "ok": True,
            "message": f"已批量删除 {removed_count} 个模型",
            "removed": removed_count,
            "remaining": len(kept)
        })

    def _update_model(self):
        """更新 models.json 中单个模型的申请ID(id)和显示名称(name)。

        修改 id 会影响实际请求，需前端确认；修改 name 仅影响界面显示。
        """
        body = self._read_body().decode("utf-8", errors="replace")
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            self._send_json({"error": f"bad JSON: {e}"}, HTTPStatus.BAD_REQUEST)
            return

        old_id = data.get("old_id", "")
        new_id = data.get("id", "")
        name = data.get("name", "")

        if not old_id or not new_id:
            self._send_json({"error": "缺少 old_id / id 参数"}, HTTPStatus.BAD_REQUEST)
            return

        models = self._read_json(MODELS_JSON_PATH, [])
        target = None
        for m in models:
            if m.get("id") == old_id:
                target = m
                break
        if target is None:
            self._send_json({"error": f"未找到模型: {old_id}"}, HTTPStatus.NOT_FOUND)
            return

        # 若修改了 id，检查新 id 是否与其它模型冲突
        if new_id != old_id:
            for m in models:
                if m.get("id") == new_id:
                    self._send_json({"error": f"申请ID已存在: {new_id}"}, HTTPStatus.CONFLICT)
                    return

        target["id"] = new_id
        if name is not None:
            target["name"] = name

        # 原子写入
        tmp = MODELS_JSON_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(models, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            json.loads(tmp.read_text(encoding="utf-8"))
        except Exception as e:
            tmp.unlink(missing_ok=True)
            self._send_json({"error": f"validate failed: {e}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        os.replace(tmp, MODELS_JSON_PATH)

        self._send_json({
            "ok": True,
            "message": f"模型已更新: {new_id}",
            "model": target
        })

    def _switch_model_key(self):
        """切换某模型(ID)实际使用的提供商/API Key。

        从 latest-models.json 取指定 (providerId, id) 的条目，用其字段更新
        models.json 中该 id 的模型，并移除同 id 的其它重复条目，确保每个 id 只保留一条。
        """
        body = self._read_body().decode("utf-8", errors="replace")
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            self._send_json({"error": f"bad JSON: {e}"}, HTTPStatus.BAD_REQUEST)
            return

        model_id = data.get("id", "")
        provider_id = data.get("providerId", "")
        if not model_id or not provider_id:
            self._send_json({"error": "缺少 id / providerId 参数"}, HTTPStatus.BAD_REQUEST)
            return

        latest = self._read_json(LATEST_MODELS_PATH, [])
        target = None
        for m in latest:
            if m.get("id") == model_id and m.get("providerId") == provider_id:
                target = m
                break
        if target is None:
            self._send_json({"error": f"未找到提供商 {provider_id} 下的模型 {model_id}"},
                            HTTPStatus.NOT_FOUND)
            return

        models = self._read_json(MODELS_JSON_PATH, [])
        # 移除同 id 的所有条目
        kept = [m for m in models if m.get("id") != model_id]

        # 用选中提供商的条目替换（含 apiKey/url/vendor/name 等）
        # 保留 namePrefix/nameSuffix 生成的显示名
        new_entry = dict(target)
        kept.append(new_entry)

        tmp = MODELS_JSON_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            json.loads(tmp.read_text(encoding="utf-8"))
        except Exception as e:
            tmp.unlink(missing_ok=True)
            self._send_json({"error": f"validate failed: {e}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        os.replace(tmp, MODELS_JSON_PATH)

        self._send_json({
            "ok": True,
            "message": f"已切换 {model_id} 到提供商 {provider_id}",
            "model": new_entry
        })

    def _switch_model_effort(self):
        """切换模型的思考强度（reasoning.defaultEffort）。

        接收 {id, effort}，将 models.json 中该模型的 reasoning.defaultEffort 设为指定值。
        """
        body = self._read_body().decode("utf-8", errors="replace")
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            self._send_json({"error": f"bad JSON: {e}"}, HTTPStatus.BAD_REQUEST)
            return

        model_id = data.get("id", "")
        effort = data.get("effort", "")
        if not model_id or not effort:
            self._send_json({"error": "缺少 id / effort 参数"}, HTTPStatus.BAD_REQUEST)
            return

        models = self._read_json(MODELS_JSON_PATH, [])
        target = None
        for m in models:
            if m.get("id") == model_id:
                target = m
                break
        if target is None:
            self._send_json({"error": f"未找到模型: {model_id}"}, HTTPStatus.NOT_FOUND)
            return

        # 更新 reasoning.defaultEffort
        if not isinstance(target.get("reasoning"), dict):
            target["reasoning"] = {}
        r = target["reasoning"]
        supported = r.get("supportedEfforts") or ["low", "medium", "high"]
        if isinstance(supported, list) and effort not in supported:
            self._send_json({"error": f"模型不支持思考强度: {effort}，支持: {', '.join(supported)}"},
                            HTTPStatus.BAD_REQUEST)
            return
        r["defaultEffort"] = effort

        tmp = MODELS_JSON_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(models, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            json.loads(tmp.read_text(encoding="utf-8"))
        except Exception as e:
            tmp.unlink(missing_ok=True)
            self._send_json({"error": f"validate failed: {e}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        os.replace(tmp, MODELS_JSON_PATH)

        self._send_json({
            "ok": True,
            "message": f"模型 {model_id} 思考强度已设为 {effort}",
            "model": target
        })

    # --- 工具 ------------------------------------------------------------
    @staticmethod
    def _read_json(path: Path, default):
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    def _send_text_file(self, path: Path):
        if not path.exists():
            self._send_json({"content": ""})
            return
        # 最多返回最近 200 行
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
        self._send_json({"content": "\n".join(lines)})

    def log_message(self, format, *args):  # 安静一点
        # pythonw.exe 下 sys.stderr 为 None，直接 write 会在请求处理线程中抛异常
        # 导致响应未写出就关闭连接（空响应）。此处做防御性处理。
        if sys.stderr is None:
            return
        try:
            sys.stderr.write("[%s] %s\n" % (self.address_string(), format % args))
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="WorkBuddy model-sync 后端服务")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=7788, help="监听端口")
    args = parser.parse_args()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"model-sync 服务启动")
    print(f"  本机地址: http://127.0.0.1:{args.port}/")
    print(f"  提供商配置: {PROVIDERS_JSON_PATH}")
    print(f"  目标输出: {MODELS_JSON_PATH}")
    print(f"  按 Ctrl+C 退出")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n关闭中 ...")
        srv.shutdown()


if __name__ == "__main__":
    main()
