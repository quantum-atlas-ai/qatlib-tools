# Lean 文档、书目与源码检索

<a id="doc-gen"></a>

## 文档注释和网站链接

`/-! ... -/` 是模块文档，适合交代文件职责、主要定义和依赖关系。`/-- ... -/` 紧贴声明，说明语义、条件与用法。传统 Markdown 中用反引号写声明的完整限定名，例如 `Qatlib.Concepts.Example.foo`。生成网站时，doc-gen4 通过已分析环境和符号表查找链接目标，完整名称可以减少歧义。Lean LSP 的 hover 只渲染本地文档，还没有整站路由和书目索引。要让论文在 hover 中可点，用标准 Markdown 链接写 DOI 或 arXiv。 [doc-gen4 分析实现](https://github.com/leanprover/doc-gen4/blob/0e1ffe2e4ba5ecfdfaf15c65b8d3805dbb7134ae/DocGen4/Process/Analyze.lean#L164-L171) · [文档链接渲染](https://github.com/leanprover/doc-gen4/blob/0e1ffe2e4ba5ecfdfaf15c65b8d3805dbb7134ae/DocGen4/Output/DocString.lean#L167-L228)

启用 Verso 的文件可选用 `{name}` 角色，把部分名称解析交给编译器。扩用前分别检查错拼名称是否编译失败、编辑器 hover 是否正确、目标站点是否生成有效链接，以及模板摘要是否泄漏原始标记。传统注释和试点迁移可并存。

站点构建可复用与源码和工具版本匹配的 HTML/JSON 快照。恢复失败时应重新生成并核对输出。只改门户文案时仍可检查生成的链接和页面，无需为了这些检查重编整个数学库。

<a id="bibliography"></a>

## 书目引用和增量合并

在文档中用 `[CiteKey]` 引用 `docs/references.bib` 的条目。精确定位原文时在引用后加定理、页码或章节，如 `[CiteKey], Theorem X`。网站生成的条目锚点形如 `references.html#ref_<CiteKey>`，手写链接应核对最终 HTML 的 `href` 和 `id`。仅有 BibTeX 键无法说明定理与论文陈述的对应关系，仍需回读原文。 [doc-gen4 书目页面实现](https://github.com/leanprover/doc-gen4/blob/0e1ffe2e4ba5ecfdfaf15c65b8d3805dbb7134ae/DocGen4/Output/References.lean#L75-L84)

开发仓可以只维护增量书目，校验时叠加锁定的公开基线。缺键、同文件重复键和同键异文应在发布前发现。接收仓可用 `qatlib-env promotion merge-bibliography --increment <文件>` 预检，确认后加 `--write`。该命令保留原书目字节并追加新条目的原文。公开库升锁后，再清退已收录且一致的增量条目并反查引用。

<a id="lexical-index"></a>

## 本地索引和检索

`qatlib-retrieve index` 静态扫描 Lake TOML 定义的本地正式库、已按锁检出的受支持 Git 依赖和各仓的 `docs/references.bib`。没有检出的依赖会记录提示，索引过程无需启动编译器、联网补依赖或占用构建池。扫描按每个源码仓的正式库根裁剪 `.git`、`.lake`、`.venv` 和任务目录，也会跳过不受支持的依赖布局。初次建立索引后可用 `index --changed` 更新。

默认文件为 `${XDG_CACHE_HOME:-~/.cache}/qatlib/retrieve/<corpus-key>.sqlite3`。键取解析后的项目根绝对路径的 SHA-256 前 24 个十六进制字符。用 `--project` 指定目标项目，用 `--db` 另选索引文件，所选路径须在所有入索引的源码仓之外。于是每个检出的查询和增量刷新都能独立进行。

`query` 和 `find` 只读已有索引，源码签名和外层 `section` 的 `variable` 可用于查重及核对隐式前提，再打开命中源码确认。词法索引无法保证覆盖宏展开、结构体自动生成项或编译器派生声明，最终以真实 Lean 编译和消费者验证为准。旧格式索引可直接重新建立。
