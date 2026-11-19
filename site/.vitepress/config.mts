import { defineConfig } from 'vitepress';

export default defineConfig({
  base: '/qatlib-tools/',
  srcDir: 'src',
  outDir: 'src/.vitepress/dist',
  cleanUrls: true,
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
          { text: '快速上手', link: '/guide' },
          { text: '维护机制手册', link: '/docs/build' },
          { text: '设计原则', link: '/design' },
          { text: 'Qatlib 主站', link: 'https://quantum-atlas-ai.github.io/qatlib/' },
        ],
        sidebar: [
          {
            text: '总览与入门',
            items: [
              { text: '快速上手与命令总览', link: '/guide' },
              { text: '设计原则与维护边界', link: '/design' },
              { text: '贡献与自测指南', link: '/contributing' },
            ],
          },
          {
            text: '工程维护机制',
            items: [
              { text: '编译环境、缓存与资源控制', link: '/docs/build' },
              { text: '持续集成 Runner 与进程排障', link: '/docs/runner' },
              { text: 'Lean 文档、书目与源码检索', link: '/docs/lean-docs' },
              { text: 'GitHub 与 Gitea 拉取请求', link: '/docs/forge' },
              { text: '共享成果进入公开库', link: '/docs/promotion' },
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
          { text: 'Quickstart', link: '/en/guide' },
          { text: 'Engineering Guides', link: '/en/docs/build' },
          { text: 'Design Principles', link: '/en/design' },
          { text: 'Qatlib Portal', link: 'https://quantum-atlas-ai.github.io/qatlib/' },
        ],
        sidebar: [
          {
            text: 'Overview',
            items: [
              { text: 'Quickstart & CLI Overview', link: '/en/guide' },
              { text: 'Design Principles & Boundaries', link: '/en/design' },
              { text: 'Contributing & Self-Test', link: '/en/contributing' },
            ],
          },
          {
            text: 'Engineering Guides',
            items: [
              { text: 'Build Environment, Cache & Budget', link: '/en/docs/build' },
              { text: 'CI Runners & Process Isolation', link: '/en/docs/runner' },
              { text: 'Docstrings, Bibliography & Retrieval', link: '/en/docs/lean-docs' },
              { text: 'Pull Requests on GitHub & Gitea', link: '/en/docs/forge' },
              { text: 'Promoting Shared Results Upstream', link: '/en/docs/promotion' },
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
