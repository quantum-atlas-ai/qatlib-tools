---
layout: home

hero:
  name: "qatlib-tools"
  text: "Lean 4 形式化项目工程维护手册与工具链"
  tagline: "将大规模并行形式化研发中验证过的编译缓存、Runner 子进程隔离、静态词法检索、GitHub / Gitea 门禁对齐与跨仓无损晋升沉淀为通用知识与轻量工具。"
  actions:
    - theme: brand
      text: 阅读工程维护手册 →
      link: /docs/build
    - theme: alt
      text: 快速上手与 CLI
      link: /guide
    - theme: alt
      text: 设计原则
      link: /design

features:
  - title: 编译环境与共享工件缓存
    details: 厘清 lakefile.toml 的 enableArtifactCache 与 restoreAllArtifacts 九种开关表现、Lean 4.35 module 可见性边界、三类缓存机制分工以及 Windows 跨盘注意事项。
    link: /docs/build
    linkText: 阅读编译与缓存机制
  - title: Runner 资源计费与进程回收
    details: 解析 Linux cgroup v2 页缓存计费假象，通过父死信号守卫、subreaper 子孙收养与瞬态 systemd scope 根治任务取消后的孤儿编译进程驻留。
    link: /docs/runner
    linkText: 阅读 Runner 与进程隔离
  - title: 文档注释、书目与静态检索
    details: 对比 doc-gen4 整站符号路由与本地 LSP hover 的渲染边界，支持增量 BibTeX 书目校验与无需启动编译器的毫秒级源码词法检索。
    link: /docs/lean-docs
    linkText: 阅读文档与检索机制
  - title: GitHub 与 Gitea 并发门禁核验
    details: >-
      核对 GitHub 的 isDraft 状态和 Gitea 的草稿标题前缀，按提交哈希对齐作业结论，并在合入或删除分支前校验远端提交。
    link: /docs/forge
    linkText: 阅读双平台协作机制
  - title: 跨仓成果晋升与无损切锁
    details: 以完整源文件为最小闭包单元，配合普通 module 消费者探针、预发阶段草稿演练以及正式发版后的原子升锁与旧副本清退，避免上下游断供或重名冲突。
    link: /docs/promotion
    linkText: 阅读跨仓晋升机制
  - title: 共享工具与离线手册
    details: 日常单步操作使用 git、gh、tea 或 gt。五篇工程专题均随 Python 包离线分发，可通过 qatlib-env docs 阅读中文原文。
    link: /design
    linkText: 查看仓库设计原则
---

<div class="vp-doc carbon-home-strip">

## 三条入口与离线查阅

安装一次即可获得 `qatlib-env`、`qatlib-retrieve` 与 `qatlib-forge` 三个命令行工具，全部专题手册均内置于分发包中：

```bash
# 使用 uv 独立安装命令行入口（亦支持 pipx 或标准库 venv + pip，详见快速上手）
uv tool install 'git+https://github.com/quantum-atlas-ai/qatlib-tools.git'

# 在终端离线列出并阅读本站同源的五篇工程维护专题
qatlib-env docs --list
qatlib-env docs build
```

</div>
