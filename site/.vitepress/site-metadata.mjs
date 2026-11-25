import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { languagePaths, pagePath } from './routes.mjs';

const origin = 'https://quantum-atlas-ai.github.io';
const imageName = 'qatlib-tools-social-preview.png';

function pageSummary(source) {
  const withoutFrontmatter = source.replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n/, '');
  const content = withoutFrontmatter.replace(/<!--[\s\S]*?-->/g, '');
  const paragraph = content.split(/\r?\n\s*\r?\n/)
    .map((block) => block.trim())
    .find((block) => /^[\p{L}\p{N}]/u.test(block));
  return paragraph?.replace(/!?\[([^\]]+)\]\([^)]+\)/g, '$1')
    .replace(/[`*]/g, '').replace(/\s+/g, ' ').trim();
}

export function siteMetadata(base, cleanUrls) {
  const siteUrl = origin + base;
  const image = siteUrl + imageName;
  const absolutePage = (page) => siteUrl + pagePath(page, cleanUrls);
  return {
    sitemap: {
      hostname: siteUrl,
      transformItems: (items) => items.map((item) => ({
        ...item,
        links: [...(item.links ?? []), { lang: 'x-default', url: siteUrl }],
      })),
    },
    transformPageData(pageData, { siteConfig }) {
      const page = pageData.relativePath;
      const source = readFileSync(join(siteConfig.srcDir, page), 'utf8');
      const summary = pageData.frontmatter.hero?.tagline || pageSummary(source);
      if (summary && !pageData.description) {
        pageData.description = summary.length > 200 ? summary.slice(0, 197).trimEnd() + '…' : summary;
      }
      const title = [pageData.title, 'qatlib-tools'].filter(Boolean).join(' | ');
      const url = absolutePage(page);
      const routes = languagePaths(page, cleanUrls);
      const english = page.startsWith('en/');
      const meta = (key, value, attribute = 'property') => ['meta', { [attribute]: key, content: value }];
      const link = (lang, href) => ['link', { rel: 'alternate', hreflang: lang, href }];
      pageData.frontmatter.head = [
        ...(pageData.frontmatter.head ?? []),
        ['link', { rel: 'canonical', href: url }],
        link('zh-Hans', siteUrl + routes['zh-Hans']),
        link('en', siteUrl + routes.en),
        link('x-default', siteUrl),
        meta('og:type', 'website'),
        meta('og:site_name', 'qatlib-tools'),
        meta('og:locale', english ? 'en_US' : 'zh_CN'),
        meta('og:locale:alternate', english ? 'zh_CN' : 'en_US'),
        meta('og:url', url),
        meta('og:title', title),
        meta('og:description', pageData.description),
        meta('og:image', image),
        meta('og:image:width', '1280'),
        meta('og:image:height', '640'),
        meta('twitter:card', 'summary_large_image', 'name'),
        meta('twitter:title', title, 'name'),
        meta('twitter:description', pageData.description, 'name'),
        meta('twitter:image', image, 'name'),
      ];
    },
    transformHead({ pageData }) {
      // Render the description as a head attribute so VitePress escapes it.
      if (pageData.description) return [['meta', { name: 'description', content: pageData.description }]];
    },
    transformHtml(html, _file, { siteConfig }) {
      // Preserve the VitePress 1.6 first-paint checks with script-src 'self'.
      return html.replace(/<script id="(check-dark-mode|check-mac-os)">([\s\S]*?)<\/script>/g,
        (_tag, id, code) => {
          const filename = `${id}.js`;
          const assetsDir = join(siteConfig.outDir, siteConfig.assetsDir);
          mkdirSync(assetsDir, { recursive: true });
          writeFileSync(join(assetsDir, filename), code);
          return `<script id="${id}" src="${base}${siteConfig.assetsDir}/${filename}"></script>`;
        });
    },
    buildEnd({ outDir, srcDir, pages }) {
      const list = (english) => pages.filter((page) => page.startsWith('en/') === english)
        .sort().map((page) => {
          const source = readFileSync(join(srcDir, page), 'utf8');
          const title = /(^|\n)# (.+)/.exec(source)?.[2].trim() || (english ? 'Home' : '首页');
          return `- [${title}](${absolutePage(page)})`;
        });
      writeFileSync(join(outDir, 'llms.txt'), [
        '# qatlib-tools', '',
        '> Engineering handbook and reusable Lean tooling for Qatlib', '',
        '## 中文', '', ...list(false), '', '## English', '', ...list(true), '',
      ].join('\n'));
    },
  };
}
