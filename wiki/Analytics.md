# Analytics

Louisiana911 uses one deferred, dependency-free GA4 tracker at `public/analytics.js`. The Google tag is loaded asynchronously after the page has parsed, and the tracker uses browser-native observers and passive listeners to keep main-thread work small.

## Events

| Event | What it measures |
| --- | --- |
| `ui_click` | Links, buttons, map/marker clicks, incident cards, and other page clicks, including page region and coarse 10% position buckets |
| `control_change` | Select, checkbox, and radio changes; free-form field contents are not collected |
| `scroll_depth` | First reach of 10%, 25%, 50%, 75%, 90%, and 100% of a page |
| `container_scroll_depth` | The same milestones inside the live/history incident lists and scrollable dialogs |
| `section_view` | A section becoming at least 50% visible for the first time |
| `engagement_milestone` | 15, 30, 60, 120, 300, and 600 seconds while the page is visible |
| `page_summary` | Active time, elapsed time, maximum scroll depth, clicks, and sections viewed when a visit ends or is backgrounded |
| `page_performance` | TTFB, DOM-ready, load, first-contentful-paint, transfer size, and navigation type |
| `web_vital` | LCP, CLS, and INP values and their good/needs-improvement/poor rating |
| `client_error` | JavaScript error type, error class, source filename, and line; error messages are intentionally omitted |
| `source_select` | A source tab is selected; `source_view` identifies the feed |
| `view_mode_select` | Map/list button selection, with `view_mode` |
| `incident_tab_select` | Live/history tab selection, with `view_mode` |
| `incident_filter` | Agency, urgency, and unit filter clicks, with `control_name` and `control_value` |
| `basemap_select` | A predefined basemap option is selected |
| `history_view` | History successfully loads and is displayed, with `source_view` and `result_count` |
| `history_load_error` | The current History request fails; no request URL or error message |
| `share` | Native sharing completes or a link is successfully copied, with `method` and `source_view`; cancellation/failure is not counted |
| `monthly_report_view` | A report is displayed from the network or browser cache, with `source_view`, `report_month`, and `view_mode` (`page` or `dialog`) |
| `report_load_error` | Monthly report loading fails |
| `engaged_visit` | GA4-admin derived event: `engagement_milestone` where `active_seconds` equals `60`; copy source parameters |

The built-in event handlers omit typed text, arbitrary URL query strings, exact click coordinates, and incident IDs. Shared incident page titles and paths are generalized in the Google tag configuration. Standard UTM and Google ad-click parameters remain in the page URL for campaign attribution; never put personal information in campaign labels. Google Signals and ad-personalization signals are disabled. Feature hooks must continue to pass only the documented aggregate parameters. Localhost and preview hosts retain a testable `dataLayer` without loading Google's tag; only the production apex and www hosts load it.

Every custom event includes the current `source_view`, rather than only the initial URL source. Non-scrollable pages do not emit scroll-depth milestones. Click-based feature events measure selections; `share`, `history_view`, and `monthly_report_view` measure successful outcomes.

## GA4 setup

Events appear in Realtime automatically after deployment. For reusable reports and Explorations, create event-scoped custom dimensions in **Admin → Data display → Custom definitions**. The most useful event parameters are:

- `page_type`, `source_view`
- `action_type`, `element_name`, `page_region`, `destination`, `link_domain`, `pointer_type`
- `x_viewport_bucket`, `y_viewport_bucket`, `y_page_bucket`
- `section_name`
- `scroll_container`
- `control_name`, `control_type`, `control_state`, `control_value`
- `metric_name`, `metric_rating`

Create custom metrics for the numeric parameters you want to chart, especially `percent_scrolled`, `active_seconds`, `max_scroll_percent`, `click_count`, `metric_value`, `ttfb_ms`, `fcp_ms`, and `load_ms`.

To approximate a click heatmap in an Exploration, filter to `ui_click`, use `x_viewport_bucket` as columns and `y_page_bucket` (whole-page position) or `y_viewport_bucket` (screen position) as rows, and use Event count as the value. Break down or filter by `page_type` and `page_region` so layouts are not mixed together.

Custom definitions begin populating only after they are created; they are not retroactive. DebugView can be used to verify raw event parameters during a test visit.

Ten event-scoped dimensions were configured in property `546098485` (`louisiana911`, stream `G-WHHVE8V5DW`) on September 5, 2026 Central time: Page type (`page_type`), Source view (`source_view`), Action type (`action_type`), Element name (`element_name`), Page region (`page_region`), View mode (`view_mode`), Control name (`control_name`), Control value (`control_value`), Share method (`method`), and Report month (`report_month`). Feature events sent by code do not also need a GA4 Create event rule; that would duplicate them. The derived `engaged_visit` rule is configured in GA4 only.

`engaged_visit` was saved as a key event, counted once per session with no default monetary value. Existing key-event choices were retained. Search Console already lists the canonical sitemap as Success with nine discovered pages (last read August 28, 2026). Deploy the website changes before expecting the new feature events; GA4 reporting definitions alone do not send them.

For a useful Exploration, put Event name in rows, Source view in columns, and Event count and Total users in values. Add Page type, View mode, or Share method as a breakdown. Allow 24–48 hours for newly registered dimensions to appear in standard reports.

References: [Google event setup](https://developers.google.com/analytics/devguides/collection/ga4/events), [custom definitions](https://support.google.com/analytics/answer/14240153).
