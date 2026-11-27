'use strict';

(() => {
  const form = $('#special-form');
  const {scanner, operation, tireSize} = form.elements;
  const guidance = $('#special-function-guidance');
  const status = $('#special-status');
  const execute = form.querySelector('button');
  let catalog = {};
  let vehicles = [];
  let job = null;
  let polling;
  let submitting = false;

  const active = () => ['queued', 'running'].includes(job?.status);

  function refreshControls() {
    const fn = catalog[operation.value];
    const needsSize = fn?.input === 'tireSize';
    $('#special-tire-size').hidden = !needsSize;
    tireSize.required = needsSize;
    execute.disabled = !fn || active() || submitting;
    const link = fn && safeURL(fn.reference);
    guidance.innerHTML = fn
      ? `OEM service information: <a href="${E(link)}" target="_blank" rel="noopener noreferrer">${E(fn.tool)}</a>.`
      : '';
  }

  function listOperations() {
    const vehicle = vehicles.find(item => item.id === scanner.value)?.assignment.vehicle;
    const make = (vehicle?.make || '').toLowerCase().replace(/[^a-z]/g, '');
    const entries = Object.entries(catalog).filter(([, fn]) => fn.makes.includes(make));
    const previous = operation.value;
    const placeholder = vehicle && !entries.length ? 'No functions listed for this make' :
      vehicle ? 'Select a function' : 'Select a vehicle first';
    operation.replaceChildren(new Option(placeholder, ''),
      ...entries.map(([key, fn]) => new Option(fn.label, key)));
    operation.disabled = !entries.length;
    operation.value = entries.some(([key]) => key === previous) ? previous : '';
    refreshControls();
  }

  function showJob(value) {
    job = value;
    const stage = {
      queued: 'Request accepted. Waiting for the scanner.',
      running: 'Reading the vehicle VIN.',
      complete: 'Vehicle identity confirmed.',
      failed: 'Vehicle identity could not be confirmed.',
    }[job.status] || job.status;
    status.innerHTML = `<h3>${E(stage)}</h3>
      <p>${E(catalog[job.type]?.label || 'Service request')} · ${E(fleetDate(job.createdAt))}</p>
      ${job.detail?.confirmedVin ? `<p>Vehicle returned VIN ${E(job.detail.confirmedVin)}.</p>` : ''}
      ${job.detail?.requestedTireSize ? `<p>Requested tire size ${E(job.detail.requestedTireSize)}.</p>` : ''}
      ${job.detail?.message ? `<p>${E(job.detail.message)}</p>` : ''}
      <p class="muted small">Request ID ${E(job.id)}</p>`;
    refreshControls();
  }

  function watchJob() {
    clearTimeout(polling);
    if (!active() || state.view !== 'special') return;
    polling = setTimeout(async () => {
      try {
        const value = await api('/scan-jobs/' + encodeURIComponent(job.id));
        if (state.view === 'special') showJob(value);
      } catch (error) {
        status.textContent = error.message;
      }
      watchJob();
    }, 1500);
  }

  async function load() {
    const [functions, adapters] = await Promise.all([api('/special-functions'), api('/adapters')]);
    if (state.view !== 'special') return;
    catalog = functions;
    vehicles = adapters.filter(item => item.assignment?.verifiedVin);
    const previous = scanner.value;
    scanner.replaceChildren(new Option(vehicles.length ? 'Select an assigned scanner' : 'No VIN-verified scanner assignments', ''),
      ...vehicles.map(item => {
        const v = item.assignment.vehicle;
        const vehicle = [v.year, v.make, v.model].filter(Boolean).join(' ') || v.vin;
        return new Option(`${vehicle} · ${item.identifier} · ${item.name || item.address}`, item.id);
      }));
    if (vehicles.some(item => item.id === previous)) scanner.value = previous;
    listOperations();
    if (!vehicles.length) guidance.innerHTML = '<a href="#scanners">Verify a vehicle assignment in Scanner Inventory</a>.';
    if (job) {
      showJob(job);
      watchJob();
    }
  }

  scanner.addEventListener('change', listOperations);
  operation.addEventListener('change', refreshControls);
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (execute.disabled) return;
    submitting = true;
    refreshControls();
    const selected = vehicles.find(item => item.id === scanner.value);
    try {
      const value = await api('/special-functions', {scannerId: selected.id, vehicleId: selected.assignment.vehicleId,
        function: operation.value, tireSize: tireSize.value.trim()});
      showJob(value);
      watchJob();
    } catch (error) {
      status.textContent = error.message;
    } finally {
      submitting = false;
      refreshControls();
    }
  });
  document.addEventListener('obd-view', event => {
    clearTimeout(polling);
    if (event.detail === 'special' && state.authenticated) load().catch(error => {status.textContent = error.message;});
  });
  if (state.view === 'special' && state.authenticated) load().catch(error => {status.textContent = error.message;});
})();
