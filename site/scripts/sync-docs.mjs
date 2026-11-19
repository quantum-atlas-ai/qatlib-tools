import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const siteDir = path.resolve(__dirname, '..');
const repoRoot = path.resolve(siteDir, '..');
const srcDir = path.join(siteDir, 'src');

function rewriteLinks(content, { isEn = false, inDocsSubdir = false } = {}) {
  let out = content;
  // Strip human-contract HTML comments from rendered site output
  out = out.replace(/^\s*<!--\s*human-contract:(?:start|end)\s*-->\s*\n?/gm, '');

  const prefix = isEn ? '/en' : '';

  // Map root Markdown cross-references to clean VitePress routes
  const rootMap = {
    'README.md': '/guide',
    'README.en.md': `/en/guide`,
    './README.md': `${prefix}/guide`,
    '../README.md': `${prefix}/guide`,
    'DESIGN.md': `${prefix}/design`,
    'DESIGN.en.md': `/en/design`,
    './DESIGN.md': `${prefix}/design`,
    '../DESIGN.md': `${prefix}/design`,
    'CONTRIBUTING.md': `${prefix}/contributing`,
    'CONTRIBUTING.en.md': `/en/contributing`,
    './CONTRIBUTING.md': `${prefix}/contributing`,
    '../CONTRIBUTING.md': `${prefix}/contributing`,
    './LICENSE': 'https://github.com/quantum-atlas-ai/qatlib-tools/blob/main/LICENSE',
    'LICENSE': 'https://github.com/quantum-atlas-ai/qatlib-tools/blob/main/LICENSE',
    'docs/design/human-contracts.sha256':
      'https://github.com/quantum-atlas-ai/qatlib-tools/blob/main/docs/design/human-contracts.sha256',
  };

  out = out.replace(/\]\(([^)]+)\)/g, (full, target) => {
    if (/^(https?:|mailto:|#)/.test(target)) return full;
    const [rawPath, hash] = target.split('#');
    const anchor = hash ? `#${hash}` : '';

    if (rootMap[rawPath]) {
      return `](${rootMap[rawPath]}${anchor})`;
    }
    // Map docs/<topic>.md or docs/en/<topic>.md or sibling <topic>.md inside docs/
    const mDocsEn = rawPath.match(/^(?:\.\/|\.\.\/)?docs\/en\/([a-z0-9-]+)\.md$/);
    if (mDocsEn) {
      return `](/en/docs/${mDocsEn[1]}${anchor})`;
    }
    const mDocs = rawPath.match(/^(?:\.\/|\.\.\/)?docs\/([a-z0-9-]+)\.md$/);
    if (mDocs) {
      return `](${prefix}/docs/${mDocs[1]}${anchor})`;
    }
    if (inDocsSubdir) {
      const mSibling = rawPath.match(/^(?:\.\/)?([a-z0-9-]+)\.md$/);
      if (mSibling && ['build', 'runner', 'lean-docs', 'forge', 'promotion'].includes(mSibling[1])) {
        return `](${prefix}/docs/${mSibling[1]}${anchor})`;
      }
    }
    return full;
  });

  return out;
}

function copyMapped(relSrc, relDest, opts) {
  const fullSrc = path.join(repoRoot, relSrc);
  if (!fs.existsSync(fullSrc)) {
    throw new Error(`Missing documentation source file: ${fullSrc}`);
  }
  const raw = fs.readFileSync(fullSrc, 'utf8');
  const transformed = rewriteLinks(raw, opts);
  const fullDest = path.join(srcDir, relDest);
  fs.mkdirSync(path.dirname(fullDest), { recursive: true });
  fs.writeFileSync(fullDest, transformed, 'utf8');
}

fs.mkdirSync(path.join(srcDir, 'en'), { recursive: true });

// Copy landing pages crafted for VitePress root
fs.copyFileSync(path.join(siteDir, 'base-pages/index.md'), path.join(srcDir, 'index.md'));
fs.copyFileSync(path.join(siteDir, 'base-pages/en/index.md'), path.join(srcDir, 'en/index.md'));

// Chinese single-source documents
copyMapped('README.md', 'guide.md', { isEn: false, inDocsSubdir: false });
copyMapped('DESIGN.md', 'design.md', { isEn: false, inDocsSubdir: false });
copyMapped('CONTRIBUTING.md', 'contributing.md', { isEn: false, inDocsSubdir: false });

for (const topic of ['build', 'runner', 'lean-docs', 'forge', 'promotion']) {
  copyMapped(`docs/${topic}.md`, `docs/${topic}.md`, { isEn: false, inDocsSubdir: true });
}

// English counterparts
copyMapped('README.en.md', 'en/guide.md', { isEn: true, inDocsSubdir: false });
copyMapped('DESIGN.en.md', 'en/design.md', { isEn: true, inDocsSubdir: false });
copyMapped('CONTRIBUTING.en.md', 'en/contributing.md', { isEn: true, inDocsSubdir: false });

for (const topic of ['build', 'runner', 'lean-docs', 'forge', 'promotion']) {
  copyMapped(`docs/en/${topic}.md`, `en/docs/${topic}.md`, { isEn: true, inDocsSubdir: true });
}

console.log('Synced single-source Markdown files into site/src/ successfully.');
