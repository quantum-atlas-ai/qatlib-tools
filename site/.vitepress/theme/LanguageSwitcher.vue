<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { useData, withBase } from 'vitepress';
import { languagePaths } from '../routes.mjs';

const { site, page, localeIndex } = useData();
const english = computed(() => localeIndex.value === 'en');
const languages = [
  { tag: 'zh-Hans', name: '简体中文' },
  { tag: 'en', name: 'English' },
];
const routes = computed(() => {
  const path = page.value.isNotFound ? 'index.md' : page.value.relativePath;
  return languagePaths(path, site.value.cleanUrls);
});
const current = computed(() => english.value ? 'en' : 'zh-Hans');
const menu = ref(null);
const summary = ref(null);

function close(restoreFocus) {
  if (!menu.value?.open) return;
  menu.value.open = false;
  if (restoreFocus) summary.value.focus();
}

function closeOutside(event) {
  if (!menu.value?.contains(event.target)) close(false);
}

function closeAndRestoreFocus() {
  close(menu.value?.contains(document.activeElement));
}

function closeOnEscape(event) {
  if (!menu.value?.open) return;
  event.stopPropagation();
  close(true);
}

onMounted(() => document.addEventListener('click', closeOutside));
onUnmounted(() => document.removeEventListener('click', closeOutside));
</script>

<template>
  <details ref="menu" class="lang-switcher" @keydown.esc="closeOnEscape">
    <summary ref="summary" :aria-label="english ? 'Language: English' : '语言：中文'">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="1.6" aria-hidden="true">
        <circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M3 12h18"/>
      </svg>
      <span :lang="current">{{ english ? 'English' : '中文' }}</span>
      <span aria-hidden="true">▾</span>
    </summary>
    <div class="lang-options">
      <a v-for="{ tag, name } in languages" :key="tag" :href="withBase(`/${routes[tag]}`)"
        :lang="tag" :hreflang="tag" :aria-current="tag === current ? 'page' : undefined"
        @click="closeAndRestoreFocus">
        {{ name }}
        <span v-if="tag === current" aria-hidden="true">✓</span>
      </a>
    </div>
  </details>
</template>
