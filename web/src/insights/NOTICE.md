# dsh-context attribution — `web/src/insights/`

This module adapts parts of **dsh-context** (`deepseek-ai/dsh-context`), read at
commit `1bc481c`. Upstream is licensed under the **Apache License 2.0**; the full
license text is in `LICENSE` next to this file.

The adapted files keep an upstream file reference in their header. No upstream
runtime dependency is added: the port replaces dsh's ViewKit / Tailwind / host
bridge with this repo's React components, CSS tokens, and `lib/api.ts` contract.

## File ledger

| Local file | Upstream file @ `1bc481c` | Nature |
| --- | --- | --- |
| `components/Donut.tsx` | `src/client/components/donut.tsx` | ADAPT — dasharray ring geometry, half-gap between adjacent segments, full ring for a single segment; ViewKit/Tailwind → token CSS, upstream hover/center API reshaped for this repo |
| `components/Heatmap.tsx` | `src/client/components/heatmap.tsx` | ADAPT — Sunday-first 8-week columns, four depth levels, click-a-day filtering; date-window computation moved to `lib/insightsModel.ts`, class names → `.ctx-insights-*` |
| `components/CompositionBar.tsx` | `src/client/components/stackedBar.tsx` | ADAPT — stacked segments with legend hover linking; dsh headroom/reserved-output segments removed (this repo has no such fields), 70/85% ratio threshold lines added |
| `components/TrendChart.tsx` | `src/client/components/trendChart.tsx` | ADAPT — one-bar-per-request, hover brief, click-to-pin, event markers, horizontal scroll-to-newest; dsh's seven-composition DNA, turn bands, and adaptive window removed (backend already folds by `granularity`/`mode`) |
| `InsightsPage.tsx` | `src/client/components/overviewPanel.tsx` | ADAPT — page information architecture (KPI band, daily chart, heatmap, composition, session list); rendering is new code against this repo's overview DTO |
| `SessionInsightsPage.tsx` | `src/client/components/contextView.tsx` | ADAPT — context/Fleet sub-page split and the composition → trend → events order; layout and data flow are new code |
| `components/FleetView.tsx` | `src/client/components/fleetView.tsx` | BUILD — upstream Fleet is a host-coupled network/task board; this repo only has self/ancestors/children cards, so the graph is not ported |
| `components/DailyChart.tsx` | — | BUILD |
| `components/SessionCard.tsx` | — | BUILD |
| `components/ContextComposition.tsx` | — | BUILD (renders this repo's `ContextUsage`) |
| `components/ContextEvents.tsx` | — | BUILD |
| `lib/insightsModel.ts` | `src/client/components/trendChart.tsx` (marker attachment), `heatmap.tsx` (depth levels) | BUILD, with the two named algorithms adapted |
| `lib/useContextSizes.ts` | — | BUILD |
| `insights.css` | — | BUILD |

## Modifications

- All color, spacing, radius, and type values come from `web/src/index.css`
  tokens; no new palette and no color literals (stylelint #1364).
- Cost / dollar rendering is intentionally absent (ticket #1420 decision).
- API access goes through `web/src/lib/api.ts`; components never fetch directly.
- Error and empty states are this repo's own (page shell survives a failed
  request; `usage_only` / `no_data` are rendered as distinct facts).
