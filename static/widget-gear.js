(() => {
  let cachedSchemas = null;

  async function fetchSchemas() {
    if (cachedSchemas) return cachedSchemas;
    const resp = await fetch('/api/widgets');
    cachedSchemas = await resp.json();
    return cachedSchemas;
  }

  function resolveParams(schema, rawParams) {
    const out = {};
    for (const p of schema?.params || []) {
      const val = rawParams && rawParams[p.key] !== undefined ? rawParams[p.key] : p.default;
      out[p.key] = val;
    }
    return out;
  }

  function buildWidgetUrl(schema, rawParams, embed = false) {
    const resolved = resolveParams(schema, rawParams);
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(resolved)) {
      if (v !== undefined && v !== null && v !== '') {
        qs.set(k, String(v));
      }
    }
    if (embed) qs.set('embed', '1');
    const q = qs.toString();
    return q ? `${schema.path}?${q}` : schema.path;
  }

  function renderParamInputs(container, schema, currentValues, onChange) {
    container.innerHTML = '';
    const params = schema?.params || [];
    if (params.length === 0) {
      const empty = document.createElement('div');
      empty.style.cssText = 'font-size:11px;color:#8a96a8;';
      empty.textContent = 'No configurable parameters.';
      container.appendChild(empty);
      return;
    }

    const resolved = resolveParams(schema, currentValues);
    for (const p of params) {
      const row = document.createElement('label');
      row.style.cssText = 'display:flex;align-items:center;justify-content:space-between;gap:10px;font-size:12px;color:#e6ecf5;';

      const lbl = document.createElement('span');
      lbl.style.cssText = 'font-weight:700;color:#8a96a8;font-size:11px;';
      lbl.textContent = p.label;
      row.appendChild(lbl);

      if (p.type === 'boolean') {
        const chk = document.createElement('input');
        chk.type = 'checkbox';
        chk.checked = resolved[p.key] === true || resolved[p.key] === 'true';
        chk.style.cssText = 'width:16px;height:16px;min-height:16px;padding:0;cursor:pointer;';
        chk.addEventListener('change', () => onChange(p.key, chk.checked));
        row.appendChild(chk);
      } else if (p.type === 'select') {
        const sel = document.createElement('select');
        sel.style.cssText = 'background:#1a202a;color:#e6ecf5;border:1px solid #283140;border-radius:6px;padding:4px 8px;font-size:11px;font-weight:700;max-width:170px;';
        for (const opt of p.options || []) {
          const o = document.createElement('option');
          o.value = opt.value;
          o.textContent = opt.label;
          if (String(resolved[p.key]) === String(opt.value)) o.selected = true;
          sel.appendChild(o);
        }
        sel.addEventListener('change', () => onChange(p.key, sel.value));
        row.appendChild(sel);
      }
      container.appendChild(row);
    }
  }

  async function mountStandaloneGear(widgetType) {
    const urlParams = new URLSearchParams(location.search);
    if (urlParams.get('embed') === '1') return;

    const schemas = await fetchSchemas();
    const schema = schemas[widgetType];
    if (!schema || !schema.params || schema.params.length === 0) return;

    const current = {};
    for (const p of schema.params) {
      if (urlParams.has(p.key)) {
        const raw = urlParams.get(p.key);
        current[p.key] = p.type === 'boolean' ? raw === 'true' : raw;
      } else {
        current[p.key] = p.default;
      }
    }

    const style = document.createElement('style');
    style.textContent = `
      .wg-corner { position: fixed; top: 8px; right: 8px; z-index: 9999; display: flex; flex-direction: column; align-items: flex-end; gap: 6px; }
      .wg-btn { opacity: 0; transition: opacity 0.16s ease, background 0.12s ease; width: 28px; height: 28px; border-radius: 7px; border: 1px solid #3a465c; background: rgba(20, 24, 32, 0.9); color: #e6ecf5; font-size: 14px; cursor: pointer; display: grid; place-items: center; }
      body:hover .wg-btn, .wg-corner:hover .wg-btn, .wg-btn.open { opacity: 0.92; }
      .wg-btn:hover { background: #232a37; }
      .wg-pop { display: none; flex-direction: column; gap: 8px; min-width: 220px; padding: 10px 12px; border-radius: 9px; border: 1px solid #3a465c; background: rgba(16, 20, 27, 0.97); box-shadow: 0 10px 28px rgba(0,0,0,0.5); font-family: ui-sans-serif, system-ui, sans-serif; }
      .wg-pop.open { display: flex; }
      .wg-pop-title { font-size: 10px; font-weight: 800; letter-spacing: 0.08em; text-transform: uppercase; color: #8a96a8; }
      .wg-pop-note { font-size: 10px; color: #64748b; }
    `;
    document.head.appendChild(style);

    const wrap = document.createElement('div');
    wrap.className = 'wg-corner';

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'wg-btn';
    btn.title = 'Temporary view parameters';
    btn.textContent = '⚙';

    const pop = document.createElement('div');
    pop.className = 'wg-pop';

    const title = document.createElement('div');
    title.className = 'wg-pop-title';
    title.textContent = `${schema.label} Settings`;

    const body = document.createElement('div');
    body.style.cssText = 'display:flex;flex-direction:column;gap:8px;';

    const note = document.createElement('div');
    note.className = 'wg-pop-note';
    note.textContent = 'Temporary for this view only.';

    renderParamInputs(body, schema, current, (key, val) => {
      current[key] = val;
      location.href = buildWidgetUrl(schema, current, false);
    });

    pop.append(title, body, note);
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const open = pop.classList.toggle('open');
      btn.classList.toggle('open', open);
    });
    document.addEventListener('click', (e) => {
      if (!wrap.contains(e.target)) {
        pop.classList.remove('open');
        btn.classList.remove('open');
      }
    });

    wrap.append(btn, pop);
    document.body.appendChild(wrap);
  }

  window.WidgetGear = { fetchSchemas, resolveParams, buildWidgetUrl, renderParamInputs, mountStandaloneGear };
})();
