# Behavior

## Map markers

Incidents use a single transparent triangle by default. Settings can switch to circles; existing saved choices are preserved. Marker shape, dark/light appearance, map/list view, and the History map setting persist in localStorage on that browser and site. Live incidents with 5 or more units pulse with a matching triangle or circle outline; the pulse stops below 5 units or in History. Reduced-motion preferences keep the outline static. Larger markers indicate less precise public locations, with dashed outlines for approximate placement. Markers are visual indicators, not exact location boundaries. Both dark and light street maps show road names and intersections at neighborhood zoom levels.

If a location cannot be validated, the incident remains available in the list without a map marker.

## Colors

- Red: higher severity
- Yellow: medium severity
- Blue: lower severity or public service
- Striped purple: medical or EMS

The same color language is used in the list and on the map. These categories are Louisiana911 display labels, not official agency classifications.

## Live and historical views

Latest shows currently published information. History shows available records for a selected date. Source and severity filters apply to both views.

All feeds keep the source's reported Central clock time and show a date, for example `Sep-5 23:40`. Caddo and Baton Rouge do not publish a call date, so the date is estimated as the latest occurrence of that clock time before the collector first observed the call. The date stays anchored to that observation across midnight; opening the page later does not shift it. Calls already present after an outage may be older than this estimate. Their History calendar continues to group records by first-observed date, so a call first collected just after midnight can show the previous day's reported time.

Lafayette imports retain the full published date and time, including calls that have lasted multiple days; History groups these by the published date. Older Lafayette records collected before this change use an estimate from their stored observation time. New Orleans and Lake Charles keep their published dates and their existing daily/delayed status rules.

When a call disappears from a successfully parsed Caddo, Baton Rouge, or Lafayette snapshot, it leaves Latest and its live map and becomes available in History. A validated empty table clears that source's remaining live calls too. Failed requests, malformed rows, or unrecognized pages preserve the previous snapshot until collection succeeds. Removal does not establish an official outcome; saved history remains available. One source's snapshot cannot clear another source's calls.

## Location quality

Published coordinates and road information are checked before display. Approximate placements are labeled, and an unresolved location is not replaced with a guessed city-center point.
