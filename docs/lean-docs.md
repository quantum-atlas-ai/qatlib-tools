# Lean 文档、书目与源码检索

当前目录是一个已经按锁检出依赖的 Lean 项目根目录。先建索引，做一次查询，再打开命中的源码。网站渲染、书目归并和旧索引路径放在查询之后。

<a id="lexical-index"></a>

## 先索引，再查询，再回源码

```bash
qatlib-retrieve index
qatlib-retrieve query 'postselection normalization' --kind theorem
qatlib-retrieve query --cite GilyenSuLowWiebe2019
```

`index` 成功时进程退出码为 0，并且没有启动 Lean 编译器或 Lake，也没有联网补齐依赖或申请构建配额。`query` 成功时打印命中声明的位置。打开那个文件，用命中结果里的源码签名和外层 `section` 的 `variable` 核对隐式前提。上面的查询字符串是示例；换成你要找的词或引用键。

`qatlib-retrieve index` 静态扫描 Lake TOML 定义的本地正式库、已按锁检出的受支持 Git 依赖，以及这些仓库中的 `docs/references.bib`。尚未检出的依赖会被跳过并给出提示。扫描以各源码仓的正式库根为基准，自动排除 `.git`、`.lake`、`.venv` 和 `.agent-tasks`。首次建索引之后，用 `qatlib-retrieve index --changed` 做增量更新。

默认索引路径为 `${QATLIB_HOME:-~/.qatlib}/retrieve/<corpus-key>.sqlite3`，其中 `<corpus-key>` 取解析后的项目根绝对路径的 SHA-256 前 24 个十六进制字符。可以用 `--project` 指定项目，或用 `--db` 另选索引文件，但所选路径必须位于所有纳入索引的源码仓之外。新索引还没建立时，查询命令可以只读打开旧目录 `${XDG_CACHE_HOME:-~/.cache}/qatlib/retrieve/` 中的对应文件。直接运行 `index` 会在新目录重建。若本地只有旧索引，第一次 `index --changed` 同样会在新目录全量重建，并保留旧文件。

`query` 和 `find` 只读已有索引。遇到旧格式时，重新建立即可。宏展开、结构体自动生成项和编译器派生声明可能未被收录。最终仍以 Lean 编译和消费者验证为准。

<a id="doc-gen"></a>

## 文档注释和网站链接

Lean 源码使用两类文档注释：`/-! ... -/` 是模块文档，适合交代文件职责、主要定义与依赖关系；`/-- ... -/` 紧贴具体声明，用于说明其语义、前提条件与用法。在传统 Markdown 注释中，可以用反引号包裹声明的完整限定名（例如 `` `MyLib.Facts.some_fact` ``）。生成网站时，doc-gen4 会根据已分析的环境和符号表查找链接目标（参见 [doc-gen4 分析实现](https://github.com/leanprover/doc-gen4/blob/0e1ffe2e4ba5ecfdfaf15c65b8d3805dbb7134ae/DocGen4/Process/Analyze.lean#L164-L171) 与 [文档链接渲染](https://github.com/leanprover/doc-gen4/blob/0e1ffe2e4ba5ecfdfaf15c65b8d3805dbb7134ae/DocGen4/Output/DocString.lean#L167-L228)）。编辑器里的 Lean LSP hover 只渲染本地文档，没有网站的路由和书目索引。希望在 hover 里也能打开论文时，在注释中写指向 DOI 或 arXiv 的普通 Markdown 链接。

启用 Verso 的文件可以使用 `{name}` 角色，由编译器解析名称。传统注释可以和 Verso 试点并存。扩大使用范围之前，分别检查错拼名称能否被编译器发现、编辑器 hover 是否正常、网站链接能否跳转，以及模板摘要是否直接显示了原始标记。

构建项目文档站点时，可以复用与当前源码及工具版本匹配的 HTML/JSON 快照。快照恢复失败时，重新生成并核对输出。改动只涉及门户文案时，直接检查对应页面和链接。

<a id="bibliography"></a>

## 书目引用和增量合并

文档中用 `[CiteKey]` 引用 `docs/references.bib`。需要精确定位时，在引用键后补充定理号、页码或章节，例如 `[CiteKey], Theorem X`。站点生成的文献条目链接形如 `references.html#ref_<CiteKey>`（参见 [doc-gen4 书目页面实现](https://github.com/leanprover/doc-gen4/blob/0e1ffe2e4ba5ecfdfaf15c65b8d3805dbb7134ae/DocGen4/Output/References.lean#L75-L84)）。手写链接时核对最终 HTML 里的 `href` 和 `id`。形式化定理与论文陈述是否一致，仍要回读原文。

开发仓日常可以只维护增量书目。校验时会叠上锁定的公开基线，一起核对缺键、同文件重复键和同键异文。合入接收仓时，先在接收仓运行 `qatlib-env promotion merge-bibliography --increment <文件>` 预检，通过后再加 `--write`。该命令原样保留 `docs/references.bib` 的原始字节，只追加新条目的原文。下游升锁之后，清理已经收录且内容一致的增量条目，再反查引用。
