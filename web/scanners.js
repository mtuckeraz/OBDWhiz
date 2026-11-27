'use strict';

(() => {
  const list = $('#scanner-list');
  const profile = $('#scanner-profile');
  const message = $('#scanner-message');
  const register = $('#scanner-register');
  let listEpoch = 0;
  let listLoaded = false;
  let profileEpoch = 0;
  let pollTimer = null;
  let profileTimer = null;
  const adapterPath = id => '/adapters/' + encodeURIComponent(id);
  const jobPath = id => '/scan-jobs/' + encodeURIComponent(id);
  const statusLabel = status => (status || 'recorded').replaceAll('_', ' ');
  const active = jobs => jobs.some(job => ['queued', 'running'].includes(job.status));
  const label = vehicle => [vehicle.year, vehicle.make, vehicle.model].filter(Boolean).join(' ') || vehicle.vin || 'Draft vehicle';
  const scanButton = scan => `<button type="button" class="text-button" data-result-scan="${E(scan.id)}">${E(fleetDate(scan.createdAt))}</button>`;
  const actorCanConfirm = () => state.user?.role === 'admin';

  function jobRows(jobs, linked = false) {
    return jobs.map(job => `<li><strong>${job.type !== 'scan' && job.type !== 'verify' ? 'Service VIN check · ' : ''}${E(statusLabel(job.status))}</strong> · ${E(fleetDate(job.createdAt))}
      ${job.detail?.message ? `<p>${E(job.detail.message)}</p>` : ''}
      ${job.detail?.detectedVin ? `<p>Detected VIN ${E(job.detail.detectedVin)}</p>` : ''}
      ${job.scanId ? `<button type="button" class="text-button" data-result-scan="${E(job.scanId)}">Open scan</button>` : ''}
      ${linked && job.status === 'needs_confirmation' ? `<a href="#scanners/${E(job.scannerId)}">Review assignment</a>` : ''}
      </li>`).join('');
  }

  async function loadList() {
    const epoch = ++listEpoch;
    const scanners = await api('/adapters');
    if (epoch !== listEpoch || state.view !== 'scanners') return;
    list.innerHTML = scanners.map(scanner => `<details class="scanner-card" data-scanner-id="${E(scanner.id)}">
      <summary><strong>${E(scanner.identifier)} – ${E(scanner.name || scanner.address)}</strong>
      <span>${E(scanner.assignment ? label(scanner.assignment.vehicle) : 'Unassigned')}</span></summary></details>`).join('');
    listLoaded = true;
    message.textContent = scanners.length ? '' : 'Connect an adapter or register one below.';
    await showSelectedRow();
  }

  async function showSelectedRow() {
    clearTimeout(profileTimer);
    ++profileEpoch;
    const id = state.viewTarget;
    const rows = Array.from(list.children);
    const selected = rows.find(row => row.dataset.scannerId === id);
    for (const row of rows) row.open = row === selected;
    profile.hidden = !selected;
    if (!selected) {
      if (id) message.textContent = 'That scanner is no longer in the inventory.';
      else if (rows.length) message.textContent = '';
      return;
    }
    message.textContent = '';
    selected.append(profile);
    profile.innerHTML = '<p role="status">Loading scanner details…</p>';
    try {
      await loadProfile(id);
    } catch (error) {
      if (state.view === 'scanners' && state.viewTarget === id) {
        profile.textContent = error.message;
        report(error);
      }
    }
  }

  async function loadProfile(id) {
    if (state.view !== 'scanners' || state.viewTarget !== id) return;
    clearTimeout(profileTimer);
    const epoch = ++profileEpoch;
    const scanner = await api(adapterPath(id));
    if (epoch !== profileEpoch || state.view !== 'scanners' || state.viewTarget !== id) return;
    const assignment = scanner.assignment;
    const pending = scanner.jobs.find(job => job.status === 'needs_confirmation');
    const canEdit = actorCanConfirm();
    const jobRunning = active(scanner.jobs);
    const capabilities = scanner.capabilities;
    profile.hidden = false;
    profile.innerHTML = `
      <p class="muted">${E([scanner.manufacturer, scanner.model, scanner.firmware, scanner.address,
        scanner.ownership === 'dedicated' ? 'Vehicle-dedicated' : 'Shop-owned'].filter(Boolean).join(' · '))}</p>
      ${scanner.notes ? `<p>${E(scanner.notes)}</p>` : ''}
      <p>${assignment ? `Assigned to <a href="#dossiers/${E(assignment.vehicleId)}">${E(label(assignment.vehicle))}</a>
        · ${assignment.verifiedVin ? 'VIN verified' : 'VIN unverified'}` : 'Unassigned'}</p>
      <p class="muted small">Last detected ${E(fleetDate(scanner.lastDetectedAt) || '—')} · Last connected
        ${E(fleetDate(scanner.lastConnectedAt) || '—')} · Last scan ${E(fleetDate(scanner.lastScanAt) || '—')}</p>
      <div class="actions">
        <button type="button" class="button primary" data-verify-scanner ${jobRunning || pending ? 'disabled' : ''}>Read and verify VIN</button>
        ${canEdit ? '<button type="button" class="button secondary" data-edit-scanner>Edit profile</button>' : ''}
        ${canEdit && assignment && !pending && !jobRunning ? '<button type="button" class="button secondary" data-release-scanner>End assignment</button>' : ''}
      </div>
      <p id="scanner-job-feedback" role="status"></p>
      ${pending ? '<section id="scanner-confirmation"></section>' : ''}
      <details><summary>Scanner QR label</summary><img width="220" height="220" src="/api${adapterPath(scanner.id)}/qr"
        alt="QR code for ${E(scanner.identifier)}"><p><a href="/api${adapterPath(scanner.id)}/qr" download="${E(scanner.identifier)}.png">Download QR label</a></p></details>
      <details><summary>Capabilities</summary>
        ${capabilities ? `<p>${E(capabilities.identity || 'Identity unavailable')} · Protocol ${E(capabilities.protocol || 'unknown')}</p>
          <pre>${E(JSON.stringify(capabilities.supportedPidsByEcu, null, 2))}</pre>` : '<p>Verify the VIN to capture capabilities.</p>'}
      </details>
      <details><summary>Assignment history</summary><ul>${scanner.history.map(item =>
        `<li><a href="#dossiers/${E(item.vehicleId)}">${E(label(item.vehicle))}</a> ·
        ${E(fleetDate(item.startedAt))} to ${item.endedAt ? E(fleetDate(item.endedAt)) : 'present'}
        · ${item.verifiedVin ? 'VIN verified' : E(statusLabel(item.method))}</li>`).join('')}</ul></details>
      <details open><summary>Scan jobs</summary><ul>${jobRows(scanner.jobs) || '<li>No scan jobs.</li>'}</ul></details>
      <details><summary>Recent scans</summary><ul>${scanner.recentScans.map(scan =>
        `<li>${scanButton(scan)} · ${E(statusLabel(scan.status))}</li>`).join('')}</ul></details>`;
    profile.querySelector('[data-verify-scanner]').onclick = async event => {
      event.currentTarget.disabled = true;
      try {
        await api('/scan-jobs', {type:'verify', scannerId:id});
        await loadProfile(id);
      } catch (error) {
        const feedback = profile.querySelector('#scanner-job-feedback');
        if (state.viewTarget === id && feedback) feedback.textContent = error.message;
        event.target.disabled = false;
      }
    };
    if (canEdit) profile.querySelector('[data-edit-scanner]').onclick = () => {
      if (!register) return;
      for (const field of register.elements) {
        if (field.name) field.value = scanner[field.name] || '';
      }
      register.closest('details').open = true;
      register.scrollIntoView({block:'start'});
    };
    if (pending) await showConfirmation(pending);
    if (epoch !== profileEpoch) return;
    const release = profile.querySelector('[data-release-scanner]');
    if (release) release.onclick = async () => {
      if (!window.confirm('End this scanner assignment? The vehicle keeps its diagnostic history.')) return;
      try {
        await api(adapterPath(id) + '/release', {});
        await loadList();
      } catch (error) { report(error); }
    };
    if (jobRunning) profileTimer = setTimeout(() => loadProfile(id).catch(report), 1500);
  }

  async function showConfirmation(job) {
    const container = $('#scanner-confirmation');
    if (!actorCanConfirm()) {
      container.textContent = 'An administrator must review this assignment.';
      return;
    }
    const suggested = job.detail.suggestedVehicle;
    const detected = job.detail.detectedVin;
    container.innerHTML = `<form><h3>Confirm assignment</h3>
      <p>${detected ? `The scanner read VIN ${E(detected)}.` : 'The vehicle did not report a VIN. A manual assignment will remain unverified.'}</p>
      ${job.detail.currentVehicle ? `<p>Current assignment: ${E(label(job.detail.currentVehicle))}</p>` : ''}
      ${job.detail.displacedScannerId ? '<p>Confirming ends the other scanner’s active assignment to this vehicle.</p>' : ''}
      ${detected ? `<p>${suggested ? E(label(suggested)) : 'Create a dossier for the detected VIN.'}</p>` :
        '<label>Find vehicle<input type="search" name="query" placeholder="VIN, plate or owner"></label><label>Dossier<select name="vehicleId" required></select></label>'}
      <div class="actions"><button class="button primary">Confirm assignment</button>
        <button type="button" class="button secondary" data-cancel-confirmation>Cancel</button></div>
      <p class="error-text" role="alert"></p></form>`;
    const form = container.querySelector('form');
    if (!detected) {
      const loadVehicles = async () => {
        const query = form.elements.query.value;
        const vehicles = await api('/dossiers?q=' + encodeURIComponent(query));
        if (form.elements.query.value !== query) return;
        form.elements.vehicleId.innerHTML = '<option value="">Select vehicle</option>' + vehicles.map(vehicle =>
          `<option value="${E(vehicle.id)}">${E([label(vehicle), vehicle.plate, vehicle.vin].filter(Boolean).join(' · '))}</option>`).join('');
        if (suggested && !query) form.elements.vehicleId.value = suggested.id;
      };
      form.elements.query.oninput = () => loadVehicles().catch(report);
      await loadVehicles();
    }
    async function submit(cancel) {
      try {
        await api(jobPath(job.id) + '/confirm', cancel ? {cancel:true} :
          {vehicleId: detected ? (suggested?.id || null) : form.elements.vehicleId.value});
        await loadProfile(job.scannerId);
      } catch (error) {
        form.querySelector('[role=alert]').textContent = error.message;
      }
    }
    form.onsubmit = event => { event.preventDefault(); submit(false); };
    form.querySelector('[data-cancel-confirmation]').onclick = () => submit(true);
  }

  function report(error) {
    message.textContent = error.message;
    toast(error.message, true);
  }

  register.onsubmit = async event => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(register));
    try {
      const scanner = await api('/adapters', data);
      register.reset();
      view('scanners/' + scanner.id);
    } catch (error) { report(error); }
  };

  async function dossierDiagnostics(container, initial = false) {
    const id = container.dataset.dossierDiagnostics;
    const result = await api('/dossiers/' + id + '/diagnostics');
    if (!container.isConnected || state.view !== 'dossiers') return;
    const assignment = result.assignment;
    const running = active(result.jobs);
    if (initial) {
      const scanners = await api('/adapters');
      if (!container.isConnected) return;
      const scans = result.scans.map(scan => `<option value="${E(scan.id)}">${E(fleetDate(scan.createdAt))}</option>`).join('');
      container.innerHTML = `
        <p data-assignment-label></p>
        <button class="button primary" type="button" data-run-diagnostics>Run diagnostic scan</button>
        <form data-dossier-verify><label>Verify a scanner for this vehicle<select name="scannerId" required>
          <option value="">Select scanner</option>${scanners.map(scanner => `<option value="${E(scanner.id)}">${E(scanner.identifier)} · ${E(scanner.name || scanner.address)}</option>`).join('')}</select></label>
          <button class="button secondary">Read VIN</button></form>
        <p data-diagnostic-error class="error-text" role="alert"></p>
        <ul data-diagnostic-jobs></ul>
        <ul data-diagnostic-history></ul>
        <form data-compare-scans ${result.scans.length < 2 ? 'hidden' : ''}>
          <div class="dossier-grid"><label>First scan<select name="before">${scans}</select></label>
          <label>Second scan<select name="after">${scans}</select></label></div>
          <button class="button secondary">Compare diagnostic codes</button>
        </form><div data-comparison role="status"></div>`;
      const compare = container.querySelector('[data-compare-scans]');
      if (result.scans.length >= 2) compare.elements.before.selectedIndex = 1;
      compare.onsubmit = async event => {
        event.preventDefault();
        try {
          const query = new URLSearchParams(new FormData(compare));
          const result = await api('/dossiers/' + id + '/compare?' + query);
          const groups = [['new','Newly reported'], ['firstObserved','First observed (earlier coverage unavailable)'],
            ['persisted','Still reported'], ['noLongerReported','No longer reported'], ['unconfirmed','Missing response or coverage']];
          container.querySelector('[data-comparison]').innerHTML = `<p>${E(fleetDate(result.before))} → ${E(fleetDate(result.after))}</p>` +
            groups.map(([key, title]) => `<p><strong>${title}</strong> · ${result[key].map(code =>
              E([code.ecu, code.code, code.status].filter(Boolean).join(' '))).join(', ') || 'None'}</p>`).join('');
        } catch (error) { container.querySelector('[data-diagnostic-error]').textContent = error.message; }
      };
      async function enqueue(type, scannerId) {
        try {
          await api('/scan-jobs', {type, scannerId, vehicleId:id});
          await dossierDiagnostics(container);
        } catch (error) { container.querySelector('[data-diagnostic-error]').textContent = error.message; }
      }
      container.querySelector('[data-run-diagnostics]').onclick = event => enqueue('scan', event.currentTarget.dataset.scannerId);
      container.querySelector('[data-dossier-verify]').onsubmit = event => {
        event.preventDefault();
        enqueue('verify', event.currentTarget.elements.scannerId.value);
      };
      container.querySelector('[data-diagnostic-history]').onclick = event => {
        const button = event.target.closest('[data-delete-diagnostic]');
        if (!button) return;
        action(button, async () => {
          if (await deleteScan(button.dataset.deleteDiagnostic, button.dataset.scanDate)) {
            await dossierDiagnostics(container, true);
          }
        });
      };
    }
    container.querySelector('[data-assignment-label]').innerHTML = assignment ?
      `<a href="#scanners/${E(assignment.scannerId)}">Open assigned scanner</a> · ${assignment.verifiedVin ? 'VIN verified' : 'VIN unverified'}` :
      'No scanner assigned. Select one below to verify its VIN.';
    const button = container.querySelector('[data-run-diagnostics]');
    button.disabled = !assignment || running;
    button.dataset.scannerId = assignment?.scannerId || '';
    container.querySelector('[data-diagnostic-jobs]').innerHTML = jobRows(result.jobs, true);
    container.querySelector('[data-diagnostic-history]').innerHTML = result.scans.map(scan =>
      `<li>${scanButton(scan)} · ${E(statusLabel(scan.status))}${state.user?.role === 'admin' ?
        ` · <button type="button" class="text-button danger" data-delete-diagnostic="${E(scan.id)}" data-scan-date="${E(scan.createdAt)}">Delete scan</button>` : ''}</li>`).join('');
    const compare = container.querySelector('[data-compare-scans]');
    if (!initial && compare.elements.before.options.length !== result.scans.length) {
      const choices = result.scans.map(scan => `<option value="${E(scan.id)}">${E(fleetDate(scan.createdAt))}</option>`).join('');
      compare.elements.before.innerHTML = compare.elements.after.innerHTML = choices;
      if (result.scans.length >= 2) compare.elements.before.selectedIndex = 1;
      compare.hidden = result.scans.length < 2;
    }
    clearTimeout(pollTimer);
    if (running) pollTimer = setTimeout(() => dossierDiagnostics(container).catch(report), 1500);
  }

  document.addEventListener('obd-dossier-rendered', () => {
    const container = $('[data-dossier-diagnostics]');
    if (container) dossierDiagnostics(container, true).catch(report);
  });
  document.addEventListener('obd-view', event => {
    if (event.detail === 'scanners' && state.authenticated) {
      const id = state.viewTarget;
      if (listLoaded && (!id || Array.from(list.children).some(row => row.dataset.scannerId === id))) {
        showSelectedRow().catch(report);
      } else loadList().catch(report);
    } else {
      listLoaded = false;
      ++listEpoch;
      ++profileEpoch;
      clearTimeout(profileTimer);
    }
  });
  list.addEventListener('click', event => {
    const summary = event.target.closest('.scanner-card > summary');
    if (!summary) return;
    event.preventDefault();
    const row = summary.parentElement;
    view(row.open ? 'scanners' : 'scanners/' + row.dataset.scannerId);
  });
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-result-scan]');
    if (button) selectScan(button.dataset.resultScan).catch(report);
  });
})();
