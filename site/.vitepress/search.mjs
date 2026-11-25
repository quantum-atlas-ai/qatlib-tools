// VitePress 1.6 excludes _render from the client config. Its default
// MiniSearch tokenizer and prefix matching can query these Han suffixes
// without deserializing a tokenizer function in the browser.
const hanRun = /\p{Script=Han}{2,}/gu;
const htmlParts = /(<h[1-6]\b[^>]*>.*?<\/h[1-6]>)|<[^>]*>|[^<]+/gs;
const maxTermLength = 12;

function hanSuffixes(text) {
  const terms = new Set();
  for (const run of text.matchAll(hanRun)) {
    const characters = Array.from(run[0]);
    for (let i = 0; i < characters.length - 1; i++) {
      terms.add(characters.slice(i, i + maxTermLength).join(''));
    }
  }
  return [...terms].join(' ');
}

export function renderSearchIndex(source, env, markdown) {
  const html = markdown.render(source, env);
  if (env.frontmatter?.search === false) return '';
  if (env.relativePath.startsWith('en/')) return html;

  // Keep result titles intact and add their terms to the section body.
  return html.replace(htmlParts, (part, title) => {
    if (title) {
      const terms = hanSuffixes(title.replace(/<[^>]*>/g, ''));
      return `${title} <span>${terms}</span> `;
    }
    if (part.startsWith('<')) return part;
    const terms = hanSuffixes(part);
    return terms ? `${part} ${terms} ` : part;
  });
}
