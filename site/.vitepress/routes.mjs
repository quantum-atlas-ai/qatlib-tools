export function pagePath(page, cleanUrls) {
  return page.replace(/(^|\/)index\.md$/, '$1')
    .replace(/\.md$/, cleanUrls ? '' : '.html');
}

export function languagePaths(page, cleanUrls) {
  const chinese = pagePath(page.replace(/^en\//, ''), cleanUrls);
  return { 'zh-Hans': chinese, en: `en/${chinese}` };
}
