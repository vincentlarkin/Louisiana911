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

## Location quality

Published coordinates and road information are checked before display. Approximate placements are labeled, and an unresolved location is not replaced with a guessed city-center point.
