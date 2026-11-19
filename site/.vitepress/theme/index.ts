import DefaultTheme from 'vitepress/theme';
import { h } from 'vue';
import './custom.css';

// Authentic Qatlib Z-QL03 vector wordmark paired with a restrained 'tools' badge,
// visually harmonizing qatlib-tools with https://quantum-atlas-ai.github.io/qatlib/
const QatlibWordmark = () =>
  h('span', { class: 'qatlib-brand-lockup', 'aria-label': 'qatlib-tools' }, [
    h(
      'svg',
      {
        class: 'qatlib-wordmark-svg',
        xmlns: 'http://www.w3.org/2000/svg',
        viewBox: '3 15 264 97',
        role: 'img',
        'aria-hidden': 'true',
      },
      [
        h('g', { id: 'prefix-qat', transform: 'translate(0,20) scale(0.8)' }, [
          h('g', { id: 'quantum-q' }, [
            h('g', { id: 'q-bowl', transform: 'translate(6,0)' }, [
              h('path', {
                d: 'M23 0 H41 C54 0 64 13 64 33 V70 C64 90 54 103 41 103 H23 C10 103 0 90 0 70 V33 C0 13 10 0 23 0 Z M23 4.2 H41 C51.2 4.2 58.6 15.2 58.6 33 V70 C58.6 87.8 51.2 98.8 41 98.8 H23 C12.8 98.8 5.4 87.8 5.4 70 V33 C5.4 15.2 12.8 4.2 23 4.2 Z',
                fill: 'currentColor',
                'fill-rule': 'evenodd',
              }),
            ]),
            h('path', {
              d: 'M40.37 76 C50.44 80 62 99 71 105 C74 107 77 107 80 105 L78 110 C73 112 69 109 65 104 C56 94 46.89 83 40.37 76 Z',
              fill: 'currentColor',
              'fill-rule': 'evenodd',
            }),
          ]),
          h('g', { id: 'operator-ligature' }, [
            h('path', {
              d: 'M78 0 H83.46 L111 85.96 L138.54 0 H144 L111 103 Z',
              fill: 'currentColor',
              'fill-rule': 'evenodd',
            }),
            h('path', { d: 'M173.9 4 L178.1 3 L177.68 37 H174.32 Z', fill: 'currentColor' }),
            h('path', { d: 'M176 -3 L180.6 2 L176 7 L171.4 2 Z', fill: 'currentColor' }),
            h('path', {
              d: 'M170.7 37 L181.3 37 L179.445 81 L176 108 L172.555 81 Z M175.35 46 V88 L176.65 83 V46 Z',
              fill: 'currentColor',
              'fill-rule': 'evenodd',
            }),
            h('path', {
              d: 'M89 33.9 C115 33.9 152 33.9 175 33.9 C184 33.9 192 33.7 197 32.6 L196 38.295 C190 37.7 182 38.1 175 38.1 C150 38.1 115 38.1 91 38.1 Z',
              fill: 'currentColor',
              'fill-rule': 'evenodd',
            }),
          ]),
          h('g', { id: 'qv-link' }, [
            h('path', { d: 'M68.5 33.9 H93 V38.1 H68.5 Z', fill: 'currentColor', 'fill-rule': 'evenodd' }),
          ]),
        ]),
        h('g', { id: 'suffix-lib', transform: 'translate(168.6,0) scale(.95,1)' }, [
          h('path', {
            d: 'M9.73 98.7C9.73 97.59 9.62 96.19 9.93 94.49C11.53 83.36 16.84 53.78 20.25 39.24C23.06 28.41 24.16 23 17.55 23C12.93 23 9.22 25.51 9.22 25.51L8.72 27.31C11.53 27.71 12.73 30.72 11.73 36.33C11.73 36.33 5.01 72.33 0.2 97.19L0 98.7ZM37.6 34.43C37.5 37.24 39.8 39.84 42.71 39.84C45.52 39.84 47.92 37.24 48.02 34.43C48.02 31.62 45.72 29.22 42.91 29.22C40 29.22 37.6 31.62 37.6 34.43ZM27.57 98.7H37.3C37.4 98.1 37.4 97.49 37.4 96.89C37.4 95.19 37.6 93.28 38 91.18C39.8 80.55 41.91 67.32 42.71 63.2C43.01 61.5 44.92 54.88 44.92 52.78C45.02 50.17 43.91 48.07 41.21 48.07C37.2 48.07 32.68 50.67 30.58 53.18L31.48 54.78C31.78 54.68 31.98 54.68 32.28 54.68C33.79 54.68 35.09 55.79 34.99 58.29C34.99 58.79 34.99 59.9 27.67 98.3ZM66.97 36.33C66.47 42.45 57.15 86.06 57.15 91.08C57.15 97.19 63.67 100 71.89 100C90.43 100 99.96 79.95 99.96 64.81C99.96 53.18 96.45 47.66 89.93 47.66C83.62 47.66 75.7 52.78 70.48 60.8C73.69 49.67 77.7 32.73 77.8 27.81C77.8 24.6 76.5 23 72.79 23C68.18 23 64.47 25.51 64.47 25.51L64.07 27.31C66.57 27.41 67.17 30.22 67.17 33.03C67.17 34.23 67.07 35.33 66.97 36.33ZM67.17 83.76C68.08 77.44 68.08 76.54 70.18 64.71C74.39 59.29 80.91 54.38 84.72 54.38C89.23 54.38 90.74 59.49 90.74 66.01C90.74 77.94 86.52 94.49 73.99 94.49C68.18 94.49 66.87 91.98 66.87 88.17C66.87 86.87 66.97 85.36 67.17 83.76Z',
            fill: 'currentColor',
            stroke: 'var(--wordmark-cutout)',
            'stroke-width': '1.5',
            'stroke-linejoin': 'round',
          }),
        ]),
      ]
    ),
    h('span', { class: 'qatlib-tools-pill' }, 'tools'),
  ]);

export default {
  extends: DefaultTheme,
  Layout() {
    return h(DefaultTheme.Layout, null, {
      'nav-bar-title-before': () => QatlibWordmark(),
    });
  },
};
