# Lean 文档、书目与源码检索

<a id="doc-gen"></a>

## 文档注释和网站链接

Lean 源码使用两类文档注释组织说明：`/-! ... -/` 是模块文档，适合交代文件职责、主要定义与依赖关系；`/-- ... -/` 紧贴具体声明，用于说明其语义、前提条件与用法。在传统 Markdown 注释中，可以用反引号包裹声明的完整限定名（例如 `MyLib.Facts.some_fact`）；生成网站时，doc-gen4 会根据已分析的环境和符号表查找链接目标（参见 [doc-gen4 分析实现](https://github.com/leanprover/doc-gen4/blob/0e1ffe2e4ba5ecfdfaf15c65b8d3805dbb7134ae/DocGen4/Process/Analyze.lean#L164-L171) 与 [文档链接渲染](https://github.com/leanprover/doc-gen4/blob/0e1ffe2e4ba5ecfdfaf15c65b8d3805dbb7134ae/DocGen4/Output/DocString.lean#L167-L228)）。相比之下，编辑器的 Lean LSP hover 仅渲染本地文档，缺少网站的路由机制和书目索引；若希望在 hover 中也能直接打开对应论文，可使用标准 Markdown 链接指向 DOI 或 arXiv。

对于启用 Verso 的文件，可以使用 `{name}` 角色交由编译器解析名称。传统注释可以与 Verso 试点并存；在扩大使用范围之前，应分别检查错拼名称能否被编译器发现、编辑器 hover 是否正常、网站链接能否正确跳转，以及模板摘要是否直接显示了原始标记。

构建项目文档站点时，可以复用与当前源码及工具版本匹配的 HTML/JSON 快照；若快照恢复失败，则重新生成并核对输出。如果改动仅涉及门户文案，直接检查对应的页面与链接即可。

<a id="bibliography"></a>

## 书目引用和增量合并

文档中可通过 `[CiteKey]` 引用 `docs/references.bib` 中的文献条目；需要精确定位时，可在引用键后补充定理号、页码或章节，例如 `[CiteKey], Theorem X`。站点生成的文献条目链接形如 `references.html#ref_<CiteKey>`（参见 [doc-gen4 书目页面实现](https://github.com/leanprover/doc-gen4/blob/0e1ffe2e4ba5ecfdfaf15c65b8d3805dbb7134ae/DocGen4/Output/References.lean#L75-L84)），手写链接时应核对最终 HTML 中的 `href` 和 `id`。至于形式化定理与论文陈述是否一致，仍需回读原文核对。

开发仓日常可只维护增量书目，校验时会叠加锁定的公开基线，一并核对缺键、同文件重复键和同键异文。合入接收仓时，可在接收仓先用 `qatlib-env promotion merge-bibliography --increment <文件>` 预检，通过后再加 `--write` 写入；该命令会原样保留 `docs/references.bib` 的原始字节，仅追加新条目的原文。待下游升锁后，即可清理已收录且内容一致的增量条目，随后再反查引用。

<a id="lexical-index"></a>

## 本地索引和检索

`qatlib-retrieve index` 会静态扫描 Lake TOML 定义的本地正式库、已按锁检出的受支持 Git 依赖，以及这些仓库中的 `docs/references.bib`；尚未检出的依赖会被跳过并给出提示。整个索引过程无需启动 Lean 编译器或 Lake，也不会联网补齐依赖或申请构建配额。扫描以各源码仓的正式库根为基准，自动排除 `.git`、`.lake`、`.venv` 和 `.agent-tasks`。完成首次建索引后，后续可用 `qatlib-retrieve index --changed` 进行增量更新。

默认索引路径为 `${QATLIB_HOME:-~/.qatlib}/retrieve/<corpus-key>.sqlite3`，其中 `<corpus-key>` 取解析后的项目根绝对路径的 SHA-256 前 24 个十六进制字符。可通过 `--project` 指定项目，或用 `--db` 另选索引文件，但所选路径须位于所有纳入索引的源码仓之外。当新索引尚未建立时，查询命令可按只读方式打开旧目录 `${XDG_CACHE_HOME:-~/.cache}/qatlib/retrieve/` 中对应的文件；直接运行 `index` 会重建到新目录，若本地仅有旧索引，首次执行 `index --changed` 同样会在新目录全量重建，并保留旧文件。

`query` 和 `find` 仅以只读方式访问已有索引；若遇到旧格式索引，可直接重新建立。检索时，可先借助命中结果中的源码签名与外层 `section` 的 `variable` 查重并核对隐式前提，再打开源码确认。受静态扫描方式所限，宏展开、结构体自动生成项和编译器派生声明可能未被收录，最终仍以 Lean 编译和消费者验证为准。
