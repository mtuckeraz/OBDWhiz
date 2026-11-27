'use strict';

(() => {
  const detail = $('#dossier-detail');
  const results = $('#dossier-results');
  const status = $('#dossier-status');
  const search = $('#dossier-search');
  const staffPanel = $('#dossier-admin');
  const gpsPolicy = $('#dossier-gps-policy');
  let vehicle = null;
  let user = null;
  let policy = null;
  let comparison = new Set();

  const fields = [
    ['vin', 'VIN'], ['make', 'Make'], ['model', 'Model'], ['year', 'Year'],
    ['trim', 'Trim'], ['engine', 'Engine'], ['color', 'Exterior color'],
    ['plate', 'License plate'], ['jurisdiction', 'Registration jurisdiction'], ['owner', 'Current owner']
  ];
  const mediaUrl = (id, variant) => '/api/media/' + encodeURIComponent(id) + '/' + variant;
  const title = v => [v.year, v.make, v.model].filter(Boolean).join(' ') || 'Vehicle dossier';
  const report = message => { status.textContent = message; };
  const handle = error => { report(error.message); toast(error.message, true); };
  const options = (items, label) =>
    '<option value="">' + E(label) + '</option>' + items.map(item =>
      '<option value="' + E(item.id ?? item) + '">' + E(item.label ?? item) + '</option>').join('');

  async function loadStaff() {
    const [staff, storage] = await Promise.all([api('/staff'), api('/staff/storage')]);
    $('#dossier-users').textContent = staff.map(person => person.name + ' (' + person.username + ', ' + person.role + ')').join(' · ');
    $('#dossier-storage').textContent = storage.files + ' originals · ' +
      (storage.originalBytes / 1048576).toFixed(1) + ' MiB stored · ' +
      (storage.freeBytes / 1073741824).toFixed(1) + ' GiB free';
  }

  async function loadStaffPolicy() {
    policy = await api('/dossiers/policy');
    gpsPolicy.checked = policy.flagMissingGps;
    gpsPolicy.disabled = false;
  }

  async function find() {
    const session = await api('/session');
    user = session.user;
    if (!user || user.mustChangePassword) {
      showLogin(Boolean(user?.mustChangePassword), user?.username || '');
      return;
    }
    const query = new FormData(search).get('q').trim();
    const vehicles = await api('/dossiers?q=' + encodeURIComponent(query));
    results.innerHTML = vehicles.map(v => `
      <button class="dossier-result" data-dossier="${E(v.id)}">
        ${v.thumbnail ? `<img loading="lazy" src="${mediaUrl(v.thumbnail, 'thumb.jpg')}" alt="">` : ''}
        <strong>${E(title(v))}</strong>
        <span>${E([v.plate, v.vin, v.owner, v.color].filter(Boolean).join(' · '))}</span>
      </button>`).join('');
    report(vehicles.length === 100 ? 'Showing 100 matches. Narrow your search to see more.' :
      vehicles.length ? vehicles.length + ' vehicle records. Select a vehicle to add photos.' :
        query ? 'No matching vehicle records found.' : 'No vehicle records yet. Add a vehicle to upload photos.');
  }

  const editForm = (v = {}) => `
    <form id="dossier-edit">
      <h2>${v.id ? 'Edit vehicle' : 'Add vehicle'}</h2>
      <div class="dossier-grid">${fields.map(([field, label]) => `
        <label>${label}<input name="${field}" value="${E(v[field] ?? '')}" ${field === 'year'
          ? 'type="number" min="1886" max="2100"' : 'maxlength="' + (field === 'vin' ? 17 : 120) + '"'}></label>`).join('')}
        <label>Garaging Location<input name="garagingLocation" maxlength="250" value="${E(v.garagingLocation ?? '')}"></label>
      </div>
      <label>Notes<textarea name="profileNotes" rows="4" maxlength="4000">${E(v.profileNotes ?? '')}</textarea></label>
      <div class="actions spaced"><button class="button primary">Save vehicle</button>
      <button class="button secondary" type="button" id="dossier-cancel">Cancel</button></div>
    </form>`;

  function editor(value) {
    detail.hidden = false;
    detail.innerHTML = editForm(value);
    detail.scrollIntoView({block:'start'});
    $('#dossier-cancel').onclick = () => value?.id ? render(value) : (detail.hidden = true);
    $('#dossier-edit').onsubmit = async event => {
      event.preventDefault();
      const form = event.currentTarget;
      const data = Object.fromEntries(new FormData(form));
      data.year = data.year ? Number(data.year) : null;
      const button = form.querySelector('button');
      button.disabled = true;
      try {
        const saved = await api('/dossiers' + (value?.id ? '/' + value.id : ''), data);
        await find();
        await open(saved.id);
        toast('Vehicle saved.');
      } catch (error) { handle(error); } finally { button.disabled = false; }
    };
  }

  function locationText(value) {
    return value ? value.latitude.toFixed(6) + ', ' + value.longitude.toFixed(6) +
      (value.accuracyMeters == null ? '' : ' (±' + Math.round(value.accuracyMeters) + ' m)') : '';
  }

  function photoCard(item, v) {
    const visit = v.visits.find(entry => entry.id === item.visitId);
    const photo = item.kind === 'photo';
    return `<article class="dossier-media">
      ${photo ? `<a href="${mediaUrl(item.id, 'preview.jpg')}" target="_blank" rel="noopener">
        <img loading="lazy" src="${mediaUrl(item.id, 'thumb.jpg')}" alt="${E(item.caption || item.filename)}"></a>` : ''}
      <strong>${E(item.filename)}</strong>
      ${item.caption ? `<p>${E(item.caption)}</p>` : ''}
      <p class="muted small">${E([item.category, visit?.label].filter(Boolean).join(' · '))}</p>
      ${photo ? `<p class="small">Captured ${item.capturedAt ? E(item.capturedAt) : 'time unavailable'}
        ${item.capturedAt && item.captureTimezone !== 'exif_offset' ? ' · timezone unknown' : ''}</p>` : ''}
      <p class="small">Uploaded ${E(fleetDate(item.uploadedAt))} by ${E(item.uploaderName || item.uploader)}</p>
      ${item.captureLocation ? `<p class="small">EXIF capture location · ${E(locationText(item.captureLocation))}</p>` : ''}
      ${item.captureGpsMissing ? '<p class="small">Original capture GPS missing</p>' : ''}
      ${item.uploadLocation ? `<p class="small">Browser upload location · ${E(locationText(item.uploadLocation))}</p>` : ''}
      ${item.manualLocation ? `<p class="small">Manual location (unverified) · ${E(item.manualLocation)}</p>` : ''}
      ${item.duplicate ? '<p class="small">Matches another original by SHA-256</p>' : ''}
      <a href="${mediaUrl(item.id, 'original')}">Download original</a>
      ${photo ? `<label class="check"><input type="checkbox" data-compare="${E(item.id)}" ${comparison.has(item.id) ? 'checked' : ''}> Compare</label>` : ''}
      ${user.role === 'admin' || item.uploader === user.id ? `<button type="button" class="text-button" data-revise="${E(item.id)}">Edit description</button>` : ''}
    </article>`;
  }

  function render(v) {
    detail.hidden = false;
    detail.innerHTML = `
      <div class="panel-heading"><div><h2>${E(title(v))}</h2><p>${E(v.vin || 'Draft · VIN unavailable')}</p>
      <p class="muted">${E([v.owner, v.plate, v.jurisdiction, v.color, v.trim, v.engine].filter(Boolean).join(' · '))}</p></div>
      <div class="actions">
        <button type="button" id="dossier-add-photos" class="button primary">Add photos</button>
        ${user.role === 'admin' ? '<button type="button" id="dossier-edit-button" class="button secondary">Edit vehicle</button><button type="button" id="dossier-delete-button" class="button secondary danger">Delete vehicle</button>' : ''}
      </div></div>
      ${v.garagingLocation ? `<p><strong>Garaging Location</strong> · ${E(v.garagingLocation)}</p>` : ''}
      ${v.profileNotes ? `<h3>Notes</h3><p class="dossier-profile-notes">${E(v.profileNotes)}</p>` : ''}
      <details open><summary>Diagnostics</summary><div data-dossier-diagnostics="${E(v.id)}"></div></details>
      <h3>Add photos or documents</h3>
      <form id="dossier-upload">
        <div class="dossier-grid">
          <label>Choose photos<input name="photos" type="file" accept="image/jpeg,image/png,image/heic,image/heif,.heic,.heif" multiple></label>
          <label>Take a photo<input name="camera" type="file" accept="image/*" capture="environment"></label>
          <label>Documents<input name="documents" type="file" multiple></label>
          <label>Category<select name="category">${options(policy.categories, 'Optional')}</select></label>
          <label>Documentation visit<select name="visitId">${options(v.visits, 'No visit')}</select></label>
          <label>Caption<input name="caption" maxlength="1000"></label>
          <label>Manual location (optional, unverified)<input name="manualLocation" maxlength="250"></label>
        </div>
        <label class="check"><input type="checkbox" name="location"> Request current upload location for photos</label>
        <p class="muted small">${window.isSecureContext ? 'Upload location can document where you are now when a photo lacks capture GPS.' :
          'Browser location requires HTTPS on the shop network. You can enter a manual location.'}</p>
        <button class="button primary">Upload selected files</button>
        <p id="dossier-upload-progress" role="status"></p>
      </form>
      <h3>Photo and document timeline</h3>
      <div class="dossier-grid">
        <label>Category filter<select id="dossier-category">${options(policy.categories, 'All categories')}</select></label>
        <label>Visit filter<select id="dossier-visit">${options(v.visits, 'All visits')}</select></label>
        <label>Filter by date, uploader or location<input id="dossier-media-search" type="search"></label>
      </div>
      <div id="dossier-comparison" class="dossier-grid"></div>
      <div id="dossier-media" class="dossier-grid"></div>
      <details><summary>Notes and documentation visits</summary>
        <form data-entry="note"><label>Note<textarea name="value" required maxlength="4000"></textarea></label><button class="button secondary">Add note</button></form>
        <form data-entry="visit"><label>Visit label<input name="value" required maxlength="120" placeholder="Example: arrival photos"></label><button class="button secondary">Add documentation visit</button></form>
        ${v.notes.map(note => `<p>${E(note.body)} <small>${E(fleetDate(note.createdAt))} · ${E(note.authorName || note.author)}</small></p>`).join('')}
      </details>
      <details><summary>Ownership history</summary>
        ${v.owners.map(owner => `<p>${E(owner.name)} · ${E(fleetDate(owner.startedAt))} to ${owner.endedAt ? E(fleetDate(owner.endedAt)) : 'present'}</p>`).join('')}
      </details>`;
    $('#dossier-edit-button')?.addEventListener('click', () => editor(v));
    $('#dossier-delete-button')?.addEventListener('click', event => {
      const name = [title(v), v.vin || v.plate].filter(Boolean).join(' · ');
      if (!window.confirm(`Delete ${name}? The vehicle record, notes, photos and documents will be permanently deleted. Saved scans will remain in Overview.`)) return;
      action(event.currentTarget, async () => {
        const result = await deleteRecord('/dossiers/' + encodeURIComponent(v.id));
        if (state.selected?.vehicleId === v.id) {
          delete state.selected.vehicleId;
          renderScan();
        }
        vehicle = null;
        comparison.clear();
        detail.replaceChildren();
        detail.hidden = true;
        view('dossiers');
        toast(result.warning || 'Vehicle deleted.', Boolean(result.warning));
      });
    });
    $('#dossier-add-photos').addEventListener('click', () => {
      const form = $('#dossier-upload');
      form.scrollIntoView({block:'start'});
      form.elements.namedItem('photos').focus({preventScroll:true});
    });
    $('#dossier-upload').onsubmit = upload;
    for (const input of detail.querySelectorAll('#dossier-category,#dossier-visit,#dossier-media-search')) {
      input.oninput = renderMedia;
    }
    for (const form of detail.querySelectorAll('[data-entry]')) {
      form.onsubmit = async event => {
        event.preventDefault();
        try {
          await api('/dossiers/' + v.id + '/' + form.dataset.entry, {value:new FormData(form).get('value')});
          await open(v.id, false);
        } catch (error) { handle(error); }
      };
    }
    renderMedia();
    document.dispatchEvent(new Event('obd-dossier-rendered'));
  }

  function renderMedia() {
    const category = $('#dossier-category').value;
    const visit = $('#dossier-visit').value;
    const query = $('#dossier-media-search').value.toLowerCase();
    const filtered = vehicle.media.filter(m => (!category || m.category === category) &&
      (!visit || m.visitId === visit) && [m.capturedAt, m.uploadedAt, m.uploaderName, m.uploader, m.manualLocation,
        locationText(m.captureLocation), locationText(m.uploadLocation)].join(' ').toLowerCase().includes(query));
    $('#dossier-media').innerHTML = filtered.map(item => photoCard(item, vehicle)).join('') ||
      '<p class="muted">No matching photos or documents.</p>';
    $('#dossier-comparison').innerHTML = vehicle.media.filter(m => comparison.has(m.id)).map(m =>
      `<figure><img src="${mediaUrl(m.id, 'preview.jpg')}" alt="${E(m.caption || m.filename)}"><figcaption>${E(m.filename)} · ${E(m.capturedAt || m.uploadedAt)}</figcaption></figure>`).join('');
  }

  async function open(id, scroll = true) {
    if (!policy) policy = await api('/dossiers/policy');
    if (vehicle?.id !== id) comparison = new Set();
    vehicle = await api('/dossiers/' + id);
    render(vehicle);
    if (scroll) detail.scrollIntoView({block:'start'});
  }

  function position() {
    return new Promise(resolve => {
      if (!window.isSecureContext || !navigator.geolocation) return resolve(null);
      navigator.geolocation.getCurrentPosition(({coords}) => resolve({
        latitude:coords.latitude, longitude:coords.longitude, accuracy:coords.accuracy
      }), () => resolve(null), {timeout:10000, maximumAge:0});
    });
  }

  async function upload(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const progress = $('#dossier-upload-progress');
    const data = new FormData(form);
    const files = ['photos', 'camera', 'documents'].flatMap(key =>
      Array.from(form.elements.namedItem(key).files, file => ({file, kind:key === 'documents' ? 'document' : 'photo'})));
    if (!files.length) {
      progress.textContent = 'Choose at least one file.';
      return;
    }
    const id = vehicle.id;
    const button = form.querySelector('button');
    button.disabled = true;
    const messages = [];
    const failed = [];
    let saved = 0;
    try {
      const uploadLocation = data.has('location') && files.some(item => item.kind === 'photo') ? await position() : null;
      if (data.has('location') && !uploadLocation) messages.push('Current location was unavailable.');
      for (const [index, {file, kind}] of files.entries()) {
        progress.textContent = 'Uploading ' + (index + 1) + ' of ' + files.length + ' · ' + file.name;
        const metadata = {name:file.name, kind, category:data.get('category'), visitId:data.get('visitId'),
          caption:data.get('caption'), manualLocation:data.get('manualLocation'),
          uploadLocation:kind === 'photo' ? uploadLocation : null};
        try {
          const response = await fetch('/api/dossiers/' + id + '/media', {method:'POST', body:file, headers:{
            'X-Apex-Client':'1', 'X-Upload-Metadata':encodeURIComponent(JSON.stringify(metadata))
          }});
          const result = await response.json();
          if (!response.ok) throw new Error(result.error || 'Upload failed');
          saved++;
          if (result.captureGpsMissing && !result.uploadLocation && !result.manualLocation) {
            messages.push(file.name + ' lacks capture GPS. Use Edit description to add an unverified location.');
          }
        } catch (error) {
          failed.push(file.name + ' · ' + error.message);
        }
      }
      await open(id, false);
      $('#dossier-upload-progress').textContent = [saved + ' of ' + files.length + ' saved.',
        ...messages, ...failed, failed.length ? 'Select failed files again to retry.' : ''].filter(Boolean).join('\n');
    } catch (error) { handle(error); } finally { button.disabled = false; }
  }

  search.onsubmit = event => { event.preventDefault(); find().catch(handle); };
  $('#dossier-new').onclick = () => editor();
  results.onclick = event => {
    const button = event.target.closest('[data-dossier]');
    if (button) open(button.dataset.dossier).catch(handle);
  };
  detail.onchange = event => {
    const id = event.target.dataset.compare;
    if (!id) return;
    if (event.target.checked) {
      if (comparison.size === 2) comparison.delete(comparison.values().next().value);
      comparison.add(id);
    } else comparison.delete(id);
    renderMedia();
  };
  detail.onclick = event => {
    const revision = event.target.closest('[data-revise]');
    if (!revision) return;
    const item = vehicle.media.find(m => m.id === revision.dataset.revise);
    const card = revision.closest('article');
    const controls = document.createElement('form');
    controls.innerHTML = `<label>Caption<input name="caption" maxlength="1000" value="${E(item.caption)}"></label>
      <label>Category<select name="category">${options(policy.categories, 'Optional')}</select></label>
      <label>Documentation visit<select name="visitId">${options(vehicle.visits, 'No visit')}</select></label>
      <label>Manual location (unverified)<input name="manualLocation" maxlength="250" value="${E(item.manualLocation)}"></label>
      <button class="button secondary">Save description</button>`;
    controls.elements.category.value = item.category;
    controls.elements.visitId.value = item.visitId || '';
    revision.replaceWith(controls);
    controls.onsubmit = async e => {
      e.preventDefault();
      try {
        await api('/media/' + item.id + '/metadata', Object.fromEntries(new FormData(controls)));
        await open(vehicle.id, false);
      } catch (error) { handle(error); }
    };
    card.scrollIntoView({block:'nearest'});
  };
  $('#dossier-user-form').onsubmit = async event => {
    event.preventDefault();
    const form = event.currentTarget;
    try {
      await api('/staff', Object.fromEntries(new FormData(form)));
      form.reset();
      await loadStaff();
      toast('Account created.');
    } catch (error) { toast(error.message, true); }
  };
  gpsPolicy.onchange = async () => {
    gpsPolicy.disabled = true;
    try {
      const result = await api('/dossiers/policy', {flagMissingGps:gpsPolicy.checked});
      policy.flagMissingGps = result.flagMissingGps;
      gpsPolicy.checked = result.flagMissingGps;
      if (vehicle) await open(vehicle.id, false);
    } catch (error) {
      gpsPolicy.checked = policy.flagMissingGps;
      toast(error.message, true);
    } finally {
      gpsPolicy.disabled = false;
    }
  };
  document.addEventListener('obd-view', event => {
    if (!state.authenticated) return;
    if (event.detail === 'setup' && state.user?.role === 'admin') {
      staffPanel.hidden = false;
      Promise.all([loadStaff(), loadStaffPolicy()]).catch(error => toast(error.message, true));
    }
    if (event.detail === 'dossiers') {
      find().then(() => { if (state.viewTarget) return open(state.viewTarget); }).catch(handle);
    }
  });
})();
