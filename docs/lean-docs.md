# Lean 文档、书目与源码检索

<a id="doc-gen"></a>

## 文档注释和网站链接

`/-! ... -/` 是模块文档，适合交代文件职责、主要定义和依赖关系。`/-- ... -/` 紧贴声明，说明语义、前提和用法。传统 Markdown 注释用反引号包住声明的完整限定名，例如 `Qatlib.Concepts.Example.foo`。生成网站时，doc-gen4 根据已分析的环境和符号表查找链接目标。编辑器的 Lean LSP hover 渲染本地文档，缺少网站的路由和书目索引。要在 hover 中直接打开论文，可以用标准 Markdown 链接指向 DOI 或 arXiv。 [doc-gen4 分析实现](https://github.com/leanprover/doc-gen4/blob/0e1ffe2e4ba5ecfdfaf15c65b8d3805dbb7134ae/DocGen4/Process/Analyze.lean#L164-L171) · [文档链接渲染](https://github.com/leanprover/doc-gen4/blob/0e1ffe2e4ba5ecfdfaf15c65b8d3805dbb7134ae/DocGen4/Output/DocString.lean#L167-L228)

启用 Verso 的文件可用 `{name}` 角色，让编译器解析名称。扩大使用范围前，分别检查错拼名称能否被编译器发现、编辑器 hover 是否正常、网站链接能否跳转，以及模板摘要是否显示了原始标记。传统注释可与 Verso 试点并存。

项目的文档站点可以复用与源码和工具版本匹配的 HTML/JSON 快照。恢复失败时重新生成，并核对输出。只改门户文案时，可以直接检查对应的页面与链接。

<a id="bibliography"></a>

## 书目引用和增量合并

用 `[CiteKey]` 引用 `docs/references.bib` 的文献条目。需要精确定位时在键后加定理号、页码或章节，例如 `[CiteKey], Theorem X`。文献条目链接形如 `references.html#ref_<CiteKey>`，手写链接时核对最终 HTML 的 `href` 和 `id`。形式化定理与论文陈述是否一致，仍需回读原文核对。 [doc-gen4 书目页面实现](https://github.com/leanprover/doc-gen4/blob/0e1ffe2e4ba5ecfdfaf15c65b8d3805dbb7134ae/DocGen4/Output/References.lean#L75-L84)

开发仓可只维护增量书目，校验时叠加锁定的公开基线，核对缺键、同文件重复键和同键异文。接收仓可用 `qatlib-env promotion merge-bibliography --increment <文件>` 预检，通过后加 `--write`。命令保留 `docs/references.bib` 的原始字节，追加新条目的原文。下游升锁后，可清理已收录且内容一致的增量条目，再反查引用。

<a id="lexical-index"></a>

## 本地索引和检索

`qatlib-retrieve index` 静态扫描 Lake TOML 定义的本地正式库、已按锁检出的受支持 Git 依赖，以及这些仓库的 `docs/references.bib`。尚未检出的依赖会被跳过并提示。索引无需启动 Lean 编译器或 Lake，也不联网补依赖或申请构建配额。扫描以各源码仓的正式库根为基准，排除 `.git`、`.lake`、`.venv` 和 `.agent-tasks`。首次建索引后，可用 `qatlib-retrieve index --changed` 增量更新。

默认索引为 `${QATLIB_HOME:-~/.qatlib}/retrieve/<corpus-key>.sqlite3`。键取解析后的项目根绝对路径的 SHA-256 前 24 个十六进制字符。用 `--project` 指定项目，用 `--db` 另选文件，路径须在所有纳入索引的源码仓之外。新索引尚未建立时，查询可只读打开旧 `${XDG_CACHE_HOME:-~/.cache}/qatlib/retrieve/` 中对应的文件。直接运行 `index` 可重建到新目录；若仅有旧索引，首次 `index --changed` 同样在新目录全量重建，保留旧文件。

`query` 和 `find` 只读已有索引。可以先用命中结果中的源码签名和外层 `section` 的 `variable` 查重、核对隐式前提，再打开源码确认。宏展开、结构体自动生成项和编译器派生声明可能未被收录，最终以 Lean 编译和消费者验证为准。旧格式索引可直接重新建立。
