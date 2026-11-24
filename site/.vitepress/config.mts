import { defineConfig } from 'vitepress';

export default defineConfig({
  base: process.env.VITEPRESS_BASE || '/qatlib-tools/',
  srcDir: 'src',
  outDir: 'src/.vitepress/dist',
  cleanUrls: process.env.VITEPRESS_CLEAN_URLS !== 'false',
  lastUpdated: false,
  markdown: {
    theme: 'github-dark',
  },
  head: [
    ['meta', { name: 'theme-color', content: '#263f5f' }],
  ],
  themeConfig: {
    search: {
      provider: 'local',
      options: {
        locales: {
          root: {
            translations: {
              button: { buttonText: '搜索文档', buttonAriaLabel: '搜索文档' },
              modal: {
                noResultsText: '未找到匹配内容',
                resetButtonTitle: '清除查询条件',
                footer: { selectText: '跳转', navigateText: '切换', closeText: '关闭' },
              },
            },
          },
        },
      },
    },
    socialLinks: [
      { icon: 'github', link: 'https://github.com/quantum-atlas-ai/qatlib-tools' },
    ],
  },
  locales: {
    root: {
      label: '简体中文',
      lang: 'zh-CN',
      title: 'qatlib-tools',
      description: 'Lean 4 形式化项目工程维护手册与共享工具链',
      themeConfig: {
        siteTitle: false,
        nav: [
          { text: '五分钟验证', link: '/docs/quickstart' },
          { text: '在 Lean 仓库里工作', link: '/docs/worktrees' },
          { text: '维护者指南', link: '/docs/github-ci' },
          { text: 'Qatlib 主站', link: 'https://quantum-atlas-ai.github.io/qatlib/' },
        ],
        sidebar: [
          {
            text: '按顺序做完',
            items: [
              { text: '手册首页', link: '/guide' },
              { text: '五分钟验证', link: '/docs/quickstart' },
              { text: '安装与资源占用', link: '/docs/install' },
              { text: '第二棵工作树', link: '/docs/worktrees' },
              { text: '第一张草稿', link: '/docs/pull-requests' },
              { text: '贡献与自测', link: '/contributing' },
            ],
          },
          {
            text: '维护者',
            items: [
              { text: 'GitHub 工作流与 Pages', link: '/docs/github-ci' },
              { text: '审阅并行提交', link: '/docs/forge' },
              { text: '自托管 Runner', link: '/docs/runner' },
              { text: 'Runner 排障', link: '/docs/runner-troubleshooting' },
            ],
          },
          {
            text: '编译与验证',
            items: [
              { text: '编译、缓存与预算', link: '/docs/build' },
              { text: '证明复核与编译门禁', link: '/docs/verification' },
            ],
          },
          {
            text: '按需查阅',
            items: [
              { text: '文档、书目与检索', link: '/docs/lean-docs' },
              { text: '进入公开库', link: '/docs/promotion' },
              { text: '设计原则与维护边界', link: '/design' },
            ],
          },
        ],
        outline: {
          level: [2, 3],
          label: '本页目录',
        },
        docFooter: {
          prev: '上一篇',
          next: '下一篇',
        },
        darkModeSwitchLabel: '外观主题',
        lightModeSwitchTitle: '切换至浅色模式',
        darkModeSwitchTitle: '切换至深色模式',
        sidebarMenuLabel: '章节导航',
        returnToTopLabel: '回到顶部',
        footer: {
          message: '以 Apache License 2.0 协议开源发布 · 与 Qatlib 配套维护',
        },
      },
    },
    en: {
      label: 'English',
      lang: 'en-US',
      link: '/en/',
      title: 'qatlib-tools',
      description: 'Engineering Handbook & Shared Tooling for Lean 4 Formalization Repositories',
      themeConfig: {
        siteTitle: false,
        nav: [
          { text: 'Five-minute check', link: '/en/docs/quickstart' },
          { text: 'Work in a Lean repo', link: '/en/docs/worktrees' },
          { text: 'Maintainer guide', link: '/en/docs/github-ci' },
          { text: 'Qatlib Portal', link: 'https://quantum-atlas-ai.github.io/qatlib/' },
        ],
        sidebar: [
          {
            text: 'Do these in order',
            items: [
              { text: 'Handbook', link: '/en/guide' },
              { text: 'Five-minute check', link: '/en/docs/quickstart' },
              { text: 'Install and resource usage', link: '/en/docs/install' },
              { text: 'A second worktree', link: '/en/docs/worktrees' },
              { text: 'First draft', link: '/en/docs/pull-requests' },
              { text: 'Contributing', link: '/en/contributing' },
            ],
          },
          {
            text: 'Maintainers',
            items: [
              { text: 'GitHub workflows and Pages', link: '/en/docs/github-ci' },
              { text: 'Review concurrent commits', link: '/en/docs/forge' },
              { text: 'Self-hosted runners', link: '/en/docs/runner' },
              { text: 'Runner troubleshooting', link: '/en/docs/runner-troubleshooting' },
            ],
          },
          {
            text: 'Build and verification',
            items: [
              { text: 'Build, cache, and budget', link: '/en/docs/build' },
              { text: 'Proof verification and build gates', link: '/en/docs/verification' },
            ],
          },
          {
            text: 'Look up when needed',
            items: [
              { text: 'Docs, bibliography, retrieval', link: '/en/docs/lean-docs' },
              { text: 'Entering the public library', link: '/en/docs/promotion' },
              { text: 'Design principles', link: '/en/design' },
            ],
          },
        ],
        outline: {
          level: [2, 3],
          label: 'On this page',
        },
        footer: {
          message: 'Released under the Apache License 2.0 · Companion tooling for Qatlib',
        },
      },
    },
  },
});
