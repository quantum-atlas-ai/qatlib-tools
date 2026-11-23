---
layout: home

hero:
  name: "qatlib-tools"
  text: "Lean 4 形式化项目的工程手册与工具"
  tagline: "先确认命令能跑，再在第二棵工作树里改一个模块，然后看当前提交的检查。缓存、Runner 和审阅规则留在后面的手册里。"
  actions:
    - theme: brand
      text: 五分钟验证工具 →
      link: /docs/quickstart
    - theme: alt
      text: 在 Lean 仓库里工作
      link: /docs/worktrees
    - theme: alt
      text: 维护者与 Runner
      link: /docs/github-ci

features:
  - title: 五分钟确认工具能用
    details: 在本工具仓的检出里执行 uv tool install .，然后看 qatlib-env docs --list 和 qatlib-env config explain --json。这一步不下载 Lean 工具链。
    link: /docs/quickstart
    linkText: 从五分钟验证开始
  - title: 第二棵工作树里的一次修改
    details: 从已锁定工具版本的 Lean 仓库创建工作树，先 plan，再 prepare，然后构建一个模块，并用 --no-build 检查刚改的文件。清理只做预览。
    link: /docs/worktrees
    linkText: 按步骤做完一次修改
  - title: 第一张草稿和当前提交的检查
    details: 推送分支，开 Draft，写下命令、结果和未运行的项，再按 head SHA 阅读 Checks。本仓的 CI 对文档改动同样运行。
    link: /docs/pull-requests
    linkText: 交出第一张草稿
  - title: 工作流、Pages 和凭据
    details: CI 与文档站是两份工作流。拉取请求上的 CI 通过，并不表示 Pages 已经部署。注册令牌、GITHUB_TOKEN 和镜像密钥各有入口。
    link: /docs/github-ci
    linkText: 看清两份工作流
  - title: 自托管 Runner
    details: 服务账号按 lane 分配。同一 lane 的实例共用账号，各自拥有工作目录、注册名和资源 drop-in。页缓存和取消后的进程按症状另查。
    link: /docs/runner
    linkText: 按顺序完成部署验收
  - title: 需要时再查阅
    details: 缓存开关、检索与书目、跨仓晋升，以及设计契约，都从一项具体任务链回机制说明。
    link: /docs/build
    linkText: 打开编译与缓存参考
---

<div class="vp-doc carbon-home-strip">

## 先确认命令可用

在本工具仓的检出根目录执行。固定提交、wheel、Windows 和下游仓库锁定的版本见[安装方式](/docs/install)。

```bash
uv tool install .
qatlib-env docs --list
qatlib-env config explain --json
```

`qatlib-env docs` 阅读中文原文。英文页面在本站的 English 入口。

</div>
