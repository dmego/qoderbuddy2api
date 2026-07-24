# Third-party notices

## Sub2API frontend UI sources

This frontend contains files derived from [Wei-Shaw/sub2api](https://github.com/Wei-Shaw/sub2api) at commit `cb24522`, imported on 2026-07-24.

The derived UI sources are provided under `LGPL-3.0-or-later`. The complete license text is available at [`licenses/sub2api-LGPL-3.0.txt`](licenses/sub2api-LGPL-3.0.txt).

| 2api target | Upstream source | Adaptation |
| --- | --- | --- |
| `tailwind.config.js` | `frontend/tailwind.config.js` | Kept UI color/token structure; removed unrelated business-only tokens. |
| `src/styles/tailwind.css` | `frontend/src/style.css` | Kept base, input, button, card and theme language. |
| `src/styles/sub2api-overrides.css` | `frontend/src/style.css` | Added 2api-specific page adaptation selectors. |
| `src/stores/ui.ts` | frontend layout theme behavior | Replaced upstream app/auth stores with local theme/navigation state. |
| `src/components/sub2api/layout/ThemeToggle.vue` | frontend layout theme controls | Replaced upstream icons, i18n and app store dependencies. |

No Sub2API names, marks, logos, screenshots, business APIs, payment features, user management, announcements or other brand assets are shipped in the 2api UI.
