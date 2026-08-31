# Zotero MCP

面向 Windows 与 WSL 的本地 Zotero MCP。读取走 Zotero Local API；需要修改 Zotero 时，先生成计划，再通过 Zotero Web API 执行并回读核验。

## 交给 Codex 配置

clone 仓库后，在仓库目录对 Codex 发送：

> 读取 AGENTS.md，按首次配置流程检查我的 Codex 与 Zotero 分别运行在 Windows 还是 WSL。账号注册、浏览器设置和密钥输入由我完成；你负责其余检测、配置与验证。

Codex 会先做只读检查。缺少 Zotero Web API、MinerU 或 SciVerse 账号时，会给出官方入口并暂停；不会索要或显示密钥。完整配置还会检查可选的 `paper-lookup` skill，并在安装前等待批准。

## 安装

需要 Python 3.11 或更新版本。

```bash
git clone <repository-url> zotero-mcp
cd zotero-mcp
python -m venv .venv
```

Windows PowerShell 安装：

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install .
```

WSL/Linux 安装：

```bash
source .venv/bin/activate
python -m pip install .
```

运行首次检查：

```bash
zotero-mcp setup plan --profile core
```

`core` 只要求 Zotero 读取链路。需要 Web API 写入、MinerU、QMD 与 SciVerse 时使用：

```bash
zotero-mcp setup plan --profile full
```

完整步骤见 [docs/SETUP.md](docs/SETUP.md)，账号边界见 [docs/ACCOUNTS.md](docs/ACCOUNTS.md)。

## 功能

- 检索、读取、匹配 Zotero 条目
- 按 key、唯一名称或完整路径解析 collection
- 读取批注、笔记、PDF 全文、页面与 Better BibTeX citation key
- 通过 plan/apply 两阶段工具导入论文、调整 collection、删除 PDF 附件
- 为缺少英文正文的条目发现并受控导入公开正式出版版 PDF；拒绝作者手稿、接受稿和预校样
- 可选使用 MinerU 批量解析 PDF，并用 QMD 建立全文检索
- 用本地 SQLite 记录重点 collection 的翻译、MinerU、QMD 和运行健康状态，供不同对话共享
- 在用户现有 collection 标准下，用 QMD 全文证据协同规划和执行文献整理
- 调用官方 PDF2zh Server 批量翻译全文，按用户配置自动命名、下载并导入译文附件；默认标题为 `CN`，文件名为“英文原文件名的全文翻译.pdf”
- 按 toolset 控制暴露给 Agent 的工具

所有 apply 工具要求精确 key 和明确的 `confirm=true`。Web API 写入前会重新读取云端状态，写后回读核验。

## Toolsets

- `literature`：检索、条目读取、collection 与论文导入
- `review`：批注、笔记、全文、MinerU 与页面工具
- `maintenance`：collection 调整与 PDF 附件删除
- `all`：全部公共工具

默认只启用 `literature`。生成 Codex 配置：

```bash
zotero-mcp setup print-codex-config --toolsets literature,review,maintenance
```

命令只输出配置块，不修改 Codex 配置文件，不包含密钥。

可用 MCP 工具：

- `zotero_ping`
- `zotero_search`
- `zotero_match`
- `zotero_item`
- `zotero_children`
- `zotero_get_annotations`
- `zotero_get_notes`
- `zotero_get_citation_key`
- `zotero_collections`
- `zotero_resolve_collection`
- `zotero_item_collections`
- `zotero_collection_items`
- `zotero_web_api_status`
- `zotero_plan_paper_import`
- `zotero_apply_paper_import`
- `zotero_plan_pdf_acquisition`
- `zotero_apply_pdf_acquisition`
- `zotero_plan_collection_reconcile`
- `zotero_apply_collection_reconcile`
- `zotero_plan_pdf_attachment_delete`
- `zotero_apply_pdf_attachment_delete`
- `zotero_plan_manual_translation_rename`
- `zotero_apply_manual_translation_rename`
- `zotero_extract_text`
- `zotero_mineru_submit`
- `zotero_mineru_result`
- `zotero_render_pages`
- `zotero_find_figure_pages`

## 可选集成

- Zotero Web API：只在写入时需要。
- MinerU：外部 PDF 解析服务，需要账号与 Token，存在调用额度限制。
- QMD：本地检索工具，无账号要求。
- SciVerse：外部文献检索服务和独立 MCP，需要账号与 Token，存在调用额度限制。
- [`paper-lookup`](https://github.com/K-Dense-AI/scientific-agent-skills/tree/main/skills/paper-lookup)：可选 Codex skill，使用多种开放学术 API；推荐与 SciVerse 互补使用。
- WebDAV：仅用于 Zotero 附件同步，不是 MCP 依赖。
- PDF2zh：独立第三方全文翻译工具；本项目提供无人值守队列和 Zotero 回写。

完整文献工作流保持四段独立边界：SciVerse 与 `paper-lookup` 互补检索候选，Zotero 查重并经 plan/apply 导入，MinerU 解析已确认的 PDF，QMD 更新索引并回读核验。任何外部上传或 Zotero 写入都需要单独批准。

## Zotero 工作流状态库

SQLite 是跨对话共享的工作流状态库，不是 Zotero 数据库，也不替代翻译队列或 QMD。它同时保存当前状态快照、MinerU 解析来源和可恢复批次回执。跟踪范围由用户初始化时指定，可同时跟踪多个递归 collection；每次 `next-batch` 或实际处理仍只针对一个明确 collection。

唯一运行状态文件：`~/.local/state/zotero-mcp/zotero_workflow.sqlite3`。CSV 不是运行状态；只有显式执行 `export-csv` 时才生成平面视图。

首次初始化时显式指定全部需要跟踪的 collection：

```bash
python -m zotero_mcp.zotero_workflow sync \
  --collection Senescence \
  --collection "Journal Club" \
  --collection Glioma
```

之后状态变化后可无参数复用已保存的跟踪范围：

```bash
python -m zotero_mcp.zotero_workflow sync
```

只处理一个 collection：

```bash
python -m zotero_mcp.zotero_workflow next-batch --collection Senescence --limit 5
```

查询汇总或单条论文：

```bash
python -m zotero_mcp.zotero_workflow status
python -m zotero_mcp.zotero_workflow status --item ITEMKEY
python -m zotero_mcp.zotero_workflow export-csv
```

同步只写入上述本地 SQLite，不修改 Zotero、PDF、MinerU、QMD 或 PDF2zh，不提交翻译任务。`export-csv` 是显式的只读导出，不参与状态恢复。`role=source_pdf`、`translated_pdf`、`supplementary_pdf` 是工作流判断；`is_primary` 只表示 Zotero 的主附件提示，不能单独用来判断英文正文。

## 正式出版版 PDF 获取

先同步统一状态库，再只读扫描缺少英文正文的条目：

```bash
zotero-workflow sync
zotero-pdf scan --collection Senescence --recursive --missing-only
zotero-pdf status --item ITEMKEY
```

扫描会查询 Crossref、OpenAlex、PMC、可选 Unpaywall 和出版社文章页，将候选及拒绝原因写入统一 SQLite；不会下载 PDF 或修改 Zotero。只自动接受能够证明为公开 Version of Record 的期刊 PDF，正式期刊条目的作者手稿、接受稿、投稿稿、预校样和旧预印本全部失败关闭。

Unpaywall 需要联系邮箱时，在用户配置中设置：

```toml
[pdf_acquisition]
unpaywall_email = "name@example.org"
```

实际下载和附件导入要求精确条目 key 和明确确认。先预演：

```bash
zotero-pdf apply --item ITEMKEY --dry-run
```

确认后执行：

```bash
zotero-pdf apply --item ITEMKEY --confirm
```

MCP 对应入口是 `zotero_apply_pdf_acquisition`，同样要求精确 key 和 `confirm=true`。执行时重新核验来源，读取一次 Zotero 当前 `attachmentRenameTemplate`，附件标题固定为 `PDF`，文件名按模板生成。不会覆盖已有英文 PDF，也不会自动触发 MinerU、QMD 或 PDF2zh。

## 协同整理 collection

QMD 建立全文索引后，Codex 可以读取用户现有分类规则，逐篇回读全文，生成 collection 调整计划。用户审阅后，工具再通过 plan/apply 两阶段执行并回读。

同一连续审核对话内，已完成且仍有效的父条目、英文 PDF、MinerU 和 QMD 预检可以复用；不因用户批准或批次衔接再次逐篇实时复核。只有发现外部状态变化、附件更换、预检失效、计划与当前状态不一致或用户明确要求时，才重新扫描。plan/apply 自身的云端冲突检查和写后回读仍必须执行。

通用流程见 [docs/COLLECTION_REVIEW.md](docs/COLLECTION_REVIEW.md)，基础记录模板见 [templates/collection_review.md](templates/collection_review.md)。

## PDF 全文翻译

用户先从官方项目安装并在 Zotero 图形界面配置 [Zotero PDF2zh](https://github.com/guaguastandup/zotero-pdf2zh)。本项目直接读取该图形界面保存的激活服务、模型和 Server 地址；API key 不进入命令行、队列、日志或仓库。

提交或检查翻译前先刷新状态库，确认目标条目没有活动任务或已有译文：

```bash
python -m zotero_mcp.zotero_workflow sync
python -m zotero_mcp.zotero_workflow status --item ITEMKEY
```

```bash
zotero-mcp setup save-secret webdav
zotero-translate doctor
zotero-translate enqueue --collection "Collection > Path" --recursive
zotero-translate run --qps 10 --pool-size 20 --max-items 3 --dry-run
zotero-translate schedule --at "2026-08-15 22:00" --qps 10 --pool-size 20 --max-items 3
```

QPS、poolSize、最多处理篇数和运行时间每批显式提供。正式运行去掉 `--dry-run`。完整配置、队列和失败恢复见 [docs/TRANSLATION.md](docs/TRANSLATION.md)。

译文命名在用户配置中统一设置；未配置时保持当前默认行为：

```toml
[translation]
attachment_title = "CN"
filename_template = "{source_stem}的全文翻译.pdf"
```

`filename_template` 必须包含且只支持 `{source_stem}` 变量，并渲染为单个 `.pdf` 文件名。用户仍可在 Zotero 中手动提交单篇翻译。默认可通过 maintenance toolset 的 plan/apply 工具事后重命名；也可显式开启 `translation.auto_rename_manual`，由后台监视器按上述配置自动重命名新译文。首次启动只记录历史基线，不批量修改旧译文。旧标题 `CN` 继续被识别，避免改配置后重复导入。两种模式都不修改 PDF2zh 插件源码或 PDF 内容。

远程 WebDAV 和 PDF2zh Server 默认必须使用 HTTPS；同机 PDF2zh Server 仍可使用 localhost HTTP。doctor 只做无写入检查，正式批处理会先创建并删除一个临时 WebDAV 探针。

## 配置位置

- Windows：`%APPDATA%\zotero-mcp\config.toml`
- WSL/Linux：`~/.config/zotero-mcp/config.toml`

可用 `ZOTERO_MCP_CONFIG` 指定配置文件，或用 `ZOTERO_MCP_CONFIG_DIR` 指定配置目录。环境变量与完整配置项见 [docs/SETUP.md](docs/SETUP.md)。

Zotero Web API key 默认保存在 Zotero MCP 配置目录下的 `zotero_web_api_key.secret`；MinerU Token 独立保存在 `~/.config/mineru/mineru_api_token.secret`。它们是仅含单行密钥的 UTF-8 纯文本文件，不是加密格式；WSL/Linux 下默认权限为 `0600`。使用 `zotero-mcp setup save-secret zotero` 或 `zotero-mcp setup save-secret mineru` 通过隐藏输入保存，不要把密钥写入仓库或提交到 Git。

## MinerU 批处理

Zotero 批处理沿用官方 `mineru-open-sdk` 的 API 契约和错误映射，并通过统一的 `NORMAL` HTTP 路由访问 MinerU，同时保留可恢复上传、Zotero item-key 目录、产物验证和 QMD 流水线。通用文件解析应另行安装官方 `mineru-open-mcp`，作为独立 MCP 使用；它与下列 Zotero 专用命令互不替代。若不希望 MCP 启动时创建空的默认输出目录，可用 `scripts/run_mineru_open_mcp_lazy.py` 启动官方 MCP；它不修改官方安装包，显式传入 `output_dir` 的行为保持不变。

单批预检与提交：

```bash
zotero-mineru plan <collection-key> --recursive
zotero-mineru submit-batch <collection-key> --recursive --max-pages 1000 --max-files 20
zotero-mineru collect <batch-id>
zotero-mineru verify <batch-id>
```

MinerU 与 QMD 的有界流水线：

```bash
zotero-mineru-qmd <collection-key> --recursive --page-budget 1000 --max-files 20
```

MinerU 论文状态和批次回执保存在 `~/.local/state/zotero-mcp/zotero_workflow.sqlite3`。不使用或生成 `mineru_todo.csv`、`.jobs` 或其他批次状态文件。替换旧解析结果时，下载内容临时放在 MinerU 输出目录的 `.staging` 中。

如果发现已有完整解析目录但 SQLite 没有记录，预检会将其标为 `untracked_existing` 并阻止重新上传。先核对当前英文附件，再显式认领：

```bash
zotero-mineru adopt-existing ITEMKEY --attachment-key ATTACHMENTKEY
zotero-mineru adopt-existing ITEMKEY --attachment-key ATTACHMENTKEY --confirm
```

认领只写入 SQLite，随后仍需正常 QMD 更新、嵌入和核验。

## 测试

```bash
python -m unittest discover -s tests -v
```

公开 Python 包位于 `src/zotero_mcp`，测试位于 `tests`；仓库根目录只保留项目元数据、文档和一级目录。

常见故障见 [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)。
