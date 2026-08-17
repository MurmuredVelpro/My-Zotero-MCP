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
- 可选使用 MinerU 批量解析 PDF，并用 QMD 建立全文检索
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

## 协同整理 collection

QMD 建立全文索引后，Codex 可以读取用户现有分类规则，逐篇回读全文，生成 collection 调整计划。用户审阅后，工具再通过 plan/apply 两阶段执行并回读。

通用流程见 [docs/COLLECTION_REVIEW.md](docs/COLLECTION_REVIEW.md)，基础记录模板见 [templates/collection_review.md](templates/collection_review.md)。

## PDF 全文翻译

用户先从官方项目安装并在 Zotero 图形界面配置 [Zotero PDF2zh](https://github.com/guaguastandup/zotero-pdf2zh)。本项目直接读取该图形界面保存的激活服务、模型和 Server 地址；API key 不进入命令行、队列、日志或仓库。

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

## MinerU 批处理

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

批次状态保存在 MinerU 输出目录的 `.jobs` 中。ledger 默认是同目录下的 `mineru_todo.csv`，也可通过配置项 `mineru.ledger` 或环境变量 `ZOTERO_MINERU_LEDGER` 指定。

## 测试

```bash
python -m unittest discover -s tests -v
```

公开 Python 包位于 `src/zotero_mcp`，测试位于 `tests`；仓库根目录只保留项目元数据、文档和一级目录。

常见故障见 [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)。
