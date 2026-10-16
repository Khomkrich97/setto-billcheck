(function(){
  'use strict';
  var KEY = new URLSearchParams(location.search).get('k') || '';
  var $ = function(id){ return document.getElementById(id); };
  var S = null, lastSig = {}, filter = 'all', resultSig = '', busy = false;

  var STATUS = { bad:'ไม่เจอในบิล Meta', dup:'ยอดซ้ำ ต้องเทียบ', wait:'รอบิล เช็คซ้ำพรุ่งนี้', ok:'ตรงบิล Meta', plat:'แพลตฟอร์มอื่น' };
  var ORDER = ['bad','dup','wait','ok','plat'];
  var ICON = {
    todo:'<svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="5" width="20" height="14" rx="2"/><path d="M2 10h20"/></svg>',
    ok:'<svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>',
    warn:'<svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 8v4"/><path d="M12 16h.01"/></svg>',
    play:'<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M7 4.5v15l13-7.5z"/></svg>'
  };

  function esc(s){ return String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }
  function baht(v){ return Number(v || 0).toLocaleString('en-US', { minimumFractionDigits:2, maximumFractionDigits:2 }); }
  // counts come back from the server as written by a PC: render numbers only, never raw values
  function counts(summary){
    var n = (summary && summary.n) || {}, out = {};
    ORDER.forEach(function(k){ var v = Math.floor(Number(n[k])); out[k] = isFinite(v) && v > 0 ? v : 0; });
    return out;
  }
  function nick(email){ return String(email || '').split('@')[0]; }

  function api(path, body){
    var opt = { headers:{ 'X-Key':KEY } };
    if(body !== undefined){ opt.method = 'POST'; opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
    return fetch(path, opt).then(function(r){ if(!r.ok) throw new Error('http ' + r.status); return r.json(); });
  }

  function show(view){
    ['boot','login','home','run','error','done'].forEach(function(v){ $('view-' + v).hidden = (v !== view); });
  }
  function changed(key, val){
    var sig = JSON.stringify(val);
    if(lastSig[key] === sig) return false;
    lastSig[key] = sig; return true;
  }

  function poll(){
    api('/api/status').then(function(s){ S = s; render(); })
      .catch(function(){ $('foot').textContent = 'โปรแกรมหยุดทำงานแล้ว ปิดหน้าต่างนี้แล้วเปิดใหม่จาก START.bat'; })
      .then(function(){ setTimeout(poll, S && S.run && S.run.phase === 'running' ? 800 : 1500); });
  }

  function render(){
    $('foot').textContent = 'เวอร์ชัน ' + S.version;
    var a = S.auth;
    $('who').hidden = !a;
    if(a){
      $('who-email').textContent = a.email;
      $('who-exp').textContent = a.days_left <= 5 ? ('การล็อกอินหมดอายุ ' + a.exp + ' ต้องล็อกอินใหม่') : ('จำการล็อกอินถึง ' + a.exp);
    }
    var ph = S.run.phase;
    if(ph === 'running') return renderRun();
    if(ph === 'done') return renderDone();
    if(ph === 'error') return renderError();
    if(!a) return show('login');
    renderHome();
  }

  // ── home ──
  function renderHome(){
    show('home');
    $('sw-auto').checked = !!S.autostart;
    $('pdf-path').textContent = S.pdf_dir || '';
    $('btn-pdf-reset').hidden = !S.pdf_custom;
    if(changed('hero', [S.done_today, S.last, S.busy_elsewhere, S.last_err, S.net_wait])) $('hero').innerHTML = heroHtml();
    if(changed('runs', S.runs)) $('runs').innerHTML = runsHtml();
  }
  function heroHtml(){
    var d = S.done_today, last = S.last, ic = 'todo', h, p, extra = '';
    if(S.net_wait){
      h = 'รออินเทอร์เน็ต'; p = 'เครื่องเพิ่งเปิด โปรแกรมจะเริ่มตรวจเองเมื่อต่ออินเทอร์เน็ตได้';
    } else if(S.busy_elsewhere){
      ic = 'warn'; h = 'มีคนกำลังตรวจอยู่';
      p = nick(S.busy_elsewhere.by) + ' เครื่อง ' + (S.busy_elsewhere.host || 'ไม่ทราบชื่อ') + ' เริ่มเมื่อ ' + S.busy_elsewhere.started + ' รอให้เสร็จแล้วดูผลในหน้าเติมงบแอดได้เลย';
    } else if(d){
      var n = counts(d.summary);
      ic = (n.bad || 0) > 0 ? 'bad' : ((n.dup || 0) > 0 ? 'warn' : 'ok');
      h = 'วันนี้ตรวจแล้ว';
      p = 'เสร็จ ' + (d.finished || d.started) + ' โดย ' + nick(d.by) + (d.host ? ' เครื่อง ' + d.host : '') + ' ผลอยู่ในหน้าเติมงบแอด แท็บเช็คเงินตัด';
      extra = sumPills(n);
    } else {
      h = 'วันนี้ยังไม่ได้ตรวจ';
      p = last ? ('ตรวจล่าสุด ' + last.finished + ' โดย ' + nick(last.by)) : 'ยังไม่เคยตรวจในระบบ กดเริ่มตรวจได้เลย';
    }
    if(S.last_err && !S.net_wait) extra += '<div class="note-line bad">' + esc(S.last_err) + '</div>';
    return '<div class="hero-ic ' + ic + '">' + (ic === 'ok' ? ICON.ok : ic === 'todo' ? ICON.todo : ICON.warn) + '</div>'
      + '<div class="hero-txt"><h1>' + esc(h) + '</h1><p>' + esc(p) + '</p>' + extra + '</div>'
      + '<div class="hero-side">'
      +   '<button class="btn primary lg" type="button" data-act="start"' + (S.busy_elsewhere ? ' disabled' : '') + '>' + ICON.play + (d ? 'ตรวจอีกรอบ' : 'เริ่มตรวจ') + '</button>'
      +   '<select id="sel-days" aria-label="ช่วงที่ตรวจ"><option value="">SMS เมื่อวาน และที่ยังค้าง</option><option value="1">เมื่อวานอย่างเดียว</option><option value="3">ย้อน 3 วัน</option><option value="7">ย้อน 7 วัน</option><option value="14">ย้อน 14 วัน</option><option value="31">ย้อน 31 วัน</option></select>'
      +   '<button class="btn ghost" type="button" data-act="nisit">เปิดผลในหน้าเติมงบแอด</button>'
      + '</div>';
  }
  function sumPills(n){
    var out = ORDER.filter(function(k){ return n[k]; }).map(function(k){
      return '<span class="pill ' + k + '"><i></i>' + STATUS[k] + ' ' + n[k] + '</span>';
    }).join('');
    return out ? '<div class="sum-pills">' + out + '</div>' : '';
  }
  function runsHtml(){
    if(!S.runs || !S.runs.length) return '<div class="muted" style="padding:10px 0">ยังไม่มีรอบตรวจ</div>';
    return S.runs.map(function(r){
      var n = counts(r.summary), pill;
      if(r.status === 'running') pill = '<span class="pill info"><i></i>กำลังตรวจ</span>';
      else if(r.status === 'failed') pill = '<span class="pill none" title="' + esc(r.error || '') + '"><i></i>ไม่สำเร็จ</span>';
      else if(n.bad) pill = '<span class="pill bad"><i></i>ไม่เจอในบิล ' + n.bad + '</span>';
      else if(n.dup) pill = '<span class="pill dup"><i></i>ต้องเทียบ ' + n.dup + '</span>';
      else pill = '<span class="pill ok"><i></i>ตรงบิลทั้งหมด</span>';
      return '<div class="run-row"><div><div class="mono">' + esc(r.started) + '</div><div class="who-run">' + esc(nick(r.by)) + (r.host ? ' เครื่อง ' + esc(r.host) : '') + '</div></div>' + pill + '</div>';
    }).join('');
  }

  // ── running ──
  function renderRun(){
    show('run');
    var R = S.run;
    $('run-started').textContent = 'เริ่ม ' + R.started;
    if(changed('steps', R.steps)){
      $('steps').innerHTML = R.steps.map(function(st, i){
        var mark = st.state === 'done' ? '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>'
                 : st.state === 'skip' ? '-' : String(i + 1);
        return '<li class="step ' + st.state + '"><span class="dot">' + mark + '</span><span>' + esc(st.label) + '</span></li>';
      }).join('');
    }
    $('fb-alert').hidden = !R.fb_login;
    var p = R.progress, bar = $('prog-bar');
    bar.parentNode.classList.toggle('indet', !p.total);
    bar.style.width = p.total ? Math.round(100 * p.done / p.total) + '%' : '';
    $('prog-num').textContent = p.total ? (p.done + ' / ' + p.total + ' บัญชี') : '';
    var lastLog = R.log.length ? R.log[R.log.length - 1].m : 'เตรียมตัว';
    $('prog-label').textContent = R.fb_login ? 'รอล็อกอิน Facebook' : lastLog;
    if(changed('accts', R.accounts)){
      $('accts').innerHTML = R.accounts.map(function(a){
        return '<div class="acct ' + esc(a.state) + '" title="' + esc(a.info || '') + '"><i class="st"></i><span>' + esc(a.name) + '</span></div>';
      }).join('');
    }
    if(changed('log', R.log)){
      var box = $('log'), atEnd = box.scrollTop + box.clientHeight >= box.scrollHeight - 8;
      box.innerHTML = R.log.map(function(l){ return '<div><time>' + esc(l.t) + '</time>' + esc(l.m) + '</div>'; }).join('');
      if(atEnd) box.scrollTop = box.scrollHeight;
    }
  }

  // ── error ──
  function renderError(){
    show('error');
    $('err-msg').textContent = S.run.error;
    $('btn-retry').textContent = S.run.auth_error ? 'ล็อกอินใหม่' : 'ตรวจอีกครั้ง';
  }

  // ── done ──
  function renderDone(){
    show('done');
    var res = S.run.result || {}, sum = res.summary || {}, n = counts(sum), sums = sum.sum || {};
    var sig = JSON.stringify([res.d_judge, res.today, sum, filter, (res.rows || []).length]);
    if(sig === resultSig) return;
    resultSig = sig;
    var bad = (n.bad || 0) + (n.dup || 0);
    $('done-title').textContent = res.saved ? (bad ? 'ตรวจเสร็จ มีรายการที่ต้องดู ' + bad + ' รายการ' : 'ตรวจเสร็จ ทุกก้อนเรียบร้อย') : 'ตรวจเสร็จ (ลองบางบัญชี ไม่ได้ส่งขึ้นระบบ)';
    var ic = document.querySelector('.done-ic');
    ic.classList.toggle('warn', bad > 0);
    ic.innerHTML = bad > 0 ? ICON.warn : ICON.ok;
    $('done-sub').textContent = 'SMS ตั้งแต่ ' + res.d_judge + ' ถึง ' + res.today + ' เทียบบิล Meta ' + (Number(sum.dl_ok) || 0) + ' บัญชี'
      + (res.saved ? ' ส่งผลขึ้นระบบแล้ว ฝ่ายบัญชีดูได้ที่หน้าเติมงบแอด แท็บเช็คเงินตัด' : '');
    var warns = [];
    if(sum.first) warns.push('<b>รอบแรก</b> ยังไม่มีประวัติคู่ที่เคยตรงกัน รายการเขียวรอบนี้เป็นผลเบื้องต้น ตรวจทุกวันแล้วผลจะแม่นขึ้นเอง');
    if((res.fails || []).length) warns.push('<b>โหลดบิลไม่สำเร็จ ' + res.fails.length + ' บัญชี</b> รายการแดงอาจมาจากบัญชีเหล่านี้ กดตรวจอีกรอบ ' + res.fails.map(function(f){ return esc(f.ad); }).join(', '));
    $('done-warn').hidden = !warns.length;
    $('done-warn').innerHTML = warns.join('<br>');

    $('tiles').innerHTML = ORDER.map(function(k){
      return '<button type="button" class="tile ' + k + (filter === k ? ' on' : '') + '" data-filter="' + k + '"><div class="l">' + STATUS[k] + '</div><div class="v">' + (n[k] || 0) + '</div><div class="s">' + baht(sums[k]) + ' บาท</div></button>';
    }).join('');
    $('chips').innerHTML = [['all','ทั้งหมด']].concat(ORDER.map(function(k){ return [k, STATUS[k]]; })).map(function(c){
      return '<button type="button" class="chip' + (filter === c[0] ? ' on' : '') + '" data-filter="' + c[0] + '">' + c[1] + '</button>';
    }).join('');
    var rows = (res.rows || []).filter(function(r){ return filter === 'all' || r.status === filter; });
    $('rows').innerHTML = rows.length ? rows.map(function(r){
      var ads = (r.bills || []).map(function(b){
        return '<div>' + esc(b.ad) + '<span class="sub mono">' + esc(b.day) + ' บัตร ' + esc(b.card) + (r.bills.length > 1 ? ' ' + baht(b.amount) : '') + '</span></div>';
      }).join('') || '<span class="muted">ไม่มี</span>';
      return '<tr class="' + (r.status === 'bad' ? 'bad' : '') + '"><td class="nowrap"><span class="pill ' + r.status + '"><i></i>' + STATUS[r.status] + '</span></td>'
        + '<td class="mono nowrap">' + esc(r.at) + '</td><td>' + esc(r.acct) + '</td><td class="num">' + baht(r.amount) + '</td>'
        + '<td>' + ads + '</td><td class="muted">' + esc(r.note) + '</td></tr>';
    }).join('') : '<tr><td colspan="6" class="empty">ไม่มีรายการ</td></tr>';
    var lon = res.lonely || [];
    $('lonely-n').textContent = lon.length + ' รายการ ไว้ดูประกอบ ไม่ได้แปลว่าผิด';
    $('lonely').innerHTML = lon.length ? lon.map(function(b){
      return '<tr><td class="mono nowrap">' + esc(b.day) + '</td><td>' + esc(b.ad) + '</td><td class="mono">' + esc(b.card) + '</td><td class="num">' + baht(b.amount) + '</td><td class="muted">' + esc(b.why) + '</td></tr>';
    }).join('') : '<tr><td colspan="5" class="empty">ไม่มี</td></tr>';
  }

  // ── actions ──
  function act(name){
    if(busy) return;
    if(name === 'start'){
      var sel = $('sel-days');
      busy = true;
      api('/api/start', { days: sel && sel.value ? sel.value : null })
        .then(function(r){ if(!r.ok) alert(r.error); resultSig = ''; })
        .catch(function(){}).then(function(){ busy = false; });
    } else if(name === 'nisit'){
      api('/api/open', { what:'nisit' });
    }
  }
  document.addEventListener('click', function(e){
    var t = e.target.closest('[data-act]');
    if(t) return act(t.getAttribute('data-act'));
    var f = e.target.closest('[data-filter]');
    if(f){ var v = f.getAttribute('data-filter'); filter = (filter === v && f.classList.contains('tile')) ? 'all' : v; resultSig = ''; renderDone(); }
  });
  $('login-form').addEventListener('submit', function(e){
    e.preventDefault();
    var btn = $('btn-login'); btn.disabled = true; btn.innerHTML = '<span class="spin"></span>กำลังล็อกอิน';
    $('login-err').hidden = true;
    api('/api/login', { email:$('in-email').value, password:$('in-pw').value }).then(function(r){
      if(!r.ok){ $('login-err').textContent = r.error; $('login-err').hidden = false; }
      else { $('in-pw').value = ''; lastSig = {}; }
    }).catch(function(){ $('login-err').textContent = 'ติดต่อโปรแกรมไม่ได้ ปิดแล้วเปิดใหม่'; $('login-err').hidden = false; })
      .then(function(){ btn.disabled = false; btn.textContent = 'ล็อกอิน'; });
  });
  $('btn-logout').addEventListener('click', function(){
    if(!confirm('ออกจากระบบ NISIT ในโปรแกรมนี้?')) return;
    api('/api/logout', {}).then(function(r){ if(!r.ok) alert(r.error); lastSig = {}; });
  });
  $('sw-auto').addEventListener('change', function(e){
    var on = e.target.checked;
    api('/api/autostart', { on:on }).then(function(r){ e.target.checked = !!r.autostart; })
      .catch(function(){ e.target.checked = !on; alert('ตั้งค่าไม่สำเร็จ'); });
  });
  function pdfDir(action){
    return api('/api/pdfdir', { action:action }).then(function(r){
      S.pdf_dir = r.pdf_dir; S.pdf_custom = r.pdf_custom;
      $('pdf-path').textContent = r.pdf_dir || ''; $('btn-pdf-reset').hidden = !r.pdf_custom;
    }).catch(function(){ alert('ตั้งค่าไม่สำเร็จ'); });
  }
  $('btn-pdf-pick').addEventListener('click', function(e){
    var b = e.currentTarget; b.disabled = true;
    pdfDir('pick').then(function(){ b.disabled = false; });
  });
  $('btn-pdf-reset').addEventListener('click', function(){ pdfDir('reset'); });
  $('btn-pdf-open').addEventListener('click', function(){ api('/api/open', { what:'pdfroot' }); });
  $('btn-stop').addEventListener('click', function(){
    if(confirm('หยุดตรวจรอบนี้? ผลจะยังไม่ขึ้นระบบ')) api('/api/stop', {});
  });
  $('btn-retry').addEventListener('click', function(){
    if(S && S.run.auth_error){ api('/api/reset', {}).then(function(){ lastSig = {}; }); return; }
    api('/api/reset', {}).then(function(){ lastSig = {}; act('start'); });
  });
  function goHome(){ api('/api/reset', {}).then(function(){ lastSig = {}; resultSig = ''; filter = 'all'; api('/api/refresh', {}); }); }
  $('btn-home').addEventListener('click', goHome);
  $('btn-home2').addEventListener('click', goHome);
  $('btn-open-nisit').addEventListener('click', function(){ api('/api/open', { what:'nisit' }); });
  $('btn-folder').addEventListener('click', function(){ api('/api/open', { what:'folder' }); });

  poll();
})();
