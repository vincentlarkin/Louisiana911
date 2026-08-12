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

The tracker does not collect typed text, URL query strings, email addresses, exact click coordinates, or incident IDs. Google Signals and ad-personalization signals are disabled.

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
