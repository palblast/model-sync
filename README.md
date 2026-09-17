# model-sync（模型同步工具）

为 **WorkBuddy** 提供「一键导入模型」能力的小工具：自动从各个 AI 服务商拉取可用模型列表，合并写入 WorkBuddy 的配置文件。

> 灵感来自 CherryStudio 的模型管理体验。

---

## ⚠️ 重要：使用前提

**本工具是 WorkBuddy 的配套插件，不能独立使用。**

它会读取并写入 WorkBuddy 的配置文件：

| 路径 | 说明 |
|------|------|
| `~/.workbuddy/models.json` | 最终输出：WorkBuddy 的模型配置 |

其中 `~` 表示你的用户主目录（Windows 下通常是 `C:\Users\你的用户名`）。

**如果你没有安装 WorkBuddy**，本工具虽然能运行，但同步结果会写入一个不存在的软件的配置目录，**没有实际用途**。

---

## 功能特性

- **多服务商支持**：一条命令同步多家 AI 服务商的模型列表
- **协议适配**：支持 4 种协议
  - `openai`（OpenAI 兼容接口，最常用）
  - `anthropic`（Anthropic 官方接口）
  - `gemini`（Google Gemini 接口）
  - `ollama`（本地部署的 Ollama）
- **智能合并**：只更新来自已启用服务商的模型，**不删除**你手动添加的自定义模型
- **能力自动推断**：根据模型名称自动判断是否支持「工具调用」「图片识别」「推理模式」
- **网页界面**：提供可视化操作界面，可勾选导入、测试模型连通性
- **原子写入**：先写临时文件再替换，避免中途失败导致配置文件损坏
- **密钥隔离**：API 密钥单独存放，不写进服务商配置文件

---

## 环境要求

- **Python 3.8 或更高版本**
- 无需安装任何第三方库（**仅使用 Python 标准库**）

---

## 快速开始

### 第一步：启动服务

```
双击 start.pyw
```

或在命令行中运行：

```bash
python server.py --port 7788
```

启动后浏览器会自动打开 `http://127.0.0.1:7788/`。

### 第二步：配置服务商

在网页界面中：

1. 点击「**提供商管理**」
2. 选择你需要的服务商（默认全部为**关闭**状态）
3. 填入你的 **API Key**（接口密钥）
4. 勾选「**启用**」
5. 保存

### 第三步：同步模型

点击「**同步并拉取模型**」，等待拉取完成。

### 第四步：导入模型

勾选你需要的模型 → 点击「**导入选中**」。

---

## 命令行用法

如果你不想用网页界面，也可以直接用命令行：

```bash
# 同步（仅拉取列表，不写入 models.json）
python model-sync.py

# 演练模式：只显示会做什么，不实际写入任何文件
python model-sync.py --dry-run

# 详细日志
python model-sync.py --verbose

# 同步并保存全量列表到 latest-models.json（供网页界面使用）
python model-sync.py --save-list

# 同步后直接全部导入 models.json
python model-sync.py --apply

# 从 selected-models.json 选择性导入
python model-sync.py --import-selected
```

---

## 配置自己的服务商

服务商配置保存在 `model-providers.json`，本仓库内置了 7 家常用服务商的模板（**默认全部关闭**）：

| 服务商 | 接口地址 |
|--------|----------|
| NVIDIA NIM | `https://integrate.api.nvidia.com/v1` |
| 硅基流动 (SiliconFlow) | `https://api.siliconflow.cn/v1` |
| DeepSeek (深度求索) | `https://api.deepseek.com` |
| Moonshot (月之暗面 Kimi) | `https://api.moonshot.cn` |
| OpenAI | `https://api.openai.com` |
| 智谱 GLM (Zhipu) | `https://open.bigmodel.cn/api/paas/v4` |
| Ollama (本地部署) | `http://127.0.0.1:11434` |

### 添加自定义服务商

在 `model-providers.json` 中按以下格式追加一项：

```json
{
  "providerId": "my-provider",
  "name": "我的服务商",
  "protocol": "openai",
  "baseUrl": "https://api.example.com/v1",
  "enabled": true,
  "modelRules": {
    "reasoningPatterns": ["o1", "r1"],
    "visionPatterns": ["vision", "4o"],
    "toolCallPatterns": ["*"],
    "excludeToolCallPatterns": ["embed"]
  }
}
```

字段说明：

| 字段 | 说明 |
|------|------|
| `providerId` | 唯一标识，建议用英文 |
| `name` | 显示名称，可用中文 |
| `protocol` | 协议类型：`openai` / `anthropic` / `gemini` / `ollama` |
| `baseUrl` | 接口地址 |
| `enabled` | 是否启用 |
| `modelRules` | 能力推断规则，用于判断模型是否支持工具调用、图片、推理 |

### 关于密钥

API 密钥**不写在 `model-providers.json` 里**，而是由网页界面自动保存到单独的 `secrets.json`。

这是**有意的安全设计**：`model-providers.json` 只保存一个引用名（`apiKeyRef`），真正的密钥单独存放，降低误提交密钥的风险。

---

## 文件说明

| 文件 | 作用 |
|------|------|
| `model-sync.py` | 同步引擎核心 |
| `server.py` | 网页后端服务 |
| `index.html` | 网页界面 |
| `start.pyw` | 双击启动器 |
| `model-providers.json` | 服务商配置（可提交） |
| `secrets.json` | **你的 API 密钥（自动生成，请勿提交）** |
| `latest-models.json` | 同步拉取的全量模型列表（自动生成） |
| `selected-models.json` | 你勾选的模型（自动生成） |
| `sync-state.json` | 同步状态记录（自动生成） |
| `sync-report.log` | 同步日志（自动生成） |

---

## 🔒 安全提示

**请勿将以下文件提交到 Git 仓库**（`.gitignore` 已默认忽略）：

- `secrets.json` — 含明文 API 密钥
- `latest-models.json` — 运行时写入真实密钥
- `selected-models.json`、`sync-state.json`、`sync-report.log` — 本地使用记录

> ⚠️ 特别注意 `latest-models.json`：**每次同步都会把真实密钥写进去**，是持续性的泄露风险点。删除文件只是一次性动作，**务必保留 `.gitignore` 中的相关规则**。

---

## 常见问题

### 同步时提示「没有可用的 provider 配置，退出」

说明**没有任何服务商被启用，或启用的服务商没有密钥**。请到「提供商管理」中启用至少一个服务商并填入 API Key。

### 提示「密钥表为空或不存在」

`secrets.json` 尚未生成。这是正常的 —— 首次使用时到网页界面填入密钥并保存，文件会自动创建。

### 某些模型同步失败

部分服务商的 API Key 权限有限，只能访问其部分模型。同步日志中会列出失败的服务商，但不影响其他服务商的同步。

### 我手动添加的模型会被删除吗？

**不会。** 同步只会更新来自「已启用服务商」的模型条目，你手动添加的自定义模型不受影响。

### 端口被占用

换一个端口启动：

```bash
python server.py --port 8899
```

---

## 技术实现

| 组件 | 选型 |
|------|------|
| 后端 | Python 标准库 `http.server`（多线程） |
| 前端 | 纯 HTML + CSS + JavaScript（无框架，单文件） |
| 协议 | 以 OpenAI 兼容接口为主 |
| 写入策略 | 原子写入（临时文件 → 替换） |

### 接口清单

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/` | 网页界面首页 |
| `GET` | `/api/state` | 同步状态 |
| `GET` | `/api/models` | 已导入的模型列表 |
| `GET` | `/api/latest-models` | 全量模型列表 |
| `GET` | `/api/providers` | 服务商配置 |
| `GET` | `/api/report` | 同步日志 |
| `POST` | `/api/sync` | 触发同步（可加 `?dry-run=1`） |
| `POST` | `/api/import` | 导入选中的模型 |
| `POST` | `/api/test-model` | 测试模型连通性 |
| `POST` | `/api/providers` | 保存服务商配置 |

---

## 许可证

本项目以 [MIT License](LICENSE) 开源。使用前请自行确认与你所使用的各 AI 服务商的条款相符。
