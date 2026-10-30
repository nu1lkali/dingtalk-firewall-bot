/* ============================================================
   动态云防火墙 · 管理后台逻辑
   ============================================================ */
(function () {
  'use strict';

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  const PAGE_META = {
    dashboard: { title: '仪表盘', sub: '实时掌握放行规则与自动化任务运行状态' },
    rules: { title: '放行规则', sub: '查看、新增、编辑、启停与删除云端放行规则' },
    cloud: { title: '阿里云防火墙', sub: '直接读取与操作轻量应用服务器防火墙规则' },
    dingtalk: { title: '钉钉机器人', sub: '群内 @机器人 发送 IP 即可完成放行' },
    cron: { title: '定时任务', sub: '内置调度器，无需外部 cron 依赖' },
    logs: { title: '日志与记录', sub: '操作审计与机器人指令留痕' },
    settings: { title: '系统设置', sub: '云账户、安全与服务参数统一管理' },
    guide: { title: '使用教程', sub: '从接入云账户到自动化放行的完整操作指引' },
  };

  const state = {
    page: 'dashboard',
    settings: null,
    rules: [],
    cloud: [],
    ruleFilter: 'current',
    ruleKeyword: '',
    ruleTag: '',
    rulePage: 1,
    rulePageSize: 20,
    ruleTotal: 0,
    rulePages: 1,
    ruleTags: [],
    geo: {},
    cron: null,
    overview: null,
    logTab: 'op',
    me: null,
  };

  const THEME_KEY = 'dcf-theme';
  const THEME_ORDER = ['auto', 'light', 'dark'];
  const THEME_LABEL = { auto: '跟随系统', light: '浅色', dark: '深色' };

  /* 规则的终态：只作为历史记录保留，可恢复到云端或从本地清理 */
  const GONE_STATUS = ['deleted', 'expired', 'stale'];

  /* ---------------------------------------------------------- 基础工具 */
  function esc(value) {
    return String(value === null || value === undefined ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function toast(message, type) {
    type = type || 'success';
    const icons = { success: '✓', error: '✕', warn: '!', info: 'i' };
    const el = document.createElement('div');
    el.className = 'toast ' + type;
    el.innerHTML = '<span>' + (icons[type] || '·') + '</span><div>' + esc(message) + '</div>';
    $('#toast-root').appendChild(el);
    setTimeout(() => {
      el.style.opacity = '0';
      el.style.transform = 'translateX(12px)';
      el.style.transition = 'all .2s';
      setTimeout(() => el.remove(), 220);
    }, type === 'error' ? 5200 : 3200);
  }

  async function api(path, options) {
    const opts = Object.assign({ headers: {} }, options || {});
    if (opts.body && typeof opts.body !== 'string') {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(opts.body);
    }
    const resp = await fetch(path, opts);
    let data = {};
    try { data = await resp.json(); } catch (e) { /* 忽略非 JSON 响应 */ }
    if (resp.status === 401) {
      location.href = '/login';
      throw new Error('登录已失效');
    }
    if (resp.status === 428) {
      // 后端要求先改初始口令，直接弹改密框
      openPasswordGate();
      throw new Error(data.message || '请先修改初始口令');
    }
    if (!resp.ok || data.ok === false) {
      throw new Error(data.message || ('请求失败（HTTP ' + resp.status + '）'));
    }
    return data.data !== undefined ? data.data : data;
  }

  function fmtTime(value) {
    if (!value) return '—';
    const text = String(value);
    return text.length >= 16 ? text.slice(5, 16) : text;
  }

  function statusBadge(status) {
    const map = {
      active: ['生效中', 'badge-green'],
      disabled: ['已禁用', 'badge-amber'],
      deleted: ['已删除', 'badge-red'],
      expired: ['已过期', 'badge-red'],
      stale: ['已清理', 'badge'],
      success: ['成功', 'badge-green'],
      failed: ['失败', 'badge-red'],
      warning: ['告警', 'badge-amber'],
      skipped: ['跳过', 'badge'],
      running: ['执行中', 'badge-blue'],
      denied: ['已拒绝', 'badge-red'],
      ignored: ['已忽略', 'badge'],
    };
    const item = map[status] || [status || '—', 'badge'];
    return '<span class="badge ' + item[1] + '">' + esc(item[0]) + '</span>';
  }

  function levelBadge(level) {
    const map = { INFO: 'badge-blue', WARNING: 'badge-amber', ERROR: 'badge-red', DEBUG: 'badge' };
    return '<span class="badge ' + (map[level] || 'badge') + '">' + esc(level || 'INFO') + '</span>';
  }

  /* ---------------------------------------------------------- 弹窗 */
  function openModal(config) {
    const root = $('#modal-root');
    root.innerHTML =
      '<div class="modal-backdrop"><div class="modal ' + (config.wide ? 'wide' : '') + '">' +
      '<div class="modal-head"><h3>' + esc(config.title) + '</h3>' +
      '<button class="btn btn-ghost btn-sm close">✕</button></div>' +
      '<div class="modal-body">' + config.body + '</div>' +
      (config.footer ? '<div class="modal-foot">' + config.footer + '</div>' : '') +
      '</div></div>';

    function close() { root.innerHTML = ''; document.removeEventListener('keydown', onKey); }
    function onKey(e) { if (e.key === 'Escape') close(); }
    document.addEventListener('keydown', onKey);
    $('.close', root).onclick = close;
    $('.modal-backdrop', root).addEventListener('click', (e) => {
      if (e.target.classList.contains('modal-backdrop')) close();
    });
    return { root: root, close: close };
  }

  function confirmDialog(title, text, danger) {
    return new Promise((resolve) => {
      const m = openModal({
        title: title,
        body: '<p style="margin:0;color:var(--text-soft)">' + esc(text) + '</p>',
        footer: '<button class="btn" data-act="cancel">取消</button>' +
          '<button class="btn ' + (danger ? 'btn-danger' : 'btn-primary') + '" data-act="ok">确认</button>',
      });
      $('[data-act="cancel"]', m.root).onclick = () => { m.close(); resolve(false); };
      $('[data-act="ok"]', m.root).onclick = () => { m.close(); resolve(true); };
    });
  }

  /* ---------------------------------------------------------- 路由 */
  function go(page) {
    if (!PAGE_META[page]) return;
    state.page = page;
    if (location.hash !== '#' + page) {
      try { history.replaceState(null, '', '#' + page); } catch (e) { /* file:// 等场景忽略 */ }
    }
    $$('.nav-item').forEach((el) => el.classList.toggle('active', el.dataset.page === page));
    $$('.page').forEach((el) => el.classList.toggle('active', el.id === 'page-' + page));
    $('#page-title').textContent = PAGE_META[page].title;
    $('#page-sub').textContent = PAGE_META[page].sub;
    $('#sidebar').classList.remove('open');
    document.body.classList.remove('nav-open');

    if (page === 'rules') loadRules();
    if (page === 'cloud') { fillInstanceSelect(); pullCloud(true); }
    if (page === 'cron') loadCron();
    if (page === 'logs') loadLogs();
    if (page === 'settings') loadSettings();
    if (page === 'dingtalk') loadDingtalk();
    if (page === 'dashboard') loadOverview();
  }

  /* ---------------------------------------------------------- 仪表盘 */
  async function loadOverview() {
    try {
      const data = await api('/api/overview');
      state.overview = data;
      const s = data.stats || {};
      $('#stat-active').innerHTML = (s.active_rules || 0) + '<small>条</small>';
      $('#stat-ips').innerHTML = (s.unique_ips || 0) + '<small>个</small>';
      $('#stat-instances').innerHTML = (s.instances || 0) + '<small>台</small>';
      $('#stat-expiring').innerHTML = (s.expiring_soon || 0) + '<small>条</small>';

      const cronOk = data.cron && data.cron.running;
      $('#pill-cron').className = 'badge ' + (cronOk ? 'badge-green' : 'badge-red');
      $('#pill-cron').innerHTML = '<span class="dot ' + (cronOk ? 'green pulse' : 'red') + '"></span> ' +
        (cronOk ? '调度运行中' : '调度未运行');

      const bot = data.dingtalk || {};
      const botOk = bot.connected;
      $('#pill-bot').className = 'badge ' + (botOk ? 'badge-green' : 'badge-amber');
      $('#pill-bot').innerHTML = '<span class="dot ' + (botOk ? 'green pulse' : 'amber') + '"></span> 机器人' +
        (botOk ? '已连接' : (bot.enabled ? '未连接' : '未启用'));

      $('#svc-cron').className = 'badge ' + (cronOk ? 'badge-green' : 'badge-red');
      $('#svc-cron').textContent = cronOk ? ('运行中 · ' + (data.cron.job_count || 0) + ' 个任务') : '未运行';

      $('#svc-bot').className = 'badge ' + (botOk ? 'badge-green' : 'badge-amber');
      $('#svc-bot').textContent = botOk
        ? ('已连接 · 收到 ' + (bot.frames || 0) + ' 帧 / ' + (bot.messages || 0) + ' 条消息')
        : (bot.last_error || '未连接');

      const keyReady = data.aliyun && data.aliyun.key_ready;
      $('#svc-aliyun').className = 'badge ' + (keyReady ? 'badge-green' : 'badge-red');
      $('#svc-aliyun').textContent = keyReady ? (data.aliyun.region_id || '已配置') : '未配置';

      $('#svc-time').textContent = data.server_time || '';

      const badge = $('#nav-rule-badge');
      if (s.expiring_soon > 0) { badge.textContent = s.expiring_soon; badge.classList.add('show'); }
      else { badge.classList.remove('show'); }

      renderRecentLogs(data.recent_logs || []);
      renderRecentBot(data.recent_bot || []);
      renderSecurityBanner(data.security_warnings || []);
    } catch (e) {
      toast(e.message, 'error');
    }
  }

  function renderSecurityBanner(warnings) {
    const box = $('#security-banner');
    if (!box) return;
    if (!warnings.length) { box.innerHTML = ''; return; }
    const LEVEL = { high: ['var(--red)', 'badge-red'], medium: ['var(--amber)', 'badge-amber'], low: ['var(--text-soft)', 'badge'] };
    box.innerHTML = warnings.map((w) => {
      const lv = LEVEL[w.level] || LEVEL.low;
      return '<div class="hint-box warn" style="margin-bottom:10px;border-left:3px solid ' + lv[0] + '">' +
        '<span class="badge ' + lv[1] + '" style="margin-right:8px">' +
        (w.level === 'high' ? '高危' : w.level === 'medium' ? '提醒' : '建议') + '</span>' +
        esc(w.message) +
        (w.key === 'weak_password' || w.key === 'webhook_no_secret'
          ? ' <a href="#" data-goto="settings" style="color:var(--accent)">去设置 →</a>' : '') +
        '</div>';
    }).join('');
  }

  function renderRecentLogs(rows) {
    const box = $('#recent-logs');
    if (!rows.length) { box.innerHTML = '<div class="empty"><span class="big">≡</span>暂无操作记录</div>'; return; }
    box.innerHTML = rows.map((row) => {
      const color = row.status === 'failed' ? 'var(--red)' : (row.status === 'warning' ? 'var(--amber)' : 'var(--green)');
      return '<div class="list-item">' +
        '<div class="ico-circle" style="background:' + (row.status === 'failed' ? 'var(--red-soft)' : 'var(--accent-soft)') + ';color:' + color + '">•</div>' +
        '<div class="body"><div class="title">' + esc(row.action) + ' <span class="muted small">' + esc(row.target || '') + '</span></div>' +
        '<div class="text">' + esc(row.message || '') + '</div>' +
        '<div class="meta">' + esc(fmtTime(row.ts)) + '</div></div></div>';
    }).join('');
  }

  function renderRecentBot(rows) {
    const box = $('#recent-bot');
    if (!rows.length) { box.innerHTML = '<div class="empty"><span class="big">✆</span>暂无指令记录</div>'; return; }
    box.innerHTML = rows.map((row) => {
      return '<div class="list-item">' +
        '<div class="ico-circle">@</div>' +
        '<div class="body"><div class="title">' + esc(row.sender_nick || row.sender_id || '未知') +
        ' <code class="small">' + esc(row.text || '') + '</code></div>' +
        '<div class="text">' + esc((row.intent || '') + ' · ' + (row.status === 'denied' ? '已拒绝' : '已处理')) + '</div>' +
        '<div class="meta">' + esc(fmtTime(row.ts)) + '</div></div></div>';
    }).join('');
  }

  /* ---------------------------------------------------------- 规则 */
  async function loadRules() {
    try {
      const params = new URLSearchParams({
        status: state.ruleFilter,
        keyword: state.ruleKeyword,
        page: String(state.rulePage),
        page_size: String(state.rulePageSize),
      });
      if (state.ruleTag) params.set('tag', state.ruleTag);
      const data = await api('/api/rules?' + params.toString());
      state.rules = data.items || [];
      state.ruleTotal = data.total || 0;
      state.rulePages = data.pages || 1;
      state.ruleTags = data.tags || [];
      // 后端可能因为筛选后页码越界，回落到最后一页
      if (state.rulePage > state.rulePages && state.rulePage > 1) {
        state.rulePage = state.rulePages;
        return loadRules();
      }
      renderTagFilter();
      renderRules();
      renderPager();
      loadGeo();
    } catch (e) { toast(e.message, 'error'); }
  }

  function renderTagFilter() {
    const sel = $('#rule-tag');
    if (!sel) return;
    const tags = state.ruleTags || [];
    const current = state.ruleTag;
    sel.innerHTML = '<option value="">全部标签</option>' + tags.map((t) =>
      '<option value="' + esc(t.tag) + '">' + esc(t.tag) + '（' + (t.total || 0) + '）</option>').join('');
    if (current && !tags.some((t) => t.tag === current)) {
      // 标签刚被清空的情况，兜底保留当前选项，避免筛选条件悄悄失效
      sel.innerHTML += '<option value="' + esc(current) + '">' + esc(current) + '</option>';
    }
    sel.value = current;
  }

  function renderRules() {
    const tbody = $('#rules-tbody');
    const rows = state.rules || [];
    if (!rows.length) {
      const goneTab = state.ruleFilter === 'gone';
      tbody.innerHTML = '<tr><td colspan="9"><div class="empty"><span class="big">' +
        (goneTab ? '🗂' : '🛡') + '</span>' +
        (goneTab
          ? '没有已失效的记录，删除后的规则会保留在这里，可恢复到云端'
          : (state.ruleKeyword || state.ruleTag
            ? '没有匹配的规则，试试清空搜索或标签筛选'
            : '暂无规则，点击右上角「新增规则」或在钉钉群发送 IP')) +
        '</div></td></tr>';
      return;
    }
    tbody.innerHTML = rows.map((r) => {
      const isActive = r.status === 'active';
      const isGone = GONE_STATUS.indexOf(r.status) >= 0;
      let actions;
      if (isGone) {
        actions = '<button class="btn btn-sm btn-primary" data-act="restore" data-id="' + r.id + '">恢复到云端</button>' +
          '<button class="btn btn-sm btn-danger" data-act="purge" data-id="' + r.id + '">清理记录</button>';
      } else if (isActive) {
        actions = '<button class="btn btn-sm" data-act="renew" data-id="' + r.id + '">续期</button>' +
          '<button class="btn btn-sm' + (r.auto_renew ? ' btn-primary' : '') + '" data-act="auto" data-id="' + r.id +
          '" data-enable="' + (r.auto_renew ? '0' : '1') + '" title="开启后到期时自动探测公网 IP 并续期">' +
          (r.auto_renew ? '自动续期 开' : '自动续期') + '</button>' +
          '<button class="btn btn-sm" data-act="edit" data-id="' + r.id + '">编辑</button>' +
          '<button class="btn btn-sm" data-act="toggle" data-id="' + r.id + '" data-enable="0">禁用</button>' +
          '<button class="btn btn-sm btn-danger" data-act="del" data-id="' + r.id + '">删除</button>';
      } else {
        actions = '<button class="btn btn-sm" data-act="toggle" data-id="' + r.id + '" data-enable="1">启用</button>' +
          '<button class="btn btn-sm" data-act="del" data-id="' + r.id + '">删除</button>';
      }
      const tagHtml = r.tag
        ? '<span class="tag-chip" data-tag="' + esc(r.tag) + '" title="只看该标签">' + esc(r.tag) + '</span>'
        : '<span class="tag-chip none">—</span>';
      // /32、/128 这种单机掩码没有信息量，别占一行
      const showCidr = r.cidr && r.cidr !== r.ip
        && r.cidr !== r.ip + '/32' && r.cidr !== r.ip + '/128';
      return '<tr>' +
        '<td data-label="IP 地址"><div class="cell-ip">' + esc(r.ip) + '</div>' +
        (showCidr ? '<div class="cell-sub">' + esc(r.cidr) + '</div>' : '') +
        '<div class="cell-sub geo-hint" data-geo="' + esc(r.ip) + '"></div></td>' +
        '<td data-label="端口">' + esc(r.port_display || r.port) + '</td>' +
        '<td data-label="协议"><span class="badge">' + esc((r.protocol || 'tcp').toUpperCase()) + '</span></td>' +
        '<td data-label="实例">' + esc(r.instance_name || r.instance_id) + '</td>' +
        '<td data-label="标签">' + tagHtml + '</td>' +
        '<td data-label="来源"><span class="badge badge-accent">' + esc(sourceLabel(r.source)) + '</span></td>' +
        '<td data-label="状态">' + statusBadge(r.status) + '</td>' +
        '<td data-label="到期">' + esc(r.expires_at || '长期') +
        (r.auto_renew ? ' <span class="badge badge-accent" title="' + (r.renew_mode === 'same' ? '到期时原 IP 原地续期（不探测、不改云端）' : '到期时探测公网 IP，IP 变了自动跟随') + '">' + (r.renew_mode === 'same' ? '自动·原IP' : '自动·跟随') + '</span>' : '') +
        '</td>' +
        '<td><div class="actions">' + actions + '</div></td></tr>';
    }).join('');

    $$('#rules-tbody [data-act]').forEach((btn) => {
      btn.onclick = () => handleRuleAction(btn.dataset.act, Number(btn.dataset.id), btn.dataset.enable === '1');
    });
    $$('#rules-tbody .tag-chip[data-tag]').forEach((chip) => {
      chip.onclick = () => {
        state.ruleTag = chip.dataset.tag;
        state.rulePage = 1;
        loadRules();
      };
    });
    applyGeo();
  }

  /* 分页条：首末页 + 当前页前后各 2 页 */
  function renderPager() {
    const box = $('#rules-pager');
    if (!box) return;
    const total = state.ruleTotal || 0;
    if (!total) { box.innerHTML = ''; return; }
    const pages = state.rulePages || 1;
    const cur = state.rulePage || 1;

    const nums = [];
    for (let p = 1; p <= pages; p += 1) {
      if (p === 1 || p === pages || Math.abs(p - cur) <= 2) nums.push(p);
      else if (nums[nums.length - 1] !== '...') nums.push('...');
    }

    const from = (cur - 1) * state.rulePageSize + 1;
    const to = Math.min(total, cur * state.rulePageSize);

    box.innerHTML =
      '<span class="info">共 ' + total + ' 条 · 第 ' + from + '-' + to + ' 条</span>' +
      '<span class="spacer"></span>' +
      '<button class="page-btn" data-page="' + (cur - 1) + '"' + (cur <= 1 ? ' disabled' : '') + '>上一页</button>' +
      nums.map((n) => (n === '...'
        ? '<span class="info">…</span>'
        : '<button class="page-btn' + (n === cur ? ' active' : '') + '" data-page="' + n + '">' + n + '</button>')).join('') +
      '<button class="page-btn" data-page="' + (cur + 1) + '"' + (cur >= pages ? ' disabled' : '') + '>下一页</button>' +
      '<select class="select" id="rule-page-size">' +
      [10, 20, 50, 100].map((n) =>
        '<option value="' + n + '"' + (n === state.rulePageSize ? ' selected' : '') + '>' + n + ' 条/页</option>').join('') +
      '</select>';

    $$('#rules-pager [data-page]').forEach((btn) => {
      btn.onclick = () => {
        const p = Number(btn.dataset.page);
        if (!p || p === state.rulePage) return;
        state.rulePage = Math.max(1, Math.min(state.rulePages, p));
        loadRules();
      };
    });
    const sizeSel = $('#rule-page-size');
    if (sizeSel) {
      sizeSel.onchange = () => {
        state.rulePageSize = Number(sizeSel.value) || 20;
        state.rulePage = 1;
        loadRules();
      };
    }
  }

  /* IP 归属地：只查当前页里还没缓存过的，失败静默 */
  async function loadGeo() {
    const cfg = (state.settings || {}).geo || {};
    if (cfg.enabled === false) return;
    const ips = (state.rules || []).map((r) => r.ip).filter((ip) => ip && !(ip in state.geo));
    if (!ips.length) return;
    try {
      const data = await api('/api/geo/lookup', { method: 'POST', body: { ips: ips.slice(0, 40) } });
      Object.keys(data || {}).forEach((ip) => { state.geo[ip] = data[ip]; });
      applyGeo();
    } catch (e) { /* 归属地查不到不影响主流程 */ }
  }

  function geoText(info) {
    if (!info) return '';
    const region = [info.region, info.city].filter(Boolean).join(' ');
    const head = region || info.country || '';
    return [head, info.isp].filter(Boolean).join(' · ');
  }

  function applyGeo() {
    $$('#rules-tbody .geo-hint').forEach((el) => {
      const text = geoText(state.geo[el.dataset.geo]);
      el.textContent = text;
      el.title = text;
    });
  }


  function sourceLabel(source) {
    const map = { dingtalk: '钉钉', webhook: 'Webhook', web: '手动', cron: '定时', 'cloud-sync': '同步', cloud: '云端', public_ip: '公网IP' };
    return map[source] || source || '手动';
  }

  async function handleRuleAction(act, id, enable) {
    const rule = state.rules.find((r) => r.id === id);
    const label = rule ? rule.ip + ':' + (rule.port_display || rule.port) : '该规则';
    try {
      if (act === 'toggle') {
        await api('/api/rules/' + id + '/toggle', { method: 'POST', body: { enable: enable } });
        toast(enable ? '已启用规则' : '已禁用规则');
      } else if (act === 'del') {
        const okToDelete = await confirmDialog('删除规则', '将从云端删除 ' + label + '，本地保留记录可随时恢复，确认继续？', true);
        if (!okToDelete) return;
        await api('/api/rules/' + id, { method: 'DELETE' });
        loadRules();
        loadOverview();
        toast('已删除，可在「已失效」中恢复到云端');
        return;
      } else if (act === 'renew') {
        openRenewModal(rule);
        return;
      } else if (act === 'auto') {
        const res = await api('/api/rules/' + id, { method: 'PUT', body: { auto_renew: enable } });
        toast(res.message || (enable ? '已开启自动续期' : '已关闭自动续期'));
        loadRules();
        return;
      } else if (act === 'restore') {
        const okToRestore = await confirmDialog('恢复到云端', '将把 ' + label + ' 重新下发到阿里云防火墙，确认继续？', false);
        if (!okToRestore) return;
        const res = await api('/api/rules/' + id + '/restore', { method: 'POST' });
        toast(res.message || '已恢复到云端');
      } else if (act === 'purge') {
        const okToPurge = await confirmDialog('清理本地记录',
          '仅从本地数据库移除 ' + label + ' 这条记录，不会影响云端。移除后无法再恢复，确认继续？', true);
        if (!okToPurge) return;
        await api('/api/rules/' + id + '/purge', { method: 'DELETE' });
        loadRules();
        toast('已从本地记录中删除');
        return;
      } else if (act === 'edit') {
        openRuleModal(rule);
        return;
      }
      loadRules();
      loadOverview();
    } catch (e) { toast(e.message, 'error'); }
  }

  /* 续期弹窗：常用时长一键选 + 自定义分钟数 */
  const RENEW_PRESETS = [
    { label: '30 分钟', minutes: 30 },
    { label: '1 小时', minutes: 60 },
    { label: '24 小时', minutes: 1440 },
    { label: '7 天', minutes: 10080 },
    { label: '30 天', minutes: 43200 },
  ];

  function openRenewModal(rule) {
    if (!rule) return;
    const m = openModal({
      title: '续期规则',
      body:
        '<div class="hint-box" style="margin-bottom:14px">' +
        '<code>' + esc(rule.ip) + '</code> : ' + esc(rule.port_display || rule.port) +
        '　当前到期 ' + esc(rule.expires_at || '长期') +
        '<br>续期从现在开始重新计时（不是叠加在原到期时间上）。</div>' +
        '<div class="field"><label>延长多久</label>' +
        '<div class="port-presets" id="renew-presets">' +
        RENEW_PRESETS.map((p, i) =>
          '<button type="button" data-min="' + p.minutes + '"' + (i === 2 ? ' class="active"' : '') + '>' + p.label + '</button>').join('') +
        '</div></div>' +
        '<div class="field" style="margin-top:12px"><label>自定义（分钟）</label>' +
        '<input class="input" id="renew-minutes" type="number" min="1" value="1440"></div>',
      footer: '<button class="btn" data-act="cancel">取消</button>' +
        '<button class="btn btn-primary" data-act="ok">确认续期</button>',
    });
    $('[data-act="cancel"]', m.root).onclick = m.close;
    $$('#renew-presets button', m.root).forEach((btn) => {
      btn.onclick = () => {
        $('#renew-minutes', m.root).value = btn.dataset.min;
        $$('#renew-presets button', m.root).forEach((b) => b.classList.toggle('active', b === btn));
      };
    });
    $('[data-act="ok"]', m.root).onclick = async () => {
      const minutes = Number($('#renew-minutes', m.root).value || 0);
      if (!minutes || minutes < 1) { toast('请填写有效的续期时长', 'warn'); return; }
      try {
        const res = await api('/api/rules/' + rule.id + '/renew', { method: 'POST', body: { minutes: minutes } });
        toast(res.message || '已续期');
        m.close();
        loadRules();
        loadOverview();
      } catch (e) { toast(e.message, 'error'); }
    };
  }

  function instanceOptions(selected) {
    const list = (state.settings && state.settings.aliyun && state.settings.aliyun.instances) || [];
    if (!list.length) return '<option value="">请先在「系统设置」中添加实例</option>';
    return list.map((i) =>
      '<option value="' + esc(i.instance_id) + '"' + (i.instance_id === selected ? ' selected' : '') + '>' +
      esc(i.name || i.instance_id) + '</option>').join('');
  }

  /* 协议下拉：必须包含 TCP+UDP，否则编辑一条 TCP+UDP 规则时
     会被 select 默认拉到 TCP，保存后 UDP 业务悄悄断掉 */
  const PROTOCOLS = ['tcp', 'udp', 'tcp+udp', 'icmp'];

  function protocolOptions(current) {
    const value = String(current || 'tcp').toLowerCase();
    return PROTOCOLS.map((p) =>
      '<option value="' + p + '"' + (p === value ? ' selected' : '') + '>' + p.toUpperCase() + '</option>').join('');
  }

  /* 端口回填：1/65535 直接显示成 ALL，避免用户看不懂 */
  function portInputValue(port) {
    const text = String(port || '').trim();
    return (!text || text === '1/65535' || text.toUpperCase() === 'ALL') ? 'ALL' : text.replace('/', '-');
  }

  const PORT_PRESETS = [
    { label: '全部端口', value: 'ALL' },
    { label: 'SSH 22', value: '22' },
    { label: 'Web 80,443', value: '80,443' },
    { label: 'RDP 3389', value: '3389' },
    { label: 'MySQL 3306', value: '3306' },
    { label: 'Redis 6379', value: '6379' },
  ];

  function tagDatalist() {
    const tags = (state.ruleTags || []).map((t) => t.tag);
    if (!tags.length) return '<datalist id="tag-options"></datalist>';
    return '<datalist id="tag-options">' +
      tags.map((t) => '<option value="' + esc(t) + '"></option>').join('') + '</datalist>';
  }

  function instanceCheckboxes() {
    const list = (state.settings && state.settings.aliyun && state.settings.aliyun.instances) || [];
    const usable = list.filter((i) => i.instance_id && i.enabled !== false);
    if (!usable.length) return '<div class="hint-box warn">还没有可用实例，请先到「系统设置 → 纳管实例」添加。</div>';
    return '<div class="inst-checks">' + usable.map((i, idx) =>
      '<label><input type="checkbox" data-inst="' + esc(i.instance_id) + '"' + (idx === 0 ? ' checked' : '') + '>' +
      '<span>' + esc(i.name || i.instance_id) + '</span>' +
      '<span class="mono" style="margin-left:auto">' + esc(i.instance_id) + '</span></label>').join('') + '</div>';
  }

  function openRuleModal(rule) {
    const isEdit = !!rule;
    const m = openModal({
      title: isEdit ? '编辑放行规则' : '新增放行规则',
      wide: !isEdit,
      body:
        (isEdit
          ? '<div class="field"><label>目标实例</label><select class="select" id="m-instance" disabled>' +
            instanceOptions(rule.instance_id) + '</select></div>'
          : '<div class="field"><label>目标实例（可多选，一次下发到多台）</label>' + instanceCheckboxes() + '</div>') +
        '<div class="form-row"><div class="field"><label>IP 地址</label>' +
        '<input class="input" id="m-ip" placeholder="1.2.3.4 或 2001:db8::1" value="' + esc(rule ? rule.ip : '') + '"></div>' +
        '<div class="field"><label>端口</label><input class="input" id="m-ports" placeholder="ALL / 22 / 80,443 / 8000-9000" value="' +
        esc(rule ? portInputValue(rule.port) : 'ALL') + '"></div></div>' +
        '<div class="port-presets" id="m-port-presets">' +
        PORT_PRESETS.map((p) => '<button type="button" data-port="' + esc(p.value) + '">' + p.label + '</button>').join('') +
        '</div>' +
        '<div class="form-row"><div class="field"><label>协议</label><select class="select" id="m-protocol">' +
        protocolOptions(rule ? rule.protocol : 'tcp') +
        '</select></div><div class="field"><label>有效期（分钟，0 长期）</label>' +
        '<input class="input" id="m-ttl" type="number" min="0" placeholder="1440"></div></div>' +
        '<div class="form-row"><div class="field"><label>标签</label>' +
        '<input class="input" id="m-tag" list="tag-options" maxlength="32" placeholder="例如：家里 / 客户A / 临时" value="' +
        esc(rule ? rule.tag : '') + '">' + tagDatalist() +
        '<div class="help">标签用于分组筛选，同一标签可跨实例。</div></div>' +
        '<div class="field"><label>备注</label><input class="input" id="m-desc" placeholder="例如：家里宽带" value="' +
        esc(rule ? rule.description : '') + '"></div></div>' +
        '<div class="field"><label class="switch"><input type="checkbox" id="m-auto-renew"' +
        (rule && rule.auto_renew ? ' checked' : '') +
        '><span class="track"></span>到期自动续期</label>' +
        '<div class="help">开启后，规则到期时会自动续期，续期方式见下方选项。</div></div>' +
        '<div class="field" id="m-renew-mode-field"' + (rule && rule.auto_renew ? '' : ' style="opacity:.5;pointer-events:none"') + '>' +
        '<label>续期方式</label>' +
        '<select class="select" id="m-renew-mode">' +
        '<option value="follow"' + ((rule ? (rule.renew_mode || 'follow') : 'follow') === 'follow' ? ' selected' : '') + '>跟随新 IP（探测当前公网 IP，变了自动替换）</option>' +
        '<option value="same"' + ((rule ? (rule.renew_mode || 'follow') : 'follow') === 'same' ? ' selected' : '') + '>原 IP 续期（保持当前 IP，仅顺延有效期，不探测、不改云端）</option>' +
        '</select>' +
        '<div class="help">「原 IP 续期」适合固定 IP / 静态办公网段；「跟随新 IP」适合家庭宽带等动态 IP。</div></div>',
      footer: '<button class="btn" data-act="cancel">取消</button>' +
        '<button class="btn btn-primary" data-act="save">' + (isEdit ? '保存' : '创建') + '</button>',
    });

    $$('#m-port-presets button', m.root).forEach((btn) => {
      btn.onclick = () => { $('#m-ports', m.root).value = btn.dataset.port; };
    });

    const autoRenewEl = $('#m-auto-renew', m.root);
    if (autoRenewEl) {
      autoRenewEl.onchange = (e) => {
        const f = $('#m-renew-mode-field', m.root);
        if (!f) return;
        f.style.opacity = e.target.checked ? '1' : '.5';
        f.style.pointerEvents = e.target.checked ? 'auto' : 'none';
      };
    }

    $('[data-act="cancel"]', m.root).onclick = m.close;
    $('[data-act="save"]', m.root).onclick = async () => {
      const payload = {
        ip: $('#m-ip', m.root).value.trim(),
        ports: $('#m-ports', m.root).value.trim() || 'ALL',
        protocol: $('#m-protocol', m.root).value,
        description: $('#m-desc', m.root).value.trim(),
        tag: $('#m-tag', m.root).value.trim(),
        ttl_minutes: Number($('#m-ttl', m.root).value || 0),
        auto_renew: $('#m-auto-renew', m.root).checked,
        renew_mode: $('#m-renew-mode', m.root).value || 'follow',
      };
      if (isEdit) {
        payload.instance_id = rule.instance_id;
      } else {
        payload.instance_ids = $$('.inst-checks [data-inst]', m.root)
          .filter((el) => el.checked).map((el) => el.dataset.inst);
        if (!payload.instance_ids.length) { toast('请至少选择一个实例', 'warn'); return; }
      }
      if (!payload.ip) { toast('请填写 IP 地址', 'warn'); return; }
      try {
        if (isEdit) await api('/api/rules/' + rule.id, { method: 'PUT', body: payload });
        else await api('/api/rules', { method: 'POST', body: payload });
        toast(isEdit ? '已保存' : '规则已创建');
        m.close();
        loadRules();
        loadOverview();
      } catch (e) { toast(e.message, 'error'); }
    };
  }

  /* ---------------------------------------------------------- 一键放行 */
  async function detectIP() {
    try {
      const data = await api('/api/ip/current');
      $('#ip-input').value = data.ip;
      showIPResult('检测到公网 IP：<b>' + esc(data.ip) + '</b>　来源 ' + esc(data.source || '-'), '');
    } catch (e) { toast(e.message, 'error'); }
  }

  function showIPResult(html, kind) {
    const box = $('#ip-result');
    box.style.display = 'block';
    box.className = 'hint-box mt-16' + (kind === 'warn' ? ' warn' : '');
    box.innerHTML = html;
  }

  async function quickAllow() {
    const btn = $('#btn-quick-allow');
    btn.disabled = true;
    btn.textContent = '处理中…';
    try {
      const payload = {
        ip: $('#ip-input').value.trim(),
        ports: $('#ip-ports').value.trim(),
        ttl_minutes: $('#ip-ttl').value.trim(),
        via_webhook: true,
      };
      const data = await api('/api/ip/allow', { method: 'POST', body: payload });
      const webhook = data.webhook || {};
      const results = (webhook.results && (webhook.results.IPv4 || webhook.results.IPv6)) || {};
      const summary = results.summary || {};
      showIPResult(
        '<b>放行完成</b>　IP <code>' + esc(data.ip) + '</code>（来源 ' + esc(data.source || '手动') + '）<br>' +
        '新增 ' + (summary.created || 0) + ' 条 · 已存在 ' + (summary.skipped || 0) + ' 条 · 失败 ' + (summary.failed || 0) + ' 条',
        summary.failed ? 'warn' : '');
      toast(webhook.message || '已放行');
      loadOverview();
      if (state.page === 'rules') loadRules();
    } catch (e) {
      showIPResult('放行失败：' + esc(e.message), 'warn');
      toast(e.message, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = '立即放行';
    }
  }

  /* ---------------------------------------------------------- 云端 */
  function fillInstanceSelect() {
    const sel = $('#cloud-instance');
    const list = (state.settings && state.settings.aliyun && state.settings.aliyun.instances) || [];
    const current = sel.value;
    sel.innerHTML = '<option value="">全部实例</option>' + list.map((i) =>
      '<option value="' + esc(i.instance_id) + '">' + esc(i.name || i.instance_id) + '</option>').join('');
    if (current) sel.value = current;
  }

  async function pullCloud(silent) {
    const instanceId = $('#cloud-instance').value;
    const tbody = $('#cloud-tbody');
    tbody.innerHTML = '<tr><td colspan="7"><div class="empty">正在从云端读取…</div></td></tr>';
    try {
      let rules = [];
      if (instanceId) {
        const data = await api('/api/cloud/rules?instance_id=' + encodeURIComponent(instanceId));
        rules = data.rules || [];
      } else {
        const data = await api('/api/cloud/rules');
        (data.instances || []).forEach((item) => {
          (item.rules || []).forEach((r) => { r.instance_name = item.instance_name; rules.push(r); });
        });
        if ((data.errors || []).length) {
          toast(data.errors.map((e) => e.instance_id + ': ' + e.message).join('; '), 'error');
        }
      }
      state.cloud = rules;
      $('#cloud-count').textContent = '共 ' + rules.length + ' 条云端规则';
      renderCloud();
      if (!silent) toast('已拉取 ' + rules.length + ' 条规则');
    } catch (e) {
      tbody.innerHTML = '<tr><td colspan="7"><div class="empty">读取失败：' + esc(e.message) + '</div></td></tr>';
      toast(e.message, 'error');
    }
  }

  function renderCloud() {
    const tbody = $('#cloud-tbody');
    if (!state.cloud.length) {
      tbody.innerHTML = '<tr><td colspan="7"><div class="empty"><span class="big">☁</span>云端没有防火墙规则</div></td></tr>';
      return;
    }
    tbody.innerHTML = state.cloud.map((r, idx) => {
      const rawPort = String(r.port || 'ALL');
      const portText = (rawPort.toUpperCase() === 'ALL' || rawPort === '1/65535')
        ? '全部端口' : rawPort.replace('/', '-');
      return '<tr>' +
        '<td data-label="来源 IP"><div class="cell-ip">' + esc(r.ip || r.cidr) + '</div></td>' +
        '<td data-label="端口">' + esc(portText) + '</td>' +
        '<td data-label="协议"><span class="badge">' + esc((r.protocol || 'tcp').toUpperCase()) + '</span></td>' +
        '<td data-label="备注" class="cell-sub">' + esc(r.description || '—') + '</td>' +
        '<td data-label="实例">' + esc(r.instance_name || r.instance_id) + '</td>' +
        '<td data-label="Rule ID" class="cell-sub mono">' + esc((r.rule_id || '').slice(0, 16)) + '</td>' +
        '<td><div class="actions">' +
        '<button class="btn btn-sm" data-act="edit" data-idx="' + idx + '">编辑</button>' +
        '<button class="btn btn-sm btn-danger" data-act="del" data-idx="' + idx + '">删除</button>' +
        '</div></td></tr>';
    }).join('');

    $$('#cloud-tbody [data-act]').forEach((btn) => {
      const rule = state.cloud[Number(btn.dataset.idx)];
      btn.onclick = async () => {
        if (btn.dataset.act === 'del') {
          const okToDelete = await confirmDialog('删除云端规则', '确认删除 ' + rule.ip + ' : ' + rule.port + ' 的防火墙规则？', true);
          if (!okToDelete) return;
          try {
            await api('/api/cloud/rule-action', {
              method: 'POST',
              body: { instance_id: rule.instance_id, rule_id: rule.rule_id, action: 'delete', ip: rule.ip, port: rule.port },
            });
            toast('已删除');
            pullCloud(true);
          } catch (e) { toast(e.message, 'error'); }
        } else {
          editCloudRule(rule);
        }
      };
    });
  }

  function editCloudRule(rule) {
    const m = openModal({
      title: '编辑云端规则',
      body:
        '<div class="hint-box" style="margin-bottom:14px">修改会先调用云端 ModifyFirewallRule，失败时自动删除重建。</div>' +
        '<div class="form-row"><div class="field"><label>来源 IP</label>' +
        '<input class="input" id="c-ip" value="' + esc(rule.ip) + '"></div>' +
        '<div class="field"><label>端口</label><input class="input" id="c-ports" value="' + esc(portInputValue(rule.port)) + '"></div></div>' +
        '<div class="field"><label>协议</label><select class="select" id="c-protocol">' +
        protocolOptions(rule.protocol) +
        '</select></div>' +
        '<div class="field"><label>备注</label><input class="input" id="c-desc" value="' + esc(rule.description || '') + '"></div>',
      footer: '<button class="btn" data-act="cancel">取消</button><button class="btn btn-primary" data-act="save">保存</button>',
    });
    $('[data-act="cancel"]', m.root).onclick = m.close;
    $('[data-act="save"]', m.root).onclick = async () => {
      try {
        await api('/api/cloud/rule-action', {
          method: 'POST',
          body: {
            instance_id: rule.instance_id,
            rule_id: rule.rule_id,
            action: 'update',
            ip: $('#c-ip', m.root).value.trim(),
            ports: $('#c-ports', m.root).value.trim() || 'ALL',
            protocol: $('#c-protocol', m.root).value,
            description: $('#c-desc', m.root).value.trim(),
          },
        });
        toast('已更新');
        m.close();
        pullCloud(true);
      } catch (e) { toast(e.message, 'error'); }
    };
  }

  /* ---------------------------------------------------------- 差异视图 */
  async function loadDiff() {
    const body = $('#diff-body');
    const instanceId = $('#cloud-instance').value;
    const summary = $('#diff-summary');
    body.innerHTML = '<div class="empty">正在比对本地镜像与云端规则…</div>';
    summary.className = 'badge';
    summary.textContent = '比对中';
    try {
      const data = await api('/api/cloud/diff' + (instanceId ? '?instance_id=' + encodeURIComponent(instanceId) : ''));
      const list = data.instances || (data.instance_id ? [data] : []);
      renderDiff(list, data.errors || []);
    } catch (e) {
      body.innerHTML = '<div class="empty">比对失败：' + esc(e.message) + '</div>';
      summary.className = 'badge badge-red';
      summary.textContent = '比对失败';
    }
  }

  function diffPort(item) {
    const raw = String(item.port || 'ALL');
    if (raw.toUpperCase() === 'ALL' || raw === '1/65535') return '全部端口';
    return raw.replace('/', '-').replace('1-65535', '全部端口');
  }

  function renderDiff(list, errors) {
    const body = $('#diff-body');
    const summary = $('#diff-summary');
    if (!list.length) {
      if (errors.length) {
        body.innerHTML = '<div class="empty"><span class="big">⚠</span>云端读取失败，无法比对</div>' +
          '<div class="hint-box warn mt-16">' +
          errors.map((e) => esc(e.instance_id + ': ' + e.message)).join('<br>') + '</div>';
        summary.className = 'badge badge-red';
        summary.textContent = '读取失败';
        return;
      }
      body.innerHTML = '<div class="empty">没有可比对的实例，请先在「系统设置」中纳管实例</div>';
      summary.className = 'badge badge-amber';
      summary.textContent = '无实例';
      return;
    }

    let totalDiff = 0;
    const html = list.map((inst) => {
      const l = inst.only_local || [];
      const c = inst.only_cloud || [];
      const d = inst.changed || [];
      totalDiff += l.length + c.length + d.length;

      function col(title, cls, items, emptyText, render) {
        return '<div class="diff-col">' +
          '<div class="diff-head"><span class="' + cls + '">' + title + '</span>' +
          '<span class="badge ' + (items.length ? 'badge-amber' : 'badge-green') + '">' + items.length + '</span></div>' +
          '<div class="diff-list">' +
          (items.length ? items.slice(0, 60).map(render).join('') +
            (items.length > 60 ? '<div class="diff-empty">还有 ' + (items.length - 60) + ' 条未显示</div>' : '')
            : '<div class="diff-empty">' + emptyText + '</div>') +
          '</div></div>';
      }

      return '<div class="diff-inst">' +
        '<div class="diff-inst-title">' +
        '<span>' + esc(inst.instance_name || inst.instance_id) + '</span>' +
        '<span class="badge">本地 ' + (inst.local_total || 0) + ' · 云端 ' + (inst.cloud_total || 0) + '</span>' +
        (inst.healthy ? '<span class="badge badge-green">一致</span>' : '<span class="badge badge-amber">有差异</span>') +
        '</div>' +
        '<div class="diff-grid">' +
        col('本地有 · 云端没有', 'diff-badge-l', l, '没有漂移',
          (it) => '<div class="diff-item"><code>' + esc(it.ip) + ' : ' + esc(diffPort(it)) + '</code>' +
            '<span class="badge badge-red" title="本地认为生效，云端已被删除">漂移</span></div>') +
        col('云端有 · 本地没有', 'diff-badge-c', c, '没有外部新增',
          (it) => '<div class="diff-item"><code>' + esc(it.ip || it.cidr) + ' : ' + esc(diffPort(it)) + '</code>' +
            '<span class="badge badge-blue" title="可能是控制台手工创建，点「从云端同步」纳入">外部</span></div>') +
        col('字段不一致', 'diff-badge-d', d, '没有变更',
          (it) => '<div class="diff-item" style="flex-direction:column;align-items:flex-start;gap:2px">' +
            '<code>本地 ' + esc(it.local.ip) + ' : ' + esc(diffPort(it.local)) + '</code>' +
            '<code>云端 ' + esc(it.cloud.ip) + ' : ' + esc(diffPort(it.cloud)) + '</code></div>') +
        '</div></div>';
    }).join('');

    body.innerHTML = html +
      (errors.length ? '<div class="hint-box warn mt-16"><b>部分实例读取失败</b><br>' +
        errors.map((e) => esc(e.instance_id + ': ' + e.message)).join('<br>') + '</div>' : '') +
      (totalDiff
        ? '<div class="hint-box mt-16">漂移说明云端规则被人手工改过或被其他系统删掉了：<b>「本地有云端没有」</b>可以在规则页点「恢复到云端」重建；' +
          '<b>「云端有本地没有」</b>点上方「从云端同步」纳入纳管。</div>'
        : '<div class="hint-box mt-16">本地镜像与云端完全一致，没有发现漂移。</div>');

    summary.className = 'badge ' + (totalDiff ? 'badge-amber' : 'badge-green');
    summary.textContent = totalDiff ? ('发现 ' + totalDiff + ' 处差异') : '完全一致';
  }

  /* ---------------------------------------------------------- 钉钉 */
  async function loadDingtalk() {
    try {
      if (!state.settings) state.settings = await api('/api/settings');
      const cfg = state.settings.dingtalk || {};
      const status = await api('/api/dingtalk/status');

      const connected = !!status.connected;
      const connecting = !!status.thread_alive && !connected;
      $('#dt-conn-text').textContent = connected ? '已连接' : (connecting ? '连接中' : (status.enabled ? '未连接' : '未启用'));
      $('#dt-conn-text').style.color = connected ? 'var(--green)' : 'var(--amber)';
      $('#dt-conn-hint').textContent = connected
        ? ('链路就绪 · ' + (status.connected_at || ''))
        : (status.last_error || '等待建立长连接');
      $('#dt-messages').innerHTML = (status.messages || 0) + '<small>条</small>';
      $('#dt-last-message').textContent = '收到原始帧 ' + (status.frames || 0) +
        ' · 最后 ' + (status.last_frame_at || '—');
      $('#dt-mode').textContent = status.mode === 'stream' ? 'Stream' : 'HTTP 回调';
      state.dingtalkStatus = status;

      $('#dt-app-id').value = cfg.app_id || '';
      $('#dt-agent-id').value = cfg.agent_id || '';
      $('#dt-client-id').value = cfg.client_id || '';
      $('#dt-client-secret').value = cfg.client_secret || '';
      $('#dt-robot-code').value = cfg.robot_code || '';
      $('#dt-mode-select').value = cfg.mode || 'stream';
      $('#dt-enabled').checked = !!cfg.enabled;
      $('#dt-default-ports').value = cfg.default_ports || 'ALL';
      $('#dt-default-protocol').value = cfg.default_protocol || 'tcp';
      $('#dt-allowed-conv').value = (cfg.allowed_conversations || []).join('\n');
      $('#dt-allowed-user').value = (cfg.allowed_users || []).join('\n');
      $('#dt-require-at').checked = cfg.require_at !== false;
      $('#dt-notify-webhook').checked = !!cfg.notify_on_webhook;
      $('#dt-notify-conv').value = cfg.notify_conversation_id || '';
      $('#dt-test-conv').value = cfg.notify_conversation_id || '';
    } catch (e) { toast(e.message, 'error'); }
  }

  async function saveDingtalk() {
    const patch = {
      dingtalk: {
        app_id: $('#dt-app-id').value.trim(),
        agent_id: $('#dt-agent-id').value.trim(),
        client_id: $('#dt-client-id').value.trim(),
        client_secret: $('#dt-client-secret').value,
        robot_code: $('#dt-robot-code').value.trim(),
        mode: $('#dt-mode-select').value,
        enabled: $('#dt-enabled').checked,
        default_ports: $('#dt-default-ports').value.trim() || 'ALL',
        default_protocol: $('#dt-default-protocol').value,
        allowed_conversations: $('#dt-allowed-conv').value.split('\n').map((s) => s.trim()).filter(Boolean),
        allowed_users: $('#dt-allowed-user').value.split('\n').map((s) => s.trim()).filter(Boolean),
        require_at: $('#dt-require-at').checked,
        notify_on_webhook: $('#dt-notify-webhook').checked,
        notify_conversation_id: $('#dt-notify-conv').value.trim(),
      },
    };
    try {
      state.settings = await api('/api/settings', { method: 'PUT', body: patch });
      toast('钉钉配置已保存');
      loadDingtalk();
    } catch (e) { toast(e.message, 'error'); }
  }

  /* 从指令记录里取会话 ID（openConversationId 只能由机器人收到消息后获得） */
  async function pickConversation(targetId) {
    let rows = [];
    try {
      rows = await api('/api/dingtalk/conversations');
    } catch (e) { toast(e.message, 'error'); return; }

    if (!rows.length) {
      toast('还没有收到过群消息：请先在群里 @机器人 发一条「帮助」', 'warn');
      return;
    }
    if (rows.length === 1) {
      $('#' + targetId).value = rows[0].conversation_id;
      toast('已填入最近的会话');
      return;
    }
    const body = '<div class="stack">' + rows.map((row, i) =>
      '<button class="job" style="text-align:left;cursor:pointer;width:100%;background:#fff;color:inherit;font-family:inherit" data-cid="' + esc(row.conversation_id) + '">' +
      '<div class="row-between"><div style="font-weight:600;font-size:13px">' +
      esc(row.conversation_title || '未命名群') + '</div>' +
      '<span class="badge badge-blue">' + (row.messages || 0) + ' 条</span></div>' +
      '<div class="small muted mono" style="margin-top:4px">' + esc(row.conversation_id) + '</div>' +
      '<div class="small muted">最近消息 ' + esc(fmtTime(row.last_ts)) + '</div>' +
      '</button>').join('') + '</div>' +
      '<div class="hint-box mt-16">列表来自机器人实际收到过的群消息，选一个即可。</div>';
    const m = openModal({
      title: '选择目标会话', body: body, wide: true,
      footer: '<button class="btn" data-act="cancel">取消</button>',
    });
    $('[data-act="cancel"]', m.root).onclick = m.close;
    $$('[data-cid]', m.root).forEach((btn) => {
      btn.onclick = () => {
        $('#' + targetId).value = btn.dataset.cid;
        m.close();
        toast('已填入会话 ID');
      };
    });
  }

  async function runDiagnose() {
    let data;
    try {
      data = await api('/api/dingtalk/diagnose', { method: 'POST' });
    } catch (e) { toast(e.message, 'error'); return; }

    const checks = (data.checks || []).map((c) =>
      '<div class="row-between" style="padding:8px 0;border-bottom:1px solid var(--border)">' +
      '<div><div style="font-weight:600;font-size:13px">' + esc(c.name) + '</div>' +
      '<div class="small muted mono" style="word-break:break-all">' + esc(c.detail || '') + '</div></div>' +
      '<span class="badge ' + (c.ok ? 'badge-green' : 'badge-red') + '">' + (c.ok ? '通过' : '异常') + '</span>' +
      '</div>').join('');

    const tips = (data.tips || []).length
      ? '<div class="hint-box warn mt-16"><b>下一步</b><br>' +
        data.tips.map((t) => '· ' + esc(t)).join('<br>') + '</div>'
      : '<div class="hint-box mt-16">链路正常，可以在群里 @机器人 试试了。</div>';

    openModal({
      title: '钉钉链路诊断',
      wide: true,
      body: checks + tips +
        '<div class="hint-box mt-16">帧日志见 <code>logs/app.log</code>（关键字：Stream 收到帧）。</div>',
      footer: '<button class="btn" data-act="close">关闭</button>',
    });
    const root = $('#modal-root');
    const closeBtn = root.querySelector('[data-act="close"]');
    if (closeBtn) closeBtn.onclick = () => { root.innerHTML = ''; };
  }

  /* ---------------------------------------------------------- 定时任务 */
  async function loadCron() {
    try {
      const data = await api('/api/cron/status');
      state.cron = data;
      $('#cron-summary').textContent = (data.running ? '调度运行中' : '调度未运行') +
        ' · ' + (data.running_jobs || 0) + '/' + (data.job_count || 0) + ' 个任务 · ' + (data.timezone || '');

      $('#cron-jobs').innerHTML = (data.jobs || []).map((job) => {
        const dot = job.last_status === 'failed' ? 'red' : (job.scheduled ? 'green' : 'amber');
        return '<div class="job">' +
          '<div class="job-head"><span class="dot ' + dot + '"></span>' +
          '<div><div class="job-name">' + esc(job.name) + '</div>' +
          '<div class="job-desc">' + esc(job.description) + '</div></div></div>' +
          '<div class="job-meta">' +
          '<span class="cron-chip">' + esc(job.cron) + '</span>' +
          '<span>下次 ' + esc(job.next_run ? job.next_run.slice(5) : '—') + '</span>' +
          '<span>上次 ' + esc(job.last_run ? job.last_run.slice(5) : '—') + '</span>' +
          '</div>' +
          '<div class="job-cron-desc">⏱ ' + esc(job.cron_desc || '') + '</div>' +
          '<div class="job-desc" style="min-height:34px">' + esc(job.last_message || '尚未执行') + '</div>' +
          '<div class="job-foot mt-16">' + statusBadge(job.last_status || 'skipped') +
          '<button class="btn btn-sm" style="margin-left:auto" data-plan="' + esc(job.id) + '">配置计划</button>' +
          '<button class="btn btn-sm" data-run="' + esc(job.id) + '">立即执行</button></div>' +
          '</div>';
      }).join('');

      $$('#cron-jobs [data-plan]').forEach((btn) => {
        btn.onclick = () => {
          const job = (state.cron.jobs || []).find((j) => j.id === btn.dataset.plan);
          if (job) openCronEditor(job);
        };
      });

      $$('#cron-jobs [data-run]').forEach((btn) => {
        btn.onclick = async () => {
          btn.disabled = true;
          try {
            await api('/api/cron/' + btn.dataset.run + '/run', { method: 'POST' });
            toast('任务已执行');
            loadCron();
          } catch (e) { toast(e.message, 'error'); btn.disabled = false; }
        };
      });

      const runs = await api('/api/cron/runs?limit=30');
      const tbody = $('#cron-runs-tbody');
      if (!runs.length) {
        tbody.innerHTML = '<tr><td colspan="5"><div class="empty">暂无执行记录</div></td></tr>';
      } else {
        tbody.innerHTML = runs.map((r) =>
          '<tr><td class="cell-sub">' + esc(r.started_at) + '</td>' +
          '<td>' + esc(r.job_name || r.job_id) + '</td>' +
          '<td>' + statusBadge(r.status) + '</td>' +
          '<td class="cell-sub">' + (r.duration_ms || 0) + ' ms</td>' +
          '<td class="cell-sub">' + esc(r.message || '') + '</td></tr>').join('');
      }
    } catch (e) { toast(e.message, 'error'); }
  }

  /* ---------------------------------------------------------- 定时计划配置器 */
  const CRON_MODES = [
    { id: 'every_minute', label: '每分钟' },
    { id: 'every_n_minutes', label: '每 N 分钟' },
    { id: 'hourly', label: '每小时' },
    { id: 'every_n_hours', label: '每 N 小时' },
    { id: 'daily', label: '每天' },
    { id: 'weekly', label: '每周' },
    { id: 'monthly', label: '每月' },
    { id: 'custom', label: '自定义 cron' },
  ];
  const DAY_NAMES = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];

  function pad2(n) { return String(n).padStart(2, '0'); }

  /* 把 "1-5" / "1,3,5" / "MON-FRI" 之类的星期字段展开成 0..6 的数字数组 */
  function expandDays(field) {
    const NAME2NUM = { sun: 0, mon: 1, tue: 2, wed: 3, thu: 4, fri: 5, sat: 6 };
    const toNum = (token) => {
      const key = String(token).trim().toLowerCase();
      if (key in NAME2NUM) return NAME2NUM[key];
      const n = Number(key);
      if (isNaN(n)) return null;
      return n === 7 ? 0 : (n >= 0 && n <= 6 ? n : null);
    };
    const out = [];
    String(field || '').split(',').forEach((part) => {
      const token = part.trim();
      if (!token || token === '*') return;
      if (token.indexOf('-') > 0) {
        const [a, b] = token.split('-');
        let start = toNum(a), end = toNum(b);
        if (start === null || end === null) return;
        for (let i = 0; i < 7; i += 1) {
          const day = (start + i) % 7;
          if (out.indexOf(day) < 0) out.push(day);
          if (day === end) break;
        }
      } else {
        const day = toNum(token);
        if (day !== null && out.indexOf(day) < 0) out.push(day);
      }
    });
    return out;
  }

  /* 把 cron 表达式反解成配置器的状态 */
  function parseCron(expr) {
    const st = { mode: 'custom', n: 5, minute: 0, hour: 3, days: [], dom: 1, expr: expr || '' };
    const parts = String(expr || '').trim().split(/\s+/);
    if (parts.length !== 5) return st;
    const [m, h, dom, , dow] = parts;
    st.expr = expr;
    if (m === '*' && h === '*' && dom === '*' && dow === '*') { st.mode = 'every_minute'; return st; }
    if (/^\*\/\d+$/.test(m) && h === '*' && dom === '*' && dow === '*') {
      st.mode = 'every_n_minutes'; st.n = Number(m.slice(2)); return st;
    }
    if (/^\d+$/.test(m) && h === '*' && dom === '*' && dow === '*') {
      st.mode = 'hourly'; st.minute = Number(m); return st;
    }
    if (/^\d+$/.test(m) && /^\*\/\d+$/.test(h) && dom === '*' && dow === '*') {
      st.mode = 'every_n_hours'; st.minute = Number(m); st.n = Number(h.slice(2)); return st;
    }
    if (/^\d+$/.test(m) && /^\d+$/.test(h)) {
      st.minute = Number(m); st.hour = Number(h);
      if (dow !== '*' && dom === '*') {
        st.mode = 'weekly';
        st.days = expandDays(dow);
        return st;
      }
      if (dow === '*' && /^\d+$/.test(dom)) { st.mode = 'monthly'; st.dom = Number(dom); return st; }
      if (dow === '*' && dom === '*') { st.mode = 'daily'; return st; }
    }
    return st;
  }

  /* 由配置器状态生成 cron 表达式 */
  function buildCron(st) {
    const m = st.minute, h = st.hour;
    switch (st.mode) {
      case 'every_minute': return '* * * * *';
      case 'every_n_minutes': return '*/' + (st.n || 5) + ' * * * *';
      case 'hourly': return m + ' * * * *';
      case 'every_n_hours': return m + ' */' + (st.n || 2) + ' * * *';
      case 'daily': return m + ' ' + h + ' * * *';
      case 'weekly': return m + ' ' + h + ' * * ' + (st.days.length ? st.days.slice().sort((a, b) => a - b).join(',') : '*');
      case 'monthly': return m + ' ' + h + ' ' + (st.dom || 1) + ' * *';
      default: return st.expr;
    }
  }

  function timeValue(st) { return pad2(st.hour) + ':' + pad2(st.minute); }

  function applyTimeInput(st, value) {
    const [hh, mm] = String(value || '00:00').split(':');
    st.hour = Number(hh) || 0;
    st.minute = Number(mm) || 0;
  }

  function openCronEditor(job) {
    const st = parseCron(job.cron);
    let enabled = job.enabled !== false;
    let expr = job.cron || '* * * * *';
    let timer = null;

    const m = openModal({
      title: '配置执行计划',
      wide: true,
      body:
        '<div class="ce-head">' +
        '<div><div class="ce-name">' + esc(job.name) + '</div>' +
        '<div class="small muted">' + esc(job.description || '') + '</div></div>' +
        '<label class="switch"><input type="checkbox" id="ce-enabled"' + (enabled ? ' checked' : '') +
        '><span class="track"></span></label></div>' +
        '<div class="field"><label>多久跑一次</label>' +
        '<div class="ce-modes" id="ce-modes">' +
        CRON_MODES.map((x) => '<button type="button" data-mode="' + x.id + '">' + x.label + '</button>').join('') +
        '</div></div>' +
        '<div id="ce-controls"></div>' +
        '<div class="field" style="margin-top:14px"><label>cron 表达式（可手动微调）</label>' +
        '<input class="input mono" id="ce-expr" value="' + esc(expr) + '" spellcheck="false"></div>' +
        '<div class="ce-desc" id="ce-desc"></div>' +
        '<div class="ce-next" id="ce-next"><span class="small muted">正在计算下次执行时间…</span></div>',
      footer:
        '<span class="small muted" style="margin-right:auto">保存后立即重载调度，无需重启服务</span>' +
        '<button class="btn" data-act="cancel">取消</button>' +
        '<button class="btn btn-primary" data-act="save">保存计划</button>',
    });

    const root = m.root;

    function renderControls() {
      const box = $('#ce-controls', root);
      const mode = st.mode;
      if (mode === 'every_minute') {
        box.innerHTML = '<div class="hint-box">每分钟都会执行一次，一般只用于调试。</div>';
      } else if (mode === 'every_n_minutes') {
        box.innerHTML = '<div class="ce-row"><div class="field"><label>间隔</label>' +
          '<div class="ce-inline"><input class="input" type="number" min="1" max="59" id="ce-n" value="' + st.n + '">' +
          '<span class="small muted">分钟执行一次</span></div></div></div>';
      } else if (mode === 'hourly') {
        box.innerHTML = '<div class="ce-row"><div class="field"><label>每小时的第几分钟</label>' +
          '<div class="ce-inline"><input class="input" type="number" min="0" max="59" id="ce-minute" value="' + st.minute + '">' +
          '<span class="small muted">分</span></div></div></div>';
      } else if (mode === 'every_n_hours') {
        box.innerHTML = '<div class="ce-row">' +
          '<div class="field"><label>间隔</label><div class="ce-inline">' +
          '<input class="input" type="number" min="1" max="23" id="ce-n" value="' + st.n + '">' +
          '<span class="small muted">小时</span></div></div>' +
          '<div class="field"><label>在第几分钟</label><div class="ce-inline">' +
          '<input class="input" type="number" min="0" max="59" id="ce-minute" value="' + st.minute + '">' +
          '<span class="small muted">分</span></div></div></div>';
      } else if (mode === 'daily') {
        box.innerHTML = '<div class="ce-row"><div class="field"><label>每天几点</label>' +
          '<input class="input" type="time" id="ce-time" value="' + timeValue(st) + '"></div></div>';
      } else if (mode === 'weekly') {
        box.innerHTML = '<div class="ce-row"><div class="field"><label>星期几（可多选）</label>' +
          '<div class="ce-days">' + DAY_NAMES.map((name, idx) =>
            '<button type="button" class="ce-day' + (st.days.indexOf(idx) >= 0 ? ' active' : '') +
            '" data-day="' + idx + '">' + name + '</button>').join('') + '</div></div>' +
          '<div class="field"><label>几点</label>' +
          '<input class="input" type="time" id="ce-time" value="' + timeValue(st) + '"></div></div>';
      } else if (mode === 'monthly') {
        box.innerHTML = '<div class="ce-row"><div class="field"><label>每月几号</label>' +
          '<div class="ce-inline"><input class="input" type="number" min="1" max="31" id="ce-dom" value="' + st.dom + '">' +
          '<span class="small muted">日</span></div></div>' +
          '<div class="field"><label>几点</label>' +
          '<input class="input" type="time" id="ce-time" value="' + timeValue(st) + '"></div></div>';
      } else {
        box.innerHTML = '<div class="hint-box">直接写 cron 表达式，格式：<code>分 时 日 月 周</code>。' +
          '例如 <code>0 3 * * *</code> 表示每天 3:00。</div>';
      }
      bindControls();
    }

    function bindControls() {
      const nEl = $('#ce-n', root);
      if (nEl) nEl.oninput = () => { st.n = Math.max(1, Number(nEl.value) || 1); sync(); };
      const minEl = $('#ce-minute', root);
      if (minEl) minEl.oninput = () => { st.minute = Math.min(59, Math.max(0, Number(minEl.value) || 0)); sync(); };
      const tEl = $('#ce-time', root);
      if (tEl) tEl.onchange = () => { applyTimeInput(st, tEl.value); sync(); };
      const domEl = $('#ce-dom', root);
      if (domEl) domEl.oninput = () => { st.dom = Math.min(31, Math.max(1, Number(domEl.value) || 1)); sync(); };
      $$('.ce-day', root).forEach((btn) => {
        btn.onclick = () => {
          const d = Number(btn.dataset.day);
          const i = st.days.indexOf(d);
          if (i >= 0) st.days.splice(i, 1); else st.days.push(d);
          btn.classList.toggle('active');
          sync();
        };
      });
    }

    /* 控件 → 表达式 → 预览 */
    function sync() {
      if (st.mode !== 'custom') {
        expr = buildCron(st);
        $('#ce-expr', root).value = expr;
      }
      preview();
    }

    function preview() {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const input = $('#ce-expr', root);
        const value = input.value.trim();
        let data;
        try {
          data = await api('/api/cron/preview', { method: 'POST', body: { expr: value, count: 5 } });
        } catch (e) {
          input.classList.add('invalid');
          $('#ce-desc', root).innerHTML = '<span style="color:var(--red)">✗ ' + esc(e.message) + '</span>';
          $('#ce-next', root).innerHTML = '<span class="small muted">表达式不合法，修正后会自动预览</span>';
          return;
        }
        input.classList.remove('invalid');
        $('#ce-desc', root).innerHTML = '✓ ' + esc(data.description);
        $('#ce-next', root).innerHTML =
          '<div class="small muted" style="margin-bottom:6px">接下来 5 次执行</div>' +
          (data.next_runs || []).map((t) => '<div class="ce-next-item">' + esc(t) + '</div>').join('');
      }, 260);
    }

    $$('#ce-modes button', root).forEach((btn) => {
      btn.classList.toggle('active', btn.dataset.mode === st.mode);
      btn.onclick = () => {
        st.mode = btn.dataset.mode;
        $$('#ce-modes button', root).forEach((b) => b.classList.toggle('active', b === btn));
        renderControls();
        sync();
      };
    });

    $('#ce-expr', root).oninput = () => {
      // 手动改表达式时，模式切到自定义，避免被控件覆盖
      st.mode = 'custom';
      $$('#ce-modes button', root).forEach((b) => b.classList.toggle('active', b.dataset.mode === 'custom'));
      renderControls();
      expr = $('#ce-expr', root).value;
      preview();
    };

    $('#ce-enabled', root).onchange = (e) => { enabled = e.target.checked; };

    $('[data-act="cancel"]', root).onclick = m.close;
    $('[data-act="save"]', root).onclick = async () => {
      const value = $('#ce-expr', root).value.trim();
      const btn = $('[data-act="save"]', root);
      btn.disabled = true;
      try {
        await api('/api/cron/jobs/' + job.id, {
          method: 'PUT',
          body: { cron: value, enabled: enabled },
        });
        m.close();
        toast('计划已保存');
        loadCron();
      } catch (e) {
        toast(e.message, 'error');
        btn.disabled = false;
      }
    };

    renderControls();
    preview();
  }

  /* ---------------------------------------------------------- 日志 */
  async function loadLogs() {
    try {
      if (state.logTab === 'op') {
        const rows = await api('/api/logs?limit=200&keyword=' + encodeURIComponent($('#log-search').value.trim()));
        const tbody = $('#logs-tbody');
        tbody.innerHTML = rows.length ? rows.map((r) =>
          '<tr><td class="cell-sub">' + esc(r.ts) + '</td>' +
          '<td>' + levelBadge(r.level) + '</td>' +
          '<td>' + esc(r.action) + '</td>' +
          '<td class="cell-sub">' + esc(r.target || '—') + '</td>' +
          '<td>' + statusBadge(r.status) + ' <span class="cell-sub">' + esc(r.message || '') + '</span></td></tr>'
        ).join('') : '<tr><td colspan="5"><div class="empty">没有匹配的日志</div></td></tr>';
      } else {
        const rows = await api('/api/bot/messages?limit=100');
        const tbody = $('#bot-tbody');
        tbody.innerHTML = rows.length ? rows.map((r) =>
          '<tr><td class="cell-sub">' + esc(r.ts) + '</td>' +
          '<td>' + esc(r.sender_nick || r.sender_id || '—') + '</td>' +
          '<td class="cell-sub mono">' + esc((r.conversation_title || r.conversation_id || '—')) + '</td>' +
          '<td><code>' + esc(r.text || '') + '</code><div class="cell-sub">' + esc(r.intent || '') + '</div></td>' +
          '<td>' + statusBadge(r.status) + '</td></tr>').join('')
          : '<tr><td colspan="5"><div class="empty">暂无机器人指令</div></td></tr>';
      }
    } catch (e) { toast(e.message, 'error'); }
  }

  /* ---------------------------------------------------------- 设置 */
  async function loadSettings() {
    try {
      state.settings = await api('/api/settings');
      const cfg = state.settings;
      const aliyun = cfg.aliyun || {};
      const policy = cfg.rules_policy || {};
      const server = cfg.server || {};
      const notify = cfg.notify || {};
      const geoCfg = cfg.geo || {};

      $('#cfg-aliyun-enabled').checked = !!aliyun.enabled;
      $('#cfg-provider').value = aliyun.provider || 'swas';
      $('#cfg-ak-id').value = aliyun.access_key_id || '';
      $('#cfg-ak-secret').value = aliyun.access_key_secret || '';
      $('#cfg-region').value = aliyun.region_id || '';
      $('#cfg-endpoint').value = aliyun.endpoint || '';

      $('#cfg-auto-cleanup').checked = policy.auto_cleanup_old_ip !== false;
      $('#cfg-cleanup-days').value = policy.cleanup_days || 7;
      $('#cfg-default-ttl').value = policy.default_ttl_minutes || 0;
      $('#cfg-max-rules').value = policy.max_rules_per_instance || 100;
      $('#cfg-log-days').value = (cfg.logging || {}).keep_days || 30;
      $('#cfg-expire-notice').value = policy.expire_notice_minutes === undefined ? 10 : policy.expire_notice_minutes;
      $('#cfg-backup-keep').value = (cfg.logging || {}).backup_keep || 7;

      $('#cfg-notify-enabled').checked = !!notify.enabled;
      $('#cfg-notify-allow').checked = notify.on_allow !== false;
      $('#cfg-notify-delete').checked = notify.on_delete !== false;
      $('#cfg-notify-renew').checked = notify.on_renew !== false;
      $('#cfg-notify-expire').checked = !!notify.on_expire;
      $('#cfg-notify-template').value = notify.template || '';

      $('#cfg-geo-enabled').checked = geoCfg.enabled !== false;
      $('#cfg-geo-endpoint').value = geoCfg.endpoint || '';

      $('#cfg-port').value = server.port || 5050;
      $('#cfg-admin-user').value = server.admin_user || 'admin';
      $('#cfg-webhook-secret').value = (cfg.webhook || {}).secret || '';
      $('#cfg-metrics-public').checked = server.metrics_public !== false;

      const publicIp = cfg.public_ip || {};
      $('#cfg-publicip-enabled').checked = !!publicIp.enabled;
      $('#cfg-publicip-notify').checked = publicIp.notify_on_change !== false;
      $('#cfg-publicip-ports').value = publicIp.ports || 'ALL';
      $('#cfg-publicip-protocol').value = publicIp.protocol || 'tcp';
      $('#cfg-publicip-ttl').value = publicIp.ttl_minutes || 0;
      $('#cfg-publicip-tag').value = publicIp.tag || '公网IP';

      renderInstances(aliyun.instances || []);
      renderCronToggles(cfg.cron || {});
      renderTwofa(cfg.twofa || {});
      renderWebhookExample();
      fillInstanceSelect();
      loadTokens();
      loadBackups();
    } catch (e) { toast(e.message, 'error'); }
  }

  /* 令牌明文只对网页会话开放，设置页单独拉一次 */
  async function loadTokens() {
    if (!$('#cfg-api-token')) return;
    try {
      const data = await api('/api/account/tokens');
      $('#cfg-api-token').value = data.api_token || '';
      $('#cfg-api-token-ro').value = data.api_token_readonly || '';
    } catch (e) {
      $('#cfg-api-token').value = '（仅网页登录可见）';
      $('#cfg-api-token-ro').value = '（仅网页登录可见）';
    }
  }

  async function loadBackups() {
    const box = $('#backup-list');
    if (!box) return;
    try {
      const data = await api('/api/backups');
      const files = data.files || [];
      $('#backup-dir').textContent = data.dir ? ('备份目录：' + data.dir) : '';
      if (!files.length) {
        box.innerHTML = '<div class="empty">还没有备份，点击右上角「立即备份」生成第一份</div>';
        return;
      }
      box.innerHTML = files.slice(0, 10).map((f) =>
        '<div class="row-between" style="padding:6px 0;border-bottom:1px solid var(--border)">' +
        '<div><div style="font-size:12.5px" class="mono">' + esc(f.name) + '</div>' +
        '<div class="small muted">' + esc(f.created_at) + ' · ' + f.size_kb + ' KB</div></div>' +
        '<a class="btn btn-sm" href="/api/backups/' + encodeURIComponent(f.name) + '">下载</a></div>').join('');
    } catch (e) {
      box.innerHTML = '<div class="empty">读取备份列表失败：' + esc(e.message) + '</div>';
    }
  }

  function renderInstances(instances) {
    const box = $('#instances-list');
    const list = instances.length ? instances : [{ instance_id: '', name: '', enabled: true }];
    box.innerHTML = list.map((item, idx) =>
      '<div class="row-between" data-idx="' + idx + '" style="gap:8px">' +
      '<input class="input" data-f="id" placeholder="实例 ID（如 c05384a6...）" value="' + esc(item.instance_id || '') + '" style="flex:2">' +
      '<input class="input" data-f="name" placeholder="备注名" value="' + esc(item.name || '') + '" style="flex:1.2">' +
      '<label class="switch" style="flex:0 0 auto"><input type="checkbox" data-f="enabled"' +
      (item.enabled !== false ? ' checked' : '') + '><span class="track"></span></label>' +
      '<button class="btn btn-sm btn-danger" data-remove="' + idx + '">移除</button>' +
      '</div>').join('');

    $$('#instances-list [data-remove]').forEach((btn) => {
      btn.onclick = () => {
        const rows = collectInstances();
        rows.splice(Number(btn.dataset.remove), 1);
        renderInstances(rows);
      };
    });
  }

  function collectInstances() {
    return $$('#instances-list [data-idx]').map((row) => ({
      instance_id: $('[data-f="id"]', row).value.trim(),
      name: $('[data-f="name"]', row).value.trim(),
      enabled: $('[data-f="enabled"]', row).checked,
    })).filter((i) => i.instance_id);
  }

  function renderCronToggles(cron) {
    const jobs = cron.jobs || {};
    const names = {
      cleanup_expired: '过期规则清理',
      sync_cloud_rules: '云端规则同步',
      cleanup_stale_ip: '旧 IP 规则清理',
      prune_logs: '日志归档清理',
      health_probe: '云凭证健康检查',
      daily_report: '每日状态推送',
      expiring_notice: '到期前提醒',
      sync_public_ip: '公网 IP 自动放行',
      backup_db: '数据库备份',
    };
    // 合并「配置里实际存在的任务」与「内置名称表」，避免新增任务后设置页漏显示
    const ids = Array.from(new Set([...Object.keys(jobs), ...Object.keys(names)]));
    const box = $('#cron-toggles');
    box.innerHTML = ids.map((id) => {
      const item = jobs[id] || {};
      const label = item.name || names[id] || id;
      return '<div class="row-between" style="padding:6px 0">' +
        '<div><div style="font-weight:600;font-size:13px">' + esc(label) + '</div>' +
        '<div class="cron-chip">' + esc(item.cron || '') + '</div></div>' +
        '<label class="switch"><input type="checkbox" data-job="' + id + '"' +
        (item.enabled !== false ? ' checked' : '') + '><span class="track"></span></label></div>';
    }).join('') +
      '<div class="row-between" style="padding:8px 0;border-top:1px solid var(--border);margin-top:8px">' +
      '<span class="small muted">总开关</span>' +
      '<label class="switch"><input type="checkbox" data-job="__master"' +
      (cron.enabled !== false ? ' checked' : '') + '><span class="track"></span></label></div>';
  }

  /* ---------------------------------------------------------- 二次验证 (2FA) */
  function renderTwofa(cfg) {
    cfg = cfg || {};
    if (!$('#cfg-2fa-enabled')) return;
    $('#cfg-2fa-enabled').checked = !!cfg.enabled;
    const scope = cfg.scope || {};
    $('#cfg-2fa-scope-web').checked = !!scope.web_login;
    $('#cfg-2fa-scope-webhook').checked = !!scope.webhook;
    $('#cfg-2fa-scope-dingtalk').checked = !!scope.dingtalk;
    $('#cfg-2fa-issuer').value = cfg.issuer || '动态云防火墙';
    $('#cfg-2fa-account').value = cfg.account || 'admin';
    const cidrs = cfg.webhook_trusted_cidrs || [];
    $('#cfg-2fa-dingtalk-all').checked = !!cfg.dingtalk_all;
    $('#cfg-2fa-webhook-skip-private').checked = !!cfg.webhook_skip_private;
    $('#cfg-2fa-trusted-cidrs').value = Array.isArray(cidrs) ? cidrs.join(', ') : (cidrs || '');
    $('#f2fa-secret').textContent = cfg.secret || '（尚未配置）';
    $('#f2fa-qr').innerHTML = '<div class="empty">点击「显示二维码」查看</div>';
    setTwofaHint('');
  }

  function setTwofaHint(text, kind) {
    const el = $('#f2fa-verify-hint');
    if (!el) return;
    el.textContent = text || '输入认证器显示的 6 位数字，确认密钥正确后再启用二次验证。';
    el.style.color = kind === 'ok' ? 'var(--accent-dark)' : (kind === 'err' ? '#c0392b' : '');
  }

  function paintTwofaQr(payload, fullSecret) {
    if (payload && payload.qr_svg) {
      $('#f2fa-qr').innerHTML = payload.qr_svg;
    } else {
      $('#f2fa-qr').innerHTML = '<div class="empty">二维码不可用，请复制下方密钥手动添加到认证器</div>';
    }
    if (fullSecret && payload && payload.secret) {
      $('#f2fa-secret').textContent = payload.secret;
    } else if (payload && payload.secret_masked) {
      $('#f2fa-secret').textContent = payload.secret_masked;
    }
  }

  async function twofaGenerate() {
    try {
      const data = await api('/api/2fa/generate', {
        method: 'POST',
        body: {
          issuer: $('#cfg-2fa-issuer').value.trim(),
          account: $('#cfg-2fa-account').value.trim(),
        },
      });
      paintTwofaQr(data, true);
      setTwofaHint('已生成新密钥，请扫码绑定后点「验证」确认。', 'ok');
      toast('已生成新密钥');
    } catch (e) { toast(e.message, 'error'); }
  }

  async function twofaShowQr() {
    try {
      const data = await api('/api/2fa/qrcode');
      paintTwofaQr(data, false);
    } catch (e) { toast(e.message, 'error'); }
  }

  async function twofaApplyCustom() {
    const secret = $('#cfg-2fa-custom').value.trim();
    if (!secret) { toast('请先粘贴 Base32 密钥', 'error'); return; }
    try {
      const data = await api('/api/2fa/secret', {
        method: 'PUT',
        body: {
          secret: secret,
          issuer: $('#cfg-2fa-issuer').value.trim(),
          account: $('#cfg-2fa-account').value.trim(),
        },
      });
      paintTwofaQr(data, true);
      setTwofaHint('已应用自定义密钥，请扫码/手动添加后点「验证」。', 'ok');
      toast('密钥已应用');
    } catch (e) { toast(e.message, 'error'); }
  }

  async function twofaVerify() {
    const code = $('#cfg-2fa-code').value.trim();
    if (!/^\d{6}$/.test(code)) { toast('请输入 6 位数字口令', 'error'); return; }
    try {
      await api('/api/2fa/verify', { method: 'POST', body: { code: code } });
      setTwofaHint('✓ 口令正确，可以启用二次验证了。', 'ok');
      toast('口令正确');
    } catch (e) {
      setTwofaHint('✗ ' + e.message, 'err');
      toast(e.message, 'error');
    }
  }

  function renderWebhookExample() {
    const origin = location.origin;
    $('#webhook-example').textContent =
      'curl -X POST ' + origin + '/webhook/ip-change \\\n' +
      '  -H "Content-Type: application/json" \\\n' +
      '  -H "X-Webhook-Secret: <你的密钥>" \\\n' +
      '  -d \'{"ipv4": "1.2.3.4", "ports": "ALL", "ttl_minutes": 1440, "domain": "home.ddns.net"}\'';
  }

  async function saveSettings() {
    const jobs = {};
    $$('#cron-toggles [data-job]').forEach((el) => {
      if (el.dataset.job === '__master') return;
      jobs[el.dataset.job] = { enabled: el.checked };
    });
    const master = $('#cron-toggles [data-job="__master"]');

    const patch = {
      aliyun: {
        enabled: $('#cfg-aliyun-enabled').checked,
        provider: $('#cfg-provider').value,
        access_key_id: $('#cfg-ak-id').value.trim(),
        access_key_secret: $('#cfg-ak-secret').value,
        region_id: $('#cfg-region').value.trim(),
        endpoint: $('#cfg-endpoint').value.trim(),
        instances: collectInstances(),
      },
      rules_policy: {
        auto_cleanup_old_ip: $('#cfg-auto-cleanup').checked,
        cleanup_days: Number($('#cfg-cleanup-days').value || 7),
        default_ttl_minutes: Number($('#cfg-default-ttl').value || 0),
        max_rules_per_instance: Number($('#cfg-max-rules').value || 100),
        expire_notice_minutes: Number($('#cfg-expire-notice').value || 0),
      },
      logging: {
        keep_days: Number($('#cfg-log-days').value || 30),
        backup_keep: Number($('#cfg-backup-keep').value || 7),
      },
      notify: {
        enabled: $('#cfg-notify-enabled').checked,
        on_allow: $('#cfg-notify-allow').checked,
        on_delete: $('#cfg-notify-delete').checked,
        on_renew: $('#cfg-notify-renew').checked,
        on_expire: $('#cfg-notify-expire').checked,
        template: $('#cfg-notify-template').value,
      },
      geo: {
        enabled: $('#cfg-geo-enabled').checked,
        endpoint: $('#cfg-geo-endpoint').value.trim(),
      },
      server: {
        port: Number($('#cfg-port').value || 5050),
        admin_user: $('#cfg-admin-user').value.trim(),
        metrics_public: $('#cfg-metrics-public').checked,
      },
      public_ip: {
        enabled: $('#cfg-publicip-enabled').checked,
        notify_on_change: $('#cfg-publicip-notify').checked,
        ports: $('#cfg-publicip-ports').value.trim() || 'ALL',
        protocol: $('#cfg-publicip-protocol').value,
        ttl_minutes: Number($('#cfg-publicip-ttl').value || 0),
        tag: $('#cfg-publicip-tag').value.trim() || '公网IP',
      },
      webhook: { secret: $('#cfg-webhook-secret').value },
      cron: { enabled: master ? master.checked : true, jobs: jobs },
    };

    // 先保存二次验证（后端会校验「启用前必须有密钥」），失败则中止整体保存
    if ($('#cfg-2fa-enabled')) {
      try {
        await api('/api/2fa/config', {
          method: 'PUT',
          body: {
            enabled: $('#cfg-2fa-enabled').checked,
            issuer: $('#cfg-2fa-issuer').value.trim(),
            account: $('#cfg-2fa-account').value.trim(),
            scope: {
              web_login: $('#cfg-2fa-scope-web').checked,
              webhook: $('#cfg-2fa-scope-webhook').checked,
              dingtalk: $('#cfg-2fa-scope-dingtalk').checked,
            },
            dingtalk_all: $('#cfg-2fa-dingtalk-all').checked,
            webhook_skip_private: $('#cfg-2fa-webhook-skip-private').checked,
            webhook_trusted_cidrs: $('#cfg-2fa-trusted-cidrs').value.trim(),
          },
        });
      } catch (e) { toast('二次验证保存失败：' + e.message, 'error'); return; }
    }

    try {
      state.settings = await api('/api/settings', { method: 'PUT', body: patch });
      toast('设置已保存并生效');
      loadSettings();
      loadOverview();
    } catch (e) { toast(e.message, 'error'); }
  }

  /* ---------------------------------------------------------- 主题 */
  function readTheme() {
    try { return localStorage.getItem(THEME_KEY) || 'auto'; } catch (e) { return 'auto'; }
  }

  function prefersDark() {
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  }

  function applyTheme(pref) {
    const dark = pref === 'dark' || (pref === 'auto' && prefersDark());
    document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
    const icon = $('#theme-icon');
    if (icon) icon.textContent = pref === 'auto' ? '◐' : (pref === 'dark' ? '☾' : '☀');
    const btn = $('#btn-theme');
    if (btn) btn.title = '主题：' + THEME_LABEL[pref] + '（点击切换）';
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', dark ? '#0d1117' : '#4f46e5');
  }

  function cycleTheme() {
    const cur = readTheme();
    const next = THEME_ORDER[(THEME_ORDER.indexOf(cur) + 1) % THEME_ORDER.length];
    try { localStorage.setItem(THEME_KEY, next); } catch (e) { /* 隐私模式忽略 */ }
    applyTheme(next);
    toast('主题：' + THEME_LABEL[next], 'info');
  }

  function initTheme() {
    applyTheme(readTheme());
    if (!window.matchMedia) return;
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    const onChange = () => { if (readTheme() === 'auto') applyTheme('auto'); };
    if (mq.addEventListener) mq.addEventListener('change', onChange);
    else if (mq.addListener) mq.addListener(onChange);
  }

  /* ---------------------------------------------------------- 强制改密 */
  let pwGateOpen = false;

  function openPasswordGate() {
    if (pwGateOpen) return;
    pwGateOpen = true;
    const el = document.createElement('div');
    el.className = 'pw-gate';
    el.id = 'pw-gate';
    el.innerHTML =
      '<div class="card" style="max-width:430px;width:100%;box-shadow:var(--shadow-lg)">' +
      '<div class="card-head"><h3>请先修改初始口令</h3></div>' +
      '<div class="card-body">' +
      '<div class="hint-box warn" style="margin-bottom:14px">系统检测到仍在使用默认口令 admin123。' +
      '改密之前不会开放其他操作，避免整站被人用默认口令接管。</div>' +
      '<div class="field"><label>原口令</label><input class="input" id="g-old" type="password" value="admin123" autocomplete="current-password"></div>' +
      '<div class="field"><label>新口令（至少 8 位，不能与旧口令相同）</label>' +
      '<input class="input" id="g-new" type="password" placeholder="建议字母 + 数字 + 符号" autocomplete="new-password"></div>' +
      '<div class="field"><label>确认新口令</label><input class="input" id="g-confirm" type="password" autocomplete="new-password"></div>' +
      '<button class="btn btn-primary btn-block mt-16" id="g-submit">更新口令并继续</button>' +
      '</div></div>';
    document.body.appendChild(el);

    const submit = async () => {
      const oldPw = $('#g-old').value;
      const newPw = $('#g-new').value;
      const confirm = $('#g-confirm').value;
      if (newPw.length < 8) { toast('新口令至少 8 位', 'warn'); return; }
      if (newPw !== confirm) { toast('两次输入的新口令不一致', 'warn'); return; }
      const btn = $('#g-submit');
      btn.disabled = true;
      btn.textContent = '提交中…';
      try {
        await api('/api/account/password', {
          method: 'POST',
          body: { old_password: oldPw, new_password: newPw, confirm_password: confirm },
        });
        el.remove();
        pwGateOpen = false;
        toast('口令已更新');
        setTimeout(() => location.reload(), 600);
      } catch (e) {
        toast(e.message, 'error');
        btn.disabled = false;
        btn.textContent = '更新口令并继续';
      }
    };
    $('#g-submit').onclick = submit;
    $$('#g-new, #g-confirm', el).forEach((input) => {
      input.onkeydown = (ev) => { if (ev.key === 'Enter') submit(); };
    });
  }

  /* ---------------------------------------------------------- PWA */
  function registerPWA() {
    if (!('serviceWorker' in navigator)) return;
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('/service-worker.js', { scope: '/' }).catch(() => {
        /* 非 HTTPS / 不支持时静默忽略，页面功能不受影响 */
      });
    });
  }

  async function changePassword() {
    const oldPw = $('#pw-old').value;
    const newPw = $('#pw-new').value;
    const confirm = $('#pw-confirm').value;
    if (!oldPw || !newPw) { toast('请填写原口令与新口令', 'warn'); return; }
    if (newPw.length < 8) { toast('新口令至少 8 位', 'warn'); return; }
    if (newPw !== confirm) { toast('两次输入的新口令不一致', 'warn'); return; }
    try {
      await api('/api/account/password', {
        method: 'POST',
        body: { old_password: oldPw, new_password: newPw, confirm_password: confirm },
      });
      $('#pw-old').value = '';
      $('#pw-new').value = '';
      $('#pw-confirm').value = '';
      toast('口令已更新');
      loadOverview();
    } catch (e) { toast(e.message, 'error'); }
  }

  function copyText(text, label) {
    if (!text) { toast('没有可复制的内容', 'warn'); return; }
    const done = () => toast((label || '内容') + '已复制');
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done));
    } else {
      fallbackCopy(text, done);
    }
  }

  function fallbackCopy(text, done) {
    // 非 HTTPS / 老浏览器：用临时 textarea 兜底
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); done(); } catch (e) { toast('复制失败，请手动选择', 'error'); }
    ta.remove();
  }

  async function rotateToken(level) {
    const name = level === 'read' ? '只读 Token' : '读写 Token';
    const okToRotate = await confirmDialog('重置 ' + name,
      '旧令牌会立即失效，所有使用它的脚本 / 看板都要同步更新。确认重置？', true);
    if (!okToRotate) return;
    try {
      const data = await api('/api/account/tokens/rotate', { method: 'POST', body: { level: level } });
      $('#cfg-api-token').value = data.api_token || '';
      $('#cfg-api-token-ro').value = data.api_token_readonly || '';
      toast(name + ' 已重置');
    } catch (e) { toast(e.message, 'error'); }
  }

  function previewNotify() {
    const tpl = $('#cfg-notify-template').value.trim() ||
      '{action} {ip} {port}（{protocol}）· 实例 {instance} · 操作人 {actor}';
    const sample = {
      action: '✅ 放行', ip: '203.0.113.42', port: '22', protocol: 'TCP',
      instance: 'web-01', actor: '张三', source: '钉钉', expire: '2026-10-30 18:00:00',
      tag: '家里', detail: '', time: '2026-10-29 18:00:00',
    };
    const text = tpl.replace(/\{(\w+)\}/g, (m, key) => (sample[key] !== undefined ? sample[key] : ''));
    const m = openModal({
      title: '通知预览',
      body: '<div class="hint-box">开启后，规则变更会以这样的内容推送到钉钉群：</div>' +
        '<div class="code-box mt-16">' + esc(text) + '</div>' +
        '<div class="help mt-16">单条规则推一条；批量操作会汇总成一条，最多列出 15 条。</div>',
      footer: '<button class="btn" data-act="close">关闭</button>',
    });
    $('[data-act="close"]', m.root).onclick = m.close;
  }

  /* ---------------------------------------------------------- 事件绑定 */
  function bindEvents() {
    $$('.nav-item').forEach((el) => { el.onclick = () => go(el.dataset.page); });
    $$('[data-goto]').forEach((el) => {
      el.onclick = (e) => { e.preventDefault(); go(el.dataset.goto); };
    });
    // 教程页目录跳转（不用 # 锚点，避免和 hash 路由打架）
    $$('[data-guide-jump]').forEach((el) => {
      el.onclick = (e) => {
        e.preventDefault();
        const target = document.getElementById(el.dataset.guideJump);
        if (!target) return;
        const top = target.getBoundingClientRect().top + window.pageYOffset - 80;
        window.scrollTo({ top: top, behavior: 'smooth' });
      };
    });

    /* 移动端抽屉菜单 */
    const toggleBtn = $('#btn-nav-toggle');
    const backdrop = $('#sidebar-backdrop');
    if (toggleBtn) {
      toggleBtn.onclick = () => {
        const open = $('#sidebar').classList.toggle('open');
        document.body.classList.toggle('nav-open', open);
      };
    }
    if (backdrop) {
      backdrop.onclick = () => {
        $('#sidebar').classList.remove('open');
        document.body.classList.remove('nav-open');
      };
    }
    window.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        $('#sidebar').classList.remove('open');
        document.body.classList.remove('nav-open');
      }
    });

    /* PWA 快捷方式 / 浏览器前进后退：地址栏 hash 变化要跟着切页 */
    window.addEventListener('hashchange', () => {
      const page = (location.hash || '').replace('#', '');
      if (PAGE_META[page] && page !== state.page) go(page);
    });

    $('#btn-refresh-all').onclick = () => {
      toast('正在刷新…', 'info');
      loadOverview();
      if (state.page === 'rules') loadRules();
      if (state.page === 'cron') loadCron();
    };

    $('#btn-logout').onclick = async (e) => {
      e.preventDefault();
      await api('/api/logout', { method: 'POST' });
      location.href = '/login';
    };

    const themeBtn = $('#btn-theme');
    if (themeBtn) themeBtn.onclick = cycleTheme;

    /* dashboard */
    $('#btn-detect-ip').onclick = detectIP;
    $('#btn-quick-allow').onclick = quickAllow;

    /* rules */
    $('#rule-filter').onclick = (e) => {
      const btn = e.target.closest('button');
      if (!btn) return;
      $$('#rule-filter button').forEach((b) => b.classList.toggle('active', b === btn));
      state.ruleFilter = btn.dataset.status;
      state.rulePage = 1;
      loadRules();
    };
    let searchTimer = null;
    $('#rule-search').oninput = (e) => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => {
        state.ruleKeyword = e.target.value.trim();
        state.rulePage = 1;
        loadRules();
      }, 320);
    };
    $('#rule-tag').onchange = (e) => {
      state.ruleTag = e.target.value;
      state.rulePage = 1;
      loadRules();
    };
    $('#btn-rule-add').onclick = () => openRuleModal(null);
    $('#btn-rule-sync').onclick = async () => {
      try {
        const data = await api('/api/cloud/sync', { method: 'POST' });
        toast('已同步 ' + (data.synced_rules || 0) + ' 条规则');
        loadRules();
      } catch (e) { toast(e.message, 'error'); }
    };

    /* cloud */
    $('#btn-cloud-pull').onclick = () => pullCloud(false);
    $('#btn-diff').onclick = loadDiff;
    $('#cloud-instance').onchange = () => { pullCloud(true); };
    $('#btn-cloud-discover').onclick = async () => {
      try {
        const list = await api('/api/cloud/instances');
        if (!list.length) { toast('账号下没有可访问的实例', 'warn'); return; }
        const body = '<div class="stack">' + list.map((i) =>
          '<div class="row-between" style="border:1px solid var(--border);border-radius:10px;padding:10px 12px">' +
          '<div><div style="font-weight:600">' + esc(i.name || i.instance_id) + '</div>' +
          '<div class="small muted mono">' + esc(i.instance_id) + ' · ' + esc(i.public_ip || '—') + ' · ' + esc(i.status || '') + '</div></div>' +
          '<span class="badge ' + (i.configured ? 'badge-green' : 'badge') + '">' +
          (i.configured ? '已纳管' : '未纳管') + '</span></div>').join('') + '</div>' +
          '<div class="hint-box mt-16">在「系统设置 → 纳管实例」中添加实例 ID 后即可对其放行。</div>';
        openModal({ title: '账号下的实例（' + list.length + '）', body: body, footer: '<button class="btn" onclick="this.closest(\'.modal-backdrop\').remove()">关闭</button>', wide: true });
      } catch (e) { toast(e.message, 'error'); }
    };

    /* dingtalk */
    $('#btn-dt-save').onclick = saveDingtalk;
    $('#btn-dt-restart').onclick = async () => {
      try {
        await api('/api/dingtalk/restart', { method: 'POST' });
        toast('已重启长连接');
        setTimeout(loadDingtalk, 1200);
      } catch (e) { toast(e.message, 'error'); }
    };
    $('#btn-dt-test').onclick = async () => {
      try {
        const data = await api('/api/settings/test/dingtalk', { method: 'POST', body: { conversation_id: $('#dt-notify-conv').value.trim() } });
        toast('凭证有效' + (data.sent ? '，测试消息已发送' : ''));
      } catch (e) { toast(e.message, 'error'); }
    };
    $('#btn-dt-fetch-test').onclick = () => pickConversation('dt-test-conv');
    $('#btn-dt-fetch-notify').onclick = () => pickConversation('dt-notify-conv');
    $('#btn-dt-diag').onclick = runDiagnose;
    $('#btn-dt-send').onclick = async () => {
      try {
        await api('/api/dingtalk/send', {
          method: 'POST',
          body: { conversation_id: $('#dt-test-conv').value.trim(), text: $('#dt-test-text').value, title: '测试消息' },
        });
        toast('已发送');
      } catch (e) { toast(e.message, 'error'); }
    };

    /* cron */
    $('#btn-cron-reload').onclick = async () => {
      try {
        await api('/api/cron/reload', { method: 'POST' });
        toast('调度已重载');
        loadCron();
      } catch (e) { toast(e.message, 'error'); }
    };
    $('#btn-cron-refresh').onclick = loadCron;

    /* logs */
    $('#log-tabs').onclick = (e) => {
      const btn = e.target.closest('button');
      if (!btn) return;
      $$('#log-tabs button').forEach((b) => b.classList.toggle('active', b === btn));
      state.logTab = btn.dataset.tab;
      $('#tab-op').style.display = state.logTab === 'op' ? '' : 'none';
      $('#tab-bot').style.display = state.logTab === 'bot' ? '' : 'none';
      loadLogs();
    };
    $('#btn-log-refresh').onclick = loadLogs;
    $('#log-search').oninput = () => { state.logTab = 'op'; loadLogs(); };

    /* settings */
    $('#btn-instance-add').onclick = () => {
      const rows = collectInstances();
      rows.push({ instance_id: '', name: '', enabled: true });
      renderInstances(rows);
    };
    $('#btn-save-settings').onclick = saveSettings;

    /* 账号与令牌 */
    $('#btn-change-pw').onclick = changePassword;
    $('#btn-copy-token').onclick = () => copyText($('#cfg-api-token').value, '读写 Token');
    $('#btn-copy-token-ro').onclick = () => copyText($('#cfg-api-token-ro').value, '只读 Token');
    $('#btn-rotate-token').onclick = () => rotateToken('write');
    $('#btn-rotate-token-ro').onclick = () => rotateToken('read');
    $('#btn-reload-tokens').onclick = loadTokens;
    $('#btn-backup-run').onclick = async (ev) => {
      const btn = ev.currentTarget;
      btn.disabled = true;
      try {
        const data = await api('/api/backup/run', { method: 'POST' });
        toast('备份完成，共 ' + (data.files || []).length + ' 份');
        loadBackups();
      } catch (e) { toast(e.message, 'error'); }
      btn.disabled = false;
    };
    $('#btn-notify-preview').onclick = previewNotify;
    /* 二次验证 (2FA) */
    if ($('#btn-2fa-generate')) {
      $('#btn-2fa-generate').onclick = twofaGenerate;
      $('#btn-2fa-show-qr').onclick = twofaShowQr;
      $('#btn-2fa-apply-custom').onclick = twofaApplyCustom;
      $('#btn-2fa-verify').onclick = twofaVerify;
    }
    $('#btn-test-aliyun').onclick = async () => {
      try {
        const data = await api('/api/settings/test/aliyun', { method: 'POST' });
        toast('阿里云凭证可用，可访问实例 ' + (data.instance_count || 0) + ' 台');
      } catch (e) { toast(e.message, 'error'); }
    };

    document.addEventListener('click', (e) => {
      const nav = e.target.closest('[data-goto]');
      if (nav) { e.preventDefault(); go(nav.dataset.goto); }
    });
  }

  /* ---------------------------------------------------------- 启动 */
  async function boot() {
    initTheme();
    registerPWA();
    bindEvents();

    try {
      const me = await api('/api/me');
      state.me = me;
      $('#footer-user').textContent = me.user || 'admin';
      if (me.must_change_password) {
        // 默认口令未改：先挡住所有操作
        openPasswordGate();
        return;
      }
    } catch (e) {
      return; // 401 已跳登录页
    }

    await loadSettings();
    await loadOverview();

    // 支持 PWA 快捷方式 /#rules 这类深链
    const hash = (location.hash || '').replace('#', '');
    if (PAGE_META[hash]) go(hash);

    setInterval(() => { if (state.page === 'dashboard') loadOverview(); }, 20000);
  }

  document.addEventListener('DOMContentLoaded', boot);
})();
