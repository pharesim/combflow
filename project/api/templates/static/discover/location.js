// ── Location Picker ──
let _leafletLoaded = false;
let _locationMap = null;
let _locationMarker = null;
let _selectedLocation = null; // {lat, lng}

function _loadLeaflet() {
  if (_leafletLoaded) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css';
    document.head.appendChild(link);
    const script = document.createElement('script');
    script.src = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js';
    script.onload = () => { _leafletLoaded = true; resolve(); };
    script.onerror = () => reject(new Error('Failed to load Leaflet'));
    document.head.appendChild(script);
  });
}

async function openLocationPicker() {
  const modal = document.getElementById('location-modal');
  Alpine.store('app').locationOpen = true;
  trapFocus(modal.querySelector('.modal'));
  try {
    await _loadLeaflet();
  } catch(e) {
    showToast('Could not load map library', 'error');
    closeLocationPicker();
    return;
  }
  if (!_locationMap) {
    _locationMap = L.map('location-map').setView([20, 0], 2);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '&copy; OpenStreetMap contributors',
      maxZoom: 19,
    }).addTo(_locationMap);
    _locationMap.on('click', function(e) {
      _placeMarker(e.latlng.lat, e.latlng.lng);
    });
  }
  setTimeout(() => _locationMap.invalidateSize(), 100);
  // Restore existing location if set
  if (_selectedLocation) {
    _placeMarker(_selectedLocation.lat, _selectedLocation.lng);
    document.getElementById('location-description').value = _selectedLocation.description || '';
  }
}

function _placeMarker(lat, lng) {
  if (_locationMarker) {
    _locationMarker.setLatLng([lat, lng]);
  } else {
    _locationMarker = L.marker([lat, lng]).addTo(_locationMap);
  }
  _selectedLocation = { lat, lng, description: (_selectedLocation && _selectedLocation.description) || '' };
  document.getElementById('location-coords').textContent = `${lat.toFixed(5)}, ${lng.toFixed(5)}`;
  document.getElementById('location-confirm-btn').disabled = false;
  _reverseGeocode(lat, lng);
}

let _locationAutoFilled = false;

function _reverseGeocode(lat, lng) {
  const descEl = document.getElementById('location-description');
  fetch(`https://nominatim.openstreetmap.org/reverse?lat=${lat}&lon=${lng}&format=json&zoom=14&accept-language=en`, {
    headers: { 'User-Agent': 'HoneyComb/1.0' }
  }).then(r => r.json()).then(data => {
    if (!data.address) return;
    const a = data.address;
    const parts = [a.city || a.town || a.village || a.hamlet || '', a.country || ''].filter(Boolean);
    const guess = parts.join(', ');
    if (guess && (!descEl.value.trim() || _locationAutoFilled)) {
      descEl.value = guess;
      _locationAutoFilled = true;
    }
  }).catch(() => {});
}

function useMyLocation() {
  if (!navigator.geolocation) {
    showToast('Geolocation not supported by your browser', 'error');
    return;
  }
  const btn = document.getElementById('location-myloc-btn');
  btn.disabled = true;
  btn.textContent = 'Locating...';
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      _placeMarker(pos.coords.latitude, pos.coords.longitude);
      _locationMap.setView([pos.coords.latitude, pos.coords.longitude], 14);
      btn.disabled = false;
      btn.innerHTML = '&#x1F4CD; My Location';
    },
    (err) => {
      if (err.code === 1) showToast('Location access denied', 'error');
      else showToast('Could not determine location', 'error');
      btn.disabled = false;
      btn.innerHTML = '&#x1F4CD; My Location';
    },
    { enableHighAccuracy: true, timeout: 10000 }
  );
}

function confirmLocation() {
  if (!_selectedLocation) return;
  _selectedLocation.description = document.getElementById('location-description').value.trim() || 'location';
  // Insert/replace worldmappin tag in body
  const bodyEl = document.getElementById('editor-body');
  const tag = `[//]:# (!worldmappin ${_selectedLocation.lat.toFixed(5)} lat ${_selectedLocation.lng.toFixed(5)} long ${_selectedLocation.description} d3scr)`;
  const wmRegex = /\[\/\/\]:#\s*\(!worldmappin\s+[\d.-]+\s+lat\s+[\d.-]+\s+long\s+.+?\s+d3scr\)/;
  if (wmRegex.test(bodyEl.value)) {
    bodyEl.value = bodyEl.value.replace(wmRegex, tag);
  } else {
    bodyEl.value = bodyEl.value.trimEnd() + '\n\n' + tag;
  }
  _updateLocationBadge();
  document.getElementById('editor-location-btn').classList.add('has-location');
  autoSaveDraft();
  closeLocationPicker();
}

function _updateLocationBadge() {
  const badge = document.getElementById('editor-location-badge');
  if (_selectedLocation) {
    badge.innerHTML = `&#x1F4CD; ${esc(_selectedLocation.description || 'Location set')} <span class="remove-location" onclick="event.stopPropagation();removeLocation()" title="Remove location">&times;</span>`;
    badge.style.display = '';
    badge.onclick = (e) => { if (!e.target.classList.contains('remove-location')) openLocationPicker(); };
  } else {
    badge.style.display = 'none';
  }
}

function removeLocation() {
  _selectedLocation = null;
  _locationAutoFilled = false;
  if (_locationMarker) {
    _locationMap.removeLayer(_locationMarker);
    _locationMarker = null;
  }
  document.getElementById('editor-location-btn').classList.remove('has-location');
  document.getElementById('location-coords').textContent = '';
  document.getElementById('location-confirm-btn').disabled = true;
  // Remove worldmappin tag from body
  const bodyEl = document.getElementById('editor-body');
  bodyEl.value = bodyEl.value.replace(/\n*\[\/\/\]:#\s*\(!worldmappin\s+[\d.-]+\s+lat\s+[\d.-]+\s+long\s+.+?\s+d3scr\)/, '');
  _updateLocationBadge();
  autoSaveDraft();
}

function closeLocationPicker() {
  const modal = document.getElementById('location-modal');
  releaseFocus(modal.querySelector('.modal'));
  Alpine.store('app').locationOpen = false;
}
