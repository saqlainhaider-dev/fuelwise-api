// Renders the interactive trip map for a saved Fuelwise trip.
// Trip data is read from the #trip-data JSON block, so the page can ship a
// strict script-src 'self' CSP with no inline script.
(function () {
  const trip = JSON.parse(document.getElementById('trip-data').textContent);
  const map = L.map('map', { zoomControl: true });

  // CARTO serves public raster basemaps for client-side apps without a key;
  // OSM's volunteer servers block unidentified or VPN-adjacent traffic, so
  // they are kept only as an automatic fallback when CARTO tiles fail.
  const attribution =
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> ' +
    'contributors &copy; <a href="https://carto.com/attributions">CARTO</a> | ' +
    'Route: HERE | Stops: supplied OPIS price list';
  const sources = [
    'https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png',
    'https://tile.openstreetmap.org/{z}/{x}/{y}.png'
  ];
  let tiles = L.tileLayer(sources[0], { maxZoom: 19, attribution, subdomains: 'abcd' }).addTo(map);
  let failed = false;
  tiles.on('tileerror', function () {
    if (failed) return;
    failed = true;
    map.removeLayer(tiles);
    tiles = L.tileLayer(sources[1], { maxZoom: 19, attribution }).addTo(map);
  });

  const points = [trip.start].concat(
    (trip.stops || []).map(function (s) { return s.access_location; }),
    [trip.finish]
  );
  const latlng = function (p) { return [p.lat, p.lng]; };

  // One polyline per leg so hover shows real road distance and fuel use.
  const bounds = [];
  (trip.legs || []).forEach(function (leg) {
    const a = points[leg.from_index], b = points[leg.to_index];
    const line = L.polyline([latlng(a), latlng(b)], { color: '#176341', weight: 5 }).addTo(map);
    line.bindTooltip(
      leg.distance_miles.toLocaleString() + ' mi · ' +
        leg.fuel_used_gallons.toFixed(2) + ' gal used',
      { sticky: true }
    );
    bounds.push(latlng(a), latlng(b));
  });

  const icon = function (label, color) {
    return L.divIcon({
      className: '',
      html:
        '<div style="background:' + color + ';color:#fff;border:2px solid #fff;' +
        'border-radius:50%;width:26px;height:26px;line-height:22px;text-align:center;' +
        'font-weight:700;box-shadow:0 1px 4px rgba(0,0,0,.5)">' + label + '</div>',
      iconSize: [26, 26],
      iconAnchor: [13, 13]
    });
  };

  L.marker(latlng(trip.start), { icon: icon('S', '#2f8f4e') })
    .bindPopup('<b>Start</b><br>' + trip.start.label).addTo(map);
  L.marker(latlng(trip.finish), { icon: icon('F', '#c0392b') })
    .bindPopup('<b>Finish</b><br>' + trip.finish.label).addTo(map);

  (trip.stops || []).forEach(function (stop, i) {
    L.marker(latlng(stop.access_location), { icon: icon(String(i + 1), '#b85b22') })
      .bindPopup(
        '<b>' + (i + 1) + '. ' + stop.name + '</b><br>' +
        stop.address + '<br>' + stop.city + ', ' + stop.state + '<br>' +
        '<b>$' + stop.price_per_gallon + '</b>/gal · bought ' +
        stop.gallons_purchased.toFixed(2) + ' gal for $' + stop.cost +
        '<br>arrived with ' + stop.arrival_fuel_gallons.toFixed(2) + ' gal'
      )
      .addTo(map);
  });

  if (bounds.length) map.fitBounds(bounds, { padding: [40, 40] });

  document.getElementById('summary').textContent =
    trip.distance_miles.toLocaleString() + ' miles · $' + trip.total_fuel_cost +
    ' fuel purchased en route · ' + (trip.stops || []).length + ' stop(s)';
})();
