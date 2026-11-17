/* ============================================================
 * app.js — Widget 状态与渲染（真实后端版）
 *
 * 来源：状态机与全部渲染结构移植自 widget-prototype/app.js（M0 验收基准），
 * 只把 mock 数据层换成真实接口：
 *   - 配置/欢迎语/示例/品牌色 ← GET /widget/config（行业词不写死）
 *   - 身份/宿主上下文       ← POST /widget/session（声明 + 在册校验）
 *   - 问答                  ← POST /chat SSE（structured/steps/citations/
 *                              conflict/gap/safe/error/done；ask 见 D-005）
 *   - 引用原文              ← GET /widget/evidence/{id}（展开时按需取）
 *   - 反馈 / 转人工         ← POST /widget/feedback、/widget/handoff
 *
 * 已知后端缺口（前端不造假数据，只保留原型 UI 结构）：
 *   D-005：后端在"关键字段已知但缺失必要字段、且问题对该字段敏感"时产生 `ask` 追问事件；前端
 *          renderAsk / pickClarify 按契约处理，选定后带 clarify 再请求一次。
 *   D-004：断网时主动中止 SSE；显示"连接中断"专用卡，网络恢复后自动重试一次。不续传。
 * ============================================================ */
(function () {
  'use strict';

  var params = new URLSearchParams(location.search);
  window.WidgetAPI.setServer(params.get('server') || '');
  /* 演示站入口码：loader 会把宿主页 URL 的 ?code= 透传进来（独立打开 widget.html 也支持） */
  var DEMO_CODE = params.get('code') || '';

  /* ---------- 内联 Lucide 图标（与原型同源，零依赖） ---------- */
  var I = {
    wrench: '<svg viewBox="0 0 24 24"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>',
    file: '<svg viewBox="0 0 24 24"><path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M10 9H8M16 13H8M16 17H8"/></svg>',
    ticket: '<svg viewBox="0 0 24 24"><rect width="8" height="4" x="8" y="2" rx="1"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><path d="M12 11h4M12 16h4M8 11h.01M8 16h.01"/></svg>',
    shield: '<svg viewBox="0 0 24 24"><path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="M12 8v4M12 16h.01"/></svg>',
    alert: '<svg viewBox="0 0 24 24"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4M12 17h.01"/></svg>',
    check: '<svg viewBox="0 0 24 24"><path d="M20 6 9 17l-5-5"/></svg>',
    checkCircle: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/></svg>',
    xCircle: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="m15 9-6 6M9 9l6 6"/></svg>',
    bookmark: '<svg viewBox="0 0 24 24"><path d="m19 21-7-4-7 4V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v16z"/></svg>',
    chev: '<svg viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg>',
    filter: '<svg viewBox="0 0 24 24"><polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/></svg>',
    retry: '<svg viewBox="0 0 24 24"><path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/></svg>',
    headphone: '<svg viewBox="0 0 24 24"><path d="M3 14h3a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-5a9 9 0 0 1 18 0v5a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3"/></svg>',
    zap: '<svg viewBox="0 0 24 24"><path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z"/></svg>',
    paperclip: '<svg viewBox="0 0 24 24"><path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57A4 4 0 1 1 18 8.84l-8.59 8.57a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>'
  };

  /* ---------- 状态 ---------- */
  var state = {
    config: null,        // GET /widget/config
    identity: { source: 'anonymous', claimedEngineerId: null },
    engineer: null,      // POST /widget/session 返回的 engineer
    hostContext: {},     // 宿主注入（postMessage）或 URL 参数
    sessionId: null,
    bootFail: false,     // 助手服务不可达（冷启动/重试仍失败）
    gateBlocked: false,  // 演示入口码缺失/无效：只提示入口，不当作服务故障
    offline: false,      // 浏览器真实断网（D-004：断流即中止，恢复后自动重试一次）
    sending: false,
    abort: null,         // 当前 /chat 的 AbortController（断网时主动中止流）
    pendingRetry: null,  // {text, clarify, node}：断流后等待 online 自动重试一次
    turnsLeft: null,     // 演示会话剩余轮次（null = 非演示站，不显示这一行）
    msgCount: 0,         // 本轮会话消息数（仅用于界面提示，转人工统计走后端）
    lastMessageId: null, // done / gap / safe 返回的本轮消息 id
    citeOrder: [],       // 引用角标编号：按步骤首次出现顺序
    citations: {},       // evidenceId -> Citation（来自 citations 事件）
    evidenceCache: {},   // evidenceId -> Evidence（原文，按需 GET）
    clarified: {},       // 追问选择缓存（同一问题只追问一次，D-005）
    demoFault: null      // 演示故障（X-Demo-Fault）：tool_timeout | not_found | null
  };

  /* ---------- DOM ---------- */
  function $(id) { return document.getElementById(id); }
  var wBody = $('wBody'), wInput = $('wInput'), wSend = $('wSend');
  var wNetbar = $('wNetbar'), wNetText = $('wNetText'), wCtxLine = $('wCtxLine');
  var wIdTag = $('wIdTag'), wIdNote = $('wIdNote'), wIdNoteText = $('wIdNoteText');
var wBrandTitle = $('wBrandTitle'), wFootHint = $('wFootHint');
  var wToasts = $('wToasts'), wHandoffHead = $('wHandoffHead'), wClose = $('wClose');
  var wQuota = $('wQuota');
  var modal = $('wHandoffModal'), wCarryList = $('wCarryList');
  var wHotline = $('wHotline'), wHotlineMeta = $('wHotlineMeta'), wHotlineNote = $('wHotlineNote');

  /* ---------- 小工具 ---------- */
  function el(tag, cls, html) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html !== undefined) n.innerHTML = html;
    return n;
  }
  /* 流式呈现期间跟随到底部（与原型"逐条呈现、始终看最新"一致）；
     用户主动上翻（滚轮/触摸）才暂停跟随，下一条问题再恢复。
     注意不能用 scroll 事件判断：scroll-behavior:smooth 的程序化滚动也会触发 scroll，
     会把"跟随"误判成"用户上翻"。 */
  var autoFollow = true;
  var activeTypers = 0;
  function scrollBottom() { wBody.scrollTop = wBody.scrollHeight; }
  function pauseFollow() { autoFollow = false; }
  wBody.addEventListener('wheel', pauseFollow, { passive: true });
  wBody.addEventListener('touchmove', pauseFollow, { passive: true });
  function toast(html) {
    var t = el('div', 'w-toast', html);
    wToasts.appendChild(t);
    setTimeout(function () { t.style.opacity = '0'; t.style.transition = 'opacity .3s'; }, 2400);
    setTimeout(function () { t.remove(); }, 2800);
  }
  function escapeHtml(value) {
    return String(value === null || value === undefined ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function textHtml(value) {
    return escapeHtml(value).replace(/\n/g, '<br>');
  }
  function reduceMotion() {
    return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }
  function escapeRegExp(value) {
    return String(value).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }

  /* 发送按钮可用性：空内容 / 发送中 / 服务不可达 / 断网 一律置灰（spec §4.2）。
     D-004：这里不再有"暂存待重发"状态（后端不支持断线续传），断网只提示。 */
  function syncSendState() {
    var empty = !wInput.value.trim();
    var blocked = state.bootFail || state.gateBlocked;
    wSend.disabled = state.sending || empty || blocked || state.offline;
    wInput.disabled = blocked;
    wSend.title = state.offline ? 'Network offline'
      : (state.gateBlocked ? 'Demo access link required'
        : (state.bootFail ? 'Assistant unavailable' : 'Send'));
  }

  /* ---------- 品牌色：由 /widget/config 注入（纪律 6），并派生浅色 ---------- */
  function hexToRgb(hex) {
    var m = /^#([0-9a-fA-F]{6})$/.exec(hex || '');
    if (!m) return null;
    var n = parseInt(m[1], 16);
    return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
  }
  function toHex(rgb) {
    function part(n) {
      var v = Math.max(0, Math.min(255, Math.round(n))).toString(16);
      return v.length === 1 ? '0' + v : v;
    }
    return '#' + part(rgb.r) + part(rgb.g) + part(rgb.b);
  }
  function mixColor(hex, targetHex, ratio) {
    var a = hexToRgb(hex), b = hexToRgb(targetHex);
    if (!a || !b) return hex;
    return toHex({
      r: a.r + (b.r - a.r) * ratio,
      g: a.g + (b.g - a.g) * ratio,
      b: a.b + (b.b - a.b) * ratio
    });
  }
  function applyBrand(hex) {
    if (!hexToRgb(hex)) return;
    var style = document.documentElement.style;
    style.setProperty('--green', hex);
    style.setProperty('--green-dark', mixColor(hex, '#000000', 0.2));
    style.setProperty('--green-soft', mixColor(hex, '#ffffff', 0.92));
    style.setProperty('--green-line', mixColor(hex, '#ffffff', 0.72));
  }
  function notifyBrand() {
    if (window.parent !== window && state.config && state.config.brandColor) {
      window.parent.postMessage({ type: 'moqian:brand', color: state.config.brandColor }, '*');
    }
  }
  function applyFeatures() {
    var features = (state.config && state.config.features) || {};
    if (features.handoff === false) {
      wHandoffHead.hidden = true;
    } else {
      wHandoffHead.hidden = false;
    }
  }

  /* ---------- 界面文案（全部来自 /widget/config，纪律 6） ---------- */
  function uiCopy() {
    return (state.config && state.config.ui) || {};
  }
  function brandName() {
    return uiCopy().productName || 'Support';
  }
  function fieldLabel(key, fallback) {
    var labels = uiCopy().fieldLabels || {};
    return labels[key] || fallback;
  }
  function applyUiCopy() {
    var ui = uiCopy();
    document.title = brandName();
    if (wBrandTitle) wBrandTitle.textContent = brandName();
    if (ui.inputPlaceholder && wInput) wInput.placeholder = ui.inputPlaceholder;
    if (ui.footerNote && wFootHint) wFootHint.textContent = ui.footerNote;
  }

  /* ---------- 身份标签与未识别提示（PRD §2.3） ---------- */
  function renderIdentity() {
    var id = state.engineer;
    if (!id) {
      wIdTag.textContent = 'Identifying...';
      wIdTag.classList.remove('is-anon');
      wIdNote.hidden = true;
      return;
    }
    wIdTag.textContent = id.identified
      ? (id.name + ' · Verified')
      : (id.claimed ? 'Not activated · ' + id.claimed : 'Not signed in');
    wIdTag.classList.toggle('is-anon', !id.identified);
    wIdTag.title = id.identified
      ? 'Identity source: ' + (id.sourceLabel || id.source) + ' (account ' + id.account + ')'
      : 'Identity is a declaration plus a directory check, not authentication.';

    if (id.identified) {
      wIdNote.hidden = true;
      return;
    }
    wIdNote.hidden = false;
    wIdNoteText.textContent = id.claimed
      ? 'Account ' + id.claimed + ' is not activated yet. You can still chat and reach a human agent.'
      : 'Not signed in. You can still chat, but answers won\'t be linked to your account.';
  }

  /* ---------- 上下文提示行 ---------- */
  function renderCtxLine() {
    var ctx = state.hostContext || {};
    var loaded = !!(ctx.orderNo || ctx.country || ctx.ticketNo || ctx.deviceModel);
    wCtxLine.textContent = loaded
      ? 'Order context loaded'
      : 'No order context · searching all policies';
  }

  /* ---------- 欢迎屏（欢迎语/示例来自 /widget/config） ---------- */
  function renderWelcome() {
    var cfg = state.config || {};
    var wrap = el('div', 'w-welcome');
    wrap.appendChild(el('div', 'w-welcome__hi', brandName()));
    var id = state.engineer;
    var welcome = cfg.welcome || 'Hi! I can help with orders, shipping, returns and our policies.';
    welcome = welcome.replace('{name}', id && id.identified ? id.name : 'there');
    wrap.appendChild(el('p', null, escapeHtml(welcome)));

    var examples = cfg.examples || [];
    if (examples.length) {
      var chips = el('div', 'w-chips');
      examples.forEach(function (ex) {
        var b = el('button', 'w-chip');
        b.innerHTML = I.zap + '<span>' + escapeHtml(ex.q) + (ex.tag ? '<small>' + escapeHtml(ex.tag) + '</small>' : '') + '</span>';
        b.addEventListener('click', function () {
          if (!state.sending) sendQuestion(ex.q);
        });
        chips.appendChild(b);
      });
      wrap.appendChild(chips);
    }

    /* 示例之外还有"转人工"这条出路：让访客一眼看到系统不止会答题（转人工弹窗里带热线与值班时间） */
    if (!(cfg.features && cfg.features.handoff === false)) {
      var handoffRow = el('div', 'w-welcome__handoff');
      var handoffLink = el(
        'button',
        'w-handoff-link',
        I.headphone + '<span>Still stuck? Talk to a human agent</span>'
      );
      handoffLink.title = cfg.handoffText || 'Talk to a human agent';
      handoffLink.addEventListener('click', function () { openHandoff(null); });
      handoffRow.appendChild(handoffLink);
      wrap.appendChild(handoffRow);
    }

    var empty = el('div', 'w-msg w-msg--ai');
    empty.appendChild(wrap);
    wBody.appendChild(empty);
  }

  /* ---------- 服务初始化：config + session ---------- */
  async function loadService(reuseConfig) {
    state.bootFail = false;
    state.gateBlocked = false;
    if (!reuseConfig || !state.config) {
      try {
        state.config = (await window.WidgetAPI.getConfig()) || {};
      } catch (e) {
        state.config = null;
        state.bootFail = true;
        renderBootFail();
        syncSendState();
        return false;
      }
      applyBrand(state.config.brandColor);
      applyFeatures();
      applyUiCopy();
      notifyBrand();
    }
    applyDemoState();
    try {
      var data = await window.WidgetAPI.createSession({
        identity: {
          source: state.identity.source || 'anonymous',
          claimedEngineerId: state.identity.claimedEngineerId || null
        },
        hostContext: state.hostContext || {},
        demoCode: DEMO_CODE || null
      });
      state.sessionId = data.sessionId;
      state.engineer = data.engineer || null;
      if (data.hostContext) state.hostContext = data.hostContext;
    } catch (e) {
      if (e && (e.code === 'DEMO_CODE_REQUIRED' || e.code === 'DEMO_CODE_INVALID'
        || e.code === 'DEMO_PAUSED')) {
        state.bootFail = false;
        state.gateBlocked = true;
        renderDemoGate(e);
        syncSendState();
        return false;
      }
      state.bootFail = true;
      renderBootFail();
      syncSendState();
      return false;
    }
    if (wBody.querySelector('.w-bootfail')) wBody.innerHTML = '';
    renderIdentity();
    renderCtxLine();
    if (!wBody.querySelector('.w-welcome') && !wBody.querySelector('.w-msg--user')) {
      renderWelcome();
    }
    syncSendState();
    return true;
  }

  async function ensureSession() {
    if (state.sessionId) return state.sessionId;
    var data = await window.WidgetAPI.createSession({
      identity: {
        source: state.identity.source || 'anonymous',
        claimedEngineerId: state.identity.claimedEngineerId || null
      },
      hostContext: state.hostContext || {},
      demoCode: DEMO_CODE || null
    });
    state.sessionId = data.sessionId;
    if (data.engineer) state.engineer = data.engineer;
    if (data.hostContext) state.hostContext = data.hostContext;
    renderIdentity();
    renderCtxLine();
    return state.sessionId;
  }

  /* ---------- 发送主流程 ---------- */
  function sendQuestion(text) {
    text = (text || wInput.value).trim();
    if (!text || state.sending || state.bootFail || state.offline) return;
    wInput.value = '';
    autoGrow();
    syncSendState();
    sendTurn(text);
  }

  async function sendTurn(text, retryNode, clarify) {
    state.sending = true;
    /* 每次真正发问都会在后端算一轮（拒答也算），所以先本地扣一次用于提示；
       追平以 /widget/config 的 sessionTurns 为准，后端 refused/quota 仍是裁判。 */
    if (typeof state.turnsLeft === 'number' && state.turnsLeft > 0) {
      state.turnsLeft -= 1;
      paintQuota();
    }
    autoFollow = true;
    syncSendState();
    if (!retryNode) {
      appendUserBubble(text);
      state.msgCount += 2;
    }
    var aiMsg = retryNode || beginAIMessage();
    if (retryNode) resetAILoading(retryNode);
    try {
      await runTurn(text, aiMsg, clarify);
    } finally {
      state.sending = false;
      syncSendState();
      scrollBottom();
    }
  }

  async function runTurn(text, aiMsg, clarify) {
    var sessionId;
    try {
      sessionId = await ensureSession();
    } catch (e) {
      renderFailure(aiMsg, text, { code: e.code, message: e.message || 'Could not start the session. Please retry.', retryable: true });
      return;
    }

    state.citeOrder = [];
    state.citations = {};
    state.lastMessageId = null;
    var card = aiMsg.querySelector('.w-ai-card');
    var failed = false;
    var question = text;
    /* 追问选择：本次传入优先，其次沿用同一问题之前的确认（一轮为限，PRD FR-Q2.3） */
    var selection = clarify || state.clarified[text] || null;
    var ctrl = typeof AbortController === 'function' ? new AbortController() : null;
    state.abort = ctrl;

    function fail(error) {
      if (failed) return;
      failed = true;
      renderFailure(aiMsg, question, error || {});
    }

    try {
      await window.WidgetAPI.streamChat(
        {
          sessionId: sessionId,
          question: question,
          context: state.hostContext,
          clarify: selection,
          fault: state.demoFault || null,
          signal: ctrl ? ctrl.signal : undefined,
        },
        function (name, payload) {
          if (failed) return;
          if (name === 'structured') {
            renderStructured(card, payload.structured || {}, payload.filterNote);
          } else if (name === 'conflict') {
            renderConflict(card, payload || {});
          } else if (name === 'steps') {
            renderStep(card, payload || {});
          } else if (name === 'tool') {
            renderTool(card, payload || {});
          } else if (name === 'degraded') {
            renderDegraded(aiMsg, payload || {});
          } else if (name === 'citations') {
            state.citations = buildCitationMap(payload.items);
            renderCitationBlock(card);
          } else if (name === 'ask') {
            renderAsk(aiMsg, payload.ask || payload, question);
          } else if (name === 'gap') {
            renderGap(aiMsg, payload.gap || {});
          } else if (name === 'safe') {
            renderSafe(aiMsg, payload.safe || {});
          } else if (name === 'handoff') {
            renderHandoff(aiMsg, payload || {});
          } else if (name === 'refused') {
            renderQuotaRefused(aiMsg, payload.refused || {});
          } else if (name === 'done') {
            state.lastMessageId = payload.messageId || null;
            renderActionBar(card, payload);
          } else if (name === 'error') {
            fail(payload.error || {});
          }
          scrollBottom();
        }
      );
    } catch (e) {
      if (failed) return;
      var disconnected = (typeof navigator !== 'undefined' && navigator.onLine === false)
        || (e && e.name === 'AbortError');
      if (disconnected) {
        state.pendingRetry = { text: question, clarify: selection, node: aiMsg };
        renderFailure(aiMsg, question, {
          code: 'NETWORK',
          message: 'The connection dropped before the answer finished. We’ll retry once the network is back, or hit Reconnect to retry now.',
          retryable: true,
        });
      } else {
        fail({ code: e && e.code, message: (e && e.message) || 'Network error - no answer was generated.', retryable: true });
      }
    } finally {
      if (state.abort === ctrl) state.abort = null;
    }
  }

  function resetAILoading(aiMsg) {
    var card = aiMsg.querySelector('.w-ai-card');
    if (!card) return;
    card.innerHTML = '';
    card.appendChild(aiHead('Searching the knowledge base…'));
  }

  /* 失败兜底：不在原位留半成品，给出「重试 / 转人工」两条出路（spec §4.8A） */
  function renderFailure(aiMsg, text, error) {
    var card = aiMsg.querySelector('.w-ai-card');
    if (!card) return;
    card.innerHTML = '';
    card.appendChild(aiHead(brandName()));

    var retryable = error.retryable !== false;
    var isNetwork = error.code === 'NETWORK';
    var title = isNetwork
      ? 'Connection dropped before the answer finished.'
      : (error.code === 'CHAT_FAILED' || !error.code ? 'Service temporarily unavailable. Please retry.' : 'Request failed. Please retry.');
    var body = error.message || 'The model service timed out or returned an error. If it keeps failing, hand off to a human agent — your conversation goes with you.';
    if (!retryable) body += '\n(Retrying may not help - talk to a human agent instead.)';
    /* 重试 = 整包重问当前问题（D13 允许；不重复用户气泡）。
       契约里 retryable=false 只作提示，界面仍保留重试入口，避免用户无路可走。 */
    var retryLabel = isNetwork ? 'Reconnect' : 'Retry';
    var ops = '<button class="w-refuse__btn" data-act="retry">' + I.retry + retryLabel + '</button>' +
      '<button class="w-refuse__btn w-refuse__btn--ghost" data-act="handoff">' + I.headphone + 'Human agent</button>';

    var box = el('div', 'w-refuse w-refuse--gap');
    box.innerHTML =
      '<div>' + I.alert + '</div>' +
      '<div><div class="w-refuse__title">' + escapeHtml(title) + '</div>' +
      '<p>' + textHtml(body) + '</p>' +
      '<div class="w-refuse__ops">' + ops + '</div></div>';
    var retryBtn = box.querySelector('[data-act="retry"]');
    if (retryBtn) {
      retryBtn.addEventListener('click', function () {
        if (state.sending || state.bootFail) return;
        if (state.offline) {
          toast(I.alert + 'Network still offline - we’ll retry your question automatically.');
          return;
        }
        state.pendingRetry = null;
        sendTurn(text, aiMsg);
      });
    }
    box.querySelector('[data-act="handoff"]').addEventListener('click', function () {
      openHandoff(state.lastMessageId);
    });
    card.appendChild(box);
    scrollBottom();
  }

  /* 服务不可达（连不上后端）：面板内给失败态与重试入口，宿主页面不受影响（spec §4.8B） */
  function renderBootFail() {
    wBody.innerHTML = '';
    var wrap = el('div', 'w-bootfail');
    var box = el('div', 'w-refuse w-refuse--gap');
    box.innerHTML =
      '<div>' + I.alert + '</div>' +
      '<div><div class="w-refuse__title">Connection failed - the assistant could not be loaded.</div>' +
      '<p>The assistant endpoint is unreachable (network down or the backend is not running). The host page is unaffected; please retry, or talk to a human agent.</p>' +
      '<div class="w-refuse__ops">' +
      '<button class="w-refuse__btn" data-act="retry">' + I.retry + 'Reconnect</button>' +
      '<button class="w-refuse__btn w-refuse__btn--ghost" data-act="handoff">' + I.headphone + 'Human agent</button>' +
      '</div></div>';
    box.querySelector('[data-act="retry"]').addEventListener('click', retryBoot);
    box.querySelector('[data-act="handoff"]').addEventListener('click', function () {
      openHandoff(null);
    });
    wrap.appendChild(box);
    wBody.appendChild(wrap);
    syncSendState();
  }

  async function retryBoot() {
    if (!state.bootFail) return;
    var ok = await loadService(true);
    if (ok) {
      wBody.innerHTML = '';
      renderWelcome();
      syncSendState();
      toast(I.check + 'Assistant reconnected.');
    } else {
      toast(I.alert + 'Still can’t reach the assistant. Please retry shortly.');
    }
  }

  /* 演示会话剩余轮次：只显示「本次会话还可提问 N 次」。
     每日额度（码 / IP / 全局）不下发，也不显示——那是运营参数。
     前端计数只用于提示，真正的裁判仍是后端（refused/quota）。 */
  function applyDemoState() {
    var demo = (state.config && state.config.demo) || {};
    var limit = Number(demo.sessionTurns || 0);
    state.turnsLeft = (demo.enabled && limit > 0) ? limit : null;
    paintQuota();
  }

  function paintQuota() {
    if (!wQuota) return;
    if (typeof state.turnsLeft !== 'number') {
      wQuota.hidden = true;
      wQuota.textContent = '';
      return;
    }
    wQuota.hidden = false;
    wQuota.textContent = state.turnsLeft > 0
      ? 'Questions left in this demo: ' + state.turnsLeft
      : 'You’ve used up this session’s questions; reopening the assistant starts a new conversation.';
  }

  /* 演示入口码缺失/无效/演示暂停：这是"从哪里进入 / 还开不开放"的问题，不是服务故障，
     所以只给说明与预约入口。具体规则由后端返回，前端不猜。 */
  function renderDemoGate(error) {
    wBody.innerHTML = '';
    var wrap = el('div', 'w-bootfail');
    var box = el('div', 'w-refuse w-refuse--safe');
    var demo = (state.config && state.config.demo) || {};
    var booking = demo.bookingUrl || '';
    var paused = !!error && error.code === 'DEMO_PAUSED';
    var title = paused
      ? (demo.pausedTitle || 'Demo paused')
      : ((error && error.code === 'DEMO_CODE_INVALID')
        ? 'Invalid or expired access code'
        : 'This demo is only available through the official link');
    var body = paused
      ? (demo.pausedText || (error && error.message) || '')
      : 'Please reopen the demo from the official case page — the link includes the '
        + 'access code. If you got this address elsewhere, book a 1:1 walkthrough '
        + 'and we\'ll go through your scenarios.';
    box.innerHTML =
      '<div>' + I.shield + '</div>' +
      '<div><div class="w-refuse__title">' + escapeHtml(title) + '</div>' +
      '<p>' + textHtml(body) + '</p>' +
      '<div class="w-refuse__ops">' +
      (booking
        ? '<a class="w-refuse__btn" href="' + escapeHtml(booking) + '" target="_blank" rel="noopener">Book a demo</a>'
        : '') +
      '</div></div>';
    wrap.appendChild(box);
    wBody.appendChild(wrap);
    wCtxLine.textContent = paused ? 'Demo unavailable' : 'Demo access not verified';
    scrollBottom();
  }

  /* 演示额度用完（后端 refused/quota）：给"预约 1:1 演示"的出口，不展示具体阈值。 */
  function renderQuotaRefused(aiMsg, info) {
    var card = aiMsg.querySelector('.w-ai-card');
    if (!card) return;
    card.innerHTML = '';
    card.appendChild(aiHead(brandName()));
    var box = el('div', 'w-refuse w-refuse--gap');
    var url = info.bookingUrl || ((state.config && state.config.demo) || {}).bookingUrl || '';
    box.innerHTML =
      '<div>' + I.shield + '</div>' +
      '<div><div class="w-refuse__title">' + escapeHtml(info.title || 'Demo quota reached') + '</div>' +
      '<p>' + textHtml(info.body || '') + '</p>' +
      '<div class="w-refuse__ops">' +
      (url
        ? '<a class="w-refuse__btn" href="' + escapeHtml(url) + '" target="_blank" rel="noopener">Book a 1:1 demo</a>'
        : '') +
      '</div></div>';
    card.appendChild(box);
    if (info.kind === 'paused') {
      state.turnsLeft = null;   // 暂停不是"用完"，不再显示剩余次数
    } else if (info.reason === 'session') {
      state.turnsLeft = 0;
    }
    paintQuota();
    scrollBottom();
  }

  /* ---------- 断网提示（真实 navigator offline/online；不做自动重发） ---------- */
  function renderOffline() {
    wNetbar.hidden = !state.offline;
    if (state.offline) wNetText.textContent = 'Network offline. We’ll retry when it’s back (partial answers are not resumed).';
    syncSendState();
  }

  /* ---------- 消息外壳 ---------- */
  function aiHead(text) {
    return el('div', 'w-ai-head', '<span class="w-avatar">' + I.wrench + '</span><span>' + escapeHtml(text) + '</span>');
  }
  function appendUserBubble(text) {
    var m = el('div', 'w-msg w-msg--user');
    m.appendChild(el('div', 'w-bubble', escapeHtml(text)));
    wBody.appendChild(m);
    scrollBottom();
  }
  function beginAIMessage() {
    var m = el('div', 'w-msg w-msg--ai');
    var card = el('div', 'w-ai-card');
    card.appendChild(aiHead('Searching the knowledge base…'));
    m.appendChild(card);
    wBody.appendChild(m);
    scrollBottom();
    return m;
  }

  /* ---------- 结构化理解卡 ---------- */
  function renderStructured(card, structured, filterNote) {
    var s = structured || {};
    function kv(label, value) {
      return value
        ? '<span class="w-kv">' + label + ' <b>' + escapeHtml(value) + '</b></span>'
        : '<span class="w-kv w-kv--empty">' + label + ' not provided · search all</span>';
    }
    var box = el('div', 'w-struct');
    box.innerHTML =
      '<div class="w-struct__title">' + I.filter + 'Structured query (filled from your message — missing fields are not guessed)</div>' +
      '<div class="w-struct__fields">' +
      kv(fieldLabel('intent', 'Intent'), s.intent) +
      kv(fieldLabel('orderNo', 'Order'), s.orderNo) +
      kv(fieldLabel('country', 'Country'), s.country) +
      kv(fieldLabel('trackingNo', 'Tracking'), s.trackingNo) +
      '</div>' +
      (filterNote ? '<div class="w-struct__filter">' + I.check + escapeHtml(filterNote) + '</div>' : '');
    card.appendChild(box);
    return box;
  }

  /* ---------- 多版本冲突提示（纪律 7：并列展示，不替工程师选择） ---------- */
  function renderConflict(card, payload) {
    var options = payload.options || [];
    var optHtml = options.length
      ? '<div class="w-conflict__opts">' + options.map(function (opt) {
          var label = escapeHtml(opt.label || '');
          var count = (opt.evidenceIds || []).length;
          return '· ' + label + (count ? ' (' + count + ' sources)' : '');
        }).join('<br>') + '</div>'
      : '';
    var box = el('div', 'w-conflict');
    box.innerHTML =
      '<div>' + I.alert + '</div>' +
      '<div><b>Multiple versions matched - the system won’t choose for you</b><br>' +
      escapeHtml(payload.conflict || '') + optHtml + '</div>';
    card.appendChild(box);
    return box;
  }

  /* ---------- 步骤清单（打字机 + 引用角标，spec §4.3③） ---------- */
  function stepVisibleText(text, citations) {
    var ids = citations || [];
    var cleaned = String(text || '');
    if (ids.length) {
      var pattern = new RegExp('[\\[【]\\s*(' + ids.map(escapeRegExp).join('|') + ')\\s*[\\]】]', 'g');
      cleaned = cleaned.replace(pattern, '');
      /* 模型有时把证据 ID 直接写进正文（不带括号）：M29023规定…，这类原始标记同样隐藏，
         统一由行尾编号角标承担引用关系（否则业务读者会看到满屏 M 编号）。 */
      var bare = new RegExp(
        '(^|[^A-Za-z0-9])(' + ids.map(escapeRegExp).join('|') + ')(?=[^A-Za-z0-9]|$)',
        'g'
      );
      cleaned = cleaned.replace(bare, '$1');
    }
    return cleaned
      /* 生成截断可能留下半个引用标记（如行尾的 "[M"），连同空白一起清掉 */
      .replace(/[\s]*[\[【]\s*M[0-9]*\s*$/u, '')
      .replace(/[ \t]{2,}/g, ' ')
      .replace(/[ \t]+([，。；、！？])/g, '$1')
      .trim();
  }

  function renderStep(card, payload) {
    if (!card.querySelector('.w-steps-label')) {
      card.appendChild(el('div', 'w-cites__label w-steps-label', I.check + 'Answer details'));
    }
    var ol = card.querySelector('.w-steps');
    if (!ol) {
      ol = el('ol', 'w-steps');
      card.appendChild(ol);
    }
    var li = el('li');
    li.appendChild(el('span', 'w-steps__no', String(payload.index)));
    var txt = el('span', 'w-steps__txt');
    li.appendChild(txt);
    ol.appendChild(li);

    var ids = (payload.citations || []).slice();
    ids.forEach(function (id) {
      if (state.citeOrder.indexOf(id) < 0) state.citeOrder.push(id);
    });
    var visible = stepVisibleText(payload.text, ids);
    typewriter(txt, visible).then(function () { appendCiteRefs(txt, ids); });
    scrollBottom();
  }

  function appendCiteRefs(node, ids) {
    ids.forEach(function (id) {
      var index = state.citeOrder.indexOf(id);
      if (index < 0) return;
      var sup = el('sup', 'w-cite-ref', String(index + 1));
      sup.dataset.cite = id;
      sup.title = 'Verify the source of this step';
      sup.addEventListener('click', function () { focusCitation(id); });
      node.appendChild(sup);
    });
  }

  function typewriter(node, text) {
    if (reduceMotion() || !text) {
      node.textContent = text || '';
      return Promise.resolve();
    }
    activeTypers += 1;
    return new Promise(function (resolve) {
      var i = 0;
      /* 真实答案单步可能很长（100–300 字）：按长度自适应步长，整步约 0.6–0.9s 呈现完 */
      var step = Math.max(2, Math.ceil(text.length / 45));
      var timer = setInterval(function () {
        i += step;
        node.textContent = text.slice(0, i);
        if (autoFollow) scrollBottom();
        if (i >= text.length) {
          clearInterval(timer);
          activeTypers -= 1;
          resolve();
        }
      }, 16);
    });
  }

  /* 点步骤角标 → 定位并展开对应引用卡，短暂高亮（spec §4.3④） */
  function focusCitation(id) {
    var target = wBody.querySelector('.w-cite[data-cite="' + id + '"]');
    if (!target) return;
    target.classList.add('is-open', 'is-flash');
    loadEvidence(target, id);
    target.scrollIntoView({ block: 'nearest', behavior: reduceMotion() ? 'auto' : 'smooth' });
    setTimeout(function () { target.classList.remove('is-flash'); }, 1700);
  }

  /* ---------- 引用卡（点击展开原文；原文来自 GET /widget/evidence/{id}） ---------- */
  function buildCitationMap(items) {
    var map = {};
    (items || []).forEach(function (item) {
      if (!item || !item.evidenceId) return;
      map[item.evidenceId] = item;
      if (state.citeOrder.indexOf(item.evidenceId) < 0) state.citeOrder.push(item.evidenceId);
    });
    return map;
  }

  function renderCitationBlock(card) {
    if (card.querySelector('.w-cites--all') || !state.citeOrder.length) return;
    var entries = state.citeOrder.map(function (id, i) { return { id: id, no: i + 1 }; });
    var box = renderCitations(entries, { numbered: true, label: true });
    box.classList.add('w-cites--all');
    card.appendChild(box);
  }

  function renderCitations(entries, opts) {
    var o = opts || {};
    var wrap = el('div', 'w-cites');
    if (o.label) {
      wrap.appendChild(el('div', 'w-cites__label', I.file + 'Sources (click to verify)'));
    }
    entries.forEach(function (entry) {
      var id = entry.id;
      var item = state.citations[id] || { evidenceId: id, kind: 'manual', title: id, loc: '' };
      var kindClass = item.kind === 'ticket' ? ' w-cite--ticket' : '';
      var icon = item.kind === 'ticket' ? I.ticket : I.file;
      var title = escapeHtml(item.title || id);
      /* 后端 citation.title 已带来源（如 Returns & refunds policy），loc 是章节/位置；
         去掉重复章节，只保留页码段，避免 "§4.4.1 §4.4.1 · P10–11"。 */
      var locText = item.loc || '';
      var titleText = item.title || id;
      var section = locText.split('·')[0].trim();
      if (section && titleText.indexOf(section) >= 0 && locText.indexOf('·') >= 0) {
        locText = '· ' + locText.slice(locText.indexOf('·') + 1).trim();
      }
      var meta = locText ? '<span class="w-cite__meta"> ' + escapeHtml(locText) + '</span>' : '';
      var batch = item.batch ? '<span class="w-cite__batch">' + escapeHtml(item.batch) + '</span>' : '';

      var c = el('div', 'w-cite' + kindClass);
      c.dataset.cite = id;
      c.innerHTML =
        '<div class="w-cite__head">' +
        '<span class="w-cite__lead">' +
        (o.numbered && entry.no ? '<span class="w-cite__no">' + entry.no + '</span>' : '') + icon +
        '</span>' +
        '<span class="w-cite__title">' + title + meta + '</span>' +
        '<span class="w-cite__chev">' + I.chev + '</span>' +
        batch +
        '</div>' +
        '<div class="w-cite__orig"></div>';
      c.addEventListener('click', function () {
        var open = c.classList.toggle('is-open');
        if (open) loadEvidence(c, id);
      });
      wrap.appendChild(c);
      if (o.store !== false) state.citations[id] = item;
    });
    return wrap;
  }

  async function loadEvidence(card, id) {
    var box = card.querySelector('.w-cite__orig');
    if (!box || box.dataset.loaded === '1' || box.dataset.loading === '1') return;
    if (state.config && state.config.features && state.config.features.evidence === false) {
      box.textContent = 'Source text is disabled in this deployment';
      box.dataset.loaded = '1';
      return;
    }
    if (state.evidenceCache[id]) {
      box.textContent = state.evidenceCache[id].text || '(empty source text)';
      box.dataset.loaded = '1';
      return;
    }
    box.dataset.loading = '1';
    box.classList.add('is-loading');
    box.textContent = 'Loading source...';
    try {
      var sessionId = await ensureSession();
      var evidence = await window.WidgetAPI.getEvidence(sessionId, id);
      state.evidenceCache[id] = evidence;
      box.textContent = evidence.text || '(empty source text)';
      box.dataset.loaded = '1';
      /* 后端原文里可能有更完整的标题/出处，回填卡片（失败不影响已展示内容） */
      var titleNode = card.querySelector('.w-cite__title');
      if (titleNode && evidence.title) {
        var meta = evidence.loc ? '<span class="w-cite__meta"> ' + escapeHtml(evidence.loc) + '</span>' : '';
        titleNode.innerHTML = escapeHtml(evidence.title) + meta;
      }
    } catch (e) {
      box.textContent = (e && e.message) || 'Could not load the source. Please retry.';
    } finally {
      box.dataset.loading = '0';
      box.classList.remove('is-loading');
    }
  }

  /* ---------- 操作条：已解决 / 没解决 / 收藏（真实 POST /widget/feedback） ---------- */
  async function pushFeedback(messageId, type) {
    var sessionId = await ensureSession();
    await window.WidgetAPI.sendFeedback(sessionId, messageId, type);
  }

  function renderActionBar(card, donePayload) {
    if (card.querySelector('.w-actions')) return;
    if (state.config && state.config.features && state.config.features.feedback === false) return;
    var messageId = (donePayload && donePayload.messageId) || state.lastMessageId;
    if (!messageId) return;

    var bar = el('div', 'w-actions');
    var ok = el('button', 'w-act w-act--ok', I.checkCircle + '<span>Resolved</span>');
    var no = el('button', 'w-act w-act--no', I.xCircle + '<span>Not resolved</span>');
    var save = el('button', 'w-act w-act--save', I.bookmark + '<span>Save to KB</span>');

    ok.addEventListener('click', async function () {
      if (ok.classList.contains('is-active') || state.sending) return;
      ok.classList.add('is-active');
      no.classList.remove('is-active');
      try {
        await pushFeedback(messageId, 'resolved');
        var id = state.engineer;
        toast(I.check + 'Marked as resolved - time to locate recorded.'
          + (id && id.identified ? '' : '(identity not identified - excluded from per-person metrics)'));
      } catch (e) {
        ok.classList.remove('is-active');
        toast(I.alert + 'Feedback failed: ' + escapeHtml((e && e.message) || 'please retry'));
      }
    });

    no.addEventListener('click', async function () {
      if (no.classList.contains('is-active') || state.sending) return;
      no.classList.add('is-active');
      ok.classList.remove('is-active');
      try {
        await pushFeedback(messageId, 'unresolved');
        toast(I.xCircle + 'Logged as a bad case for review. Thanks!');
      } catch (e) {
        no.classList.remove('is-active');
        toast(I.alert + 'Feedback failed: ' + escapeHtml((e && e.message) || 'please retry'));
      }
    });

    save.addEventListener('click', async function () {
      if (save.classList.contains('is-active') || state.sending) return;
      save.classList.add('is-active');
      try {
        await pushFeedback(messageId, 'saved');
        var id = state.engineer;
        toast(I.bookmark + (id && id.identified
          ? 'Saved - a supervisor must approve before it enters the knowledge base.'
          : 'Saved (author not identified); a supervisor can still approve it.'));
      } catch (e) {
        save.classList.remove('is-active');
        toast(I.alert + 'Save failed: ' + escapeHtml((e && e.message) || 'please retry'));
      }
    });

    bar.appendChild(ok);
    bar.appendChild(no);
    bar.appendChild(save);
    bar.appendChild(el('span', 'w-act-note', 'Your feedback shapes the knowledge base'));
    card.appendChild(bar);
    scrollBottom();
  }

  /* ---------- 单轮追问卡（D-005：后端暂不产生 ask 事件；结构按契约保留） ---------- */
  function renderAsk(aiMsg, ask, question) {
    var card = aiMsg.querySelector('.w-ai-card');
    if (!card) return;
    card.innerHTML = '';
    card.appendChild(aiHead(brandName() + ' · one quick check'));
    var box = el('div', 'w-ask');
    var options = ask.options || [];
    box.innerHTML =
      '<div class="w-ask__title">' + I.filter + escapeHtml(ask.title || 'One detail first') + '</div>' +
      '<p class="w-ask__body">' + textHtml(ask.body || '') + '</p>' +
      '<div class="w-ask__opts">' + options.map(function (opt) {
        return '<button class="w-ask__opt" data-value="' + escapeHtml(opt.value) + '">' +
          '<b>' + escapeHtml(opt.label || opt.value) + '</b>' +
          '<small>' + escapeHtml(opt.hint || '') + '</small></button>';
      }).join('') + '</div>' +
      '<div class="w-ask__hint">We ask only once; after you choose, the answer comes straight through.</div>';
    box.querySelectorAll('.w-ask__opt').forEach(function (btn) {
      btn.addEventListener('click', function () {
        if (state.sending) return;
        var opt = options.filter(function (o) { return String(o.value) === btn.dataset.value; })[0];
        if (opt) pickClarify(aiMsg, question, ask, opt);
      });
    });
    card.appendChild(box);
    scrollBottom();
  }

  async function pickClarify(aiMsg, question, ask, opt) {
    aiMsg.querySelectorAll('.w-ask__opt').forEach(function (btn) {
      btn.disabled = true;
      btn.classList.toggle('is-picked', btn.dataset.value === String(opt.value));
    });
    var selection = { field: ask.field, value: opt.value, label: opt.label || opt.value };
    state.clarified[question] = selection;  // 记住：同一问题不再追问第二次
    appendUserBubble(opt.label || opt.value);
    state.msgCount += 2;
    var next = beginAIMessage();
    await sendTurn(question, next, selection);
  }

  /* ---------- 拒答卡（证据不足必须拒答并转人工，PRD §5.2） ---------- */
  function renderGap(aiMsg, gap) {
    var card = aiMsg.querySelector('.w-ai-card');
    if (!card) return;
    card.innerHTML = '';
    card.appendChild(aiHead(brandName()));
    if (gap.messageId) state.lastMessageId = gap.messageId;

    var box = el('div', 'w-refuse w-refuse--gap');
    box.innerHTML =
      '<div>' + I.xCircle + '</div>' +
      '<div><div class="w-refuse__title">' + escapeHtml(gap.title || 'No reliable evidence found - not answering this time.') + '</div>' +
      '<p>' + textHtml(gap.body || '') + '</p>' +
      '<button class="w-refuse__btn" data-act="handoff">' + I.headphone + 'Hand to a human agent</button></div>';
    box.querySelector('[data-act="handoff"]').addEventListener('click', function () {
      openHandoff(gap.messageId || state.lastMessageId);
    });
    card.appendChild(box);
    scrollBottom();
  }

  /* ---------- 安全卡（不生成任何操作步骤，只提示联系厂家并引用安规） ---------- */
  function renderSafe(aiMsg, safe) {
    var card = aiMsg.querySelector('.w-ai-card');
    if (!card) return;
    card.innerHTML = '';
    card.appendChild(aiHead('Safety check'));
    if (safe.messageId) state.lastMessageId = safe.messageId;

    var box = el('div', 'w-refuse w-refuse--safe');
    box.innerHTML =
      '<div>' + I.shield + '</div>' +
      '<div><div class="w-refuse__title">' + escapeHtml(safe.title || 'High-risk operation - no procedure steps provided.') + '</div>' +
      '<p>' + textHtml(safe.body || '') + '</p></div>';

    var citeWrap = el('div');
    var items = safe.citations || [];
    if (items.length) {
      state.citeOrder = [];
      buildCitationMap(items);
      citeWrap.appendChild(renderCitations(items.map(function (item) { return { id: item.evidenceId }; }), { compact: true, store: false }));
    }
    var btn = el('button', 'w-refuse__btn');
    btn.innerHTML = I.headphone + 'Contact the manufacturer / certified technician';
    btn.addEventListener('click', function () { openHandoff(safe.messageId || state.lastMessageId); });
    citeWrap.appendChild(btn);
    box.children[1].appendChild(citeWrap);
    card.appendChild(box);
    scrollBottom();
  }

  /* ---------- 工具查询卡（P3：订单/物流查询的过程与结果） ---------- */
  function renderTool(card, payload) {
    var status = payload.status || 'running';
    var labels = {
      running: 'Looking up your order…',
      ok: 'Order lookup complete',
      not_found: 'Order not found',
      timeout: 'Order lookup timed out',
      error: 'Order lookup failed'
    };
    var icons = {
      ok: I.check,
      running: '',
      not_found: I.xCircle,
      timeout: I.xCircle,
      error: I.xCircle
    };
    var box = el('div', 'w-tool w-tool--' + status);
    box.innerHTML =
      '<span class="w-tool__icon">' + (icons[status] || '') + '</span>' +
      '<span class="w-tool__text">' + escapeHtml(labels[status] || status) +
      (payload.latencyMs ? '<small>' + escapeHtml(String(payload.latencyMs)) + ' ms</small>' : '') +
      '</span>';
    card.appendChild(box);
  }

  /* ---------- 转人工卡（四类硬转 / 连续失败）：固定话术 + 联系方式 ---------- */
  function renderHandoff(aiMsg, payload) {
    var card = aiMsg.querySelector('.w-ai-card');
    if (!card) return;
    card.innerHTML = '';
    card.appendChild(aiHead(brandName()));
    if (payload.messageId) state.lastMessageId = payload.messageId;

    var labels = {
      refund: 'Refund request',
      complaint: 'Complaint',
      legal: 'Legal matter',
      account: 'Account security'
    };
    var box = el('div', 'w-refuse w-refuse--safe');
    box.innerHTML =
      '<div>' + I.headphone + '</div>' +
      '<div><div class="w-refuse__title">' +
      escapeHtml(labels[payload.category] || 'Routed to a human agent') + '</div>' +
      '<p>' + textHtml(payload.text || '') + '</p>' +
      '<p class="w-refuse__meta">' + escapeHtml(
        (payload.hotline || '') + (payload.hours ? ' · ' + payload.hours : '')
      ) + '</p>' +
      '<button class="w-refuse__btn" data-act="handoff">' + I.headphone +
      'Talk to a human agent</button></div>';
    box.querySelector('[data-act="handoff"]').addEventListener('click', function () {
      openHandoff(payload.messageId || state.lastMessageId);
    });
    card.appendChild(box);
    scrollBottom();
  }

  /* ---------- 降级卡（工具失败：固定话术 + 转人工，绝不显示"半成品答案"） ---------- */
  function renderDegraded(aiMsg, payload) {
    var card = aiMsg.querySelector('.w-ai-card');
    if (!card) return;
    card.innerHTML = '';
    card.appendChild(aiHead(brandName()));
    if (payload.messageId) state.lastMessageId = payload.messageId;

    var box = el('div', 'w-refuse w-refuse--gap');
    box.innerHTML =
      '<div>' + I.xCircle + '</div>' +
      '<div><div class="w-refuse__title">Our order system did not respond</div>' +
      '<p>' + textHtml(payload.text || '') + '</p>' +
      '<button class="w-refuse__btn" data-act="handoff">' + I.headphone +
      'Talk to a human agent</button></div>';
    box.querySelector('[data-act="handoff"]').addEventListener('click', function () {
      openHandoff(payload.messageId || state.lastMessageId);
    });
    card.appendChild(box);
    scrollBottom();
  }

  /* ---------- 转人工（真实 POST /widget/handoff；字段全部来自后端快照） ---------- */
  function renderCarry(data) {
    var ctx = data.context || {};
    var id = state.engineer;
    var idLine = id && id.identified
      ? id.name + ' (' + id.account + ') · source: ' + (id.sourceLabel || id.source)
      : (id && id.claimed ? 'Account ' + id.claimed + ' not activated' : 'Not signed in')
        + ' (' + (id ? (id.sourceLabel || id.source) : 'anonymous') + ')';
    var rounds = ctx.rounds || 0;
    var cited = ctx.citedCount || 0;
    var handoffs = ctx.handoffCount || 0;
    var rows = [
      ['Order', data.orderNo || data.ticketNo || 'Not provided by host'],
      ['Country / language', (ctx.country || '—') + ' / ' + (ctx.locale || '—')],
      ['Identity', idLine],
      ['Turns', rounds ? String(rounds) : '—'],
      ['Sources cited', cited ? cited + ' (knowledge base passages)' : 'None'],
      ['Handoffs', handoffs ? handoffs + ' (including this one)' : '1 (this one)']
    ];
    wCarryList.innerHTML = rows.map(function (row) {
      return '<li><span>' + escapeHtml(row[0]) + '</span><b>' + escapeHtml(row[1]) + '</b></li>';
    }).join('');
  }

  async function openHandoff(messageId) {
    var sessionId;
    try {
      sessionId = await ensureSession();
    } catch (e) {
      toast(I.alert + 'No session yet - cannot transfer to a human agent.');
      return;
    }
    try {
      var data = await window.WidgetAPI.handoff(sessionId, messageId || null);
      renderCarry(data);
      wHotline.textContent = data.hotline || '—';
      wHotlineMeta.textContent = data.hours ? 'Support hours: ' + data.hours : 'Support hours not provided';
      wHotlineNote.textContent = data.text || (state.config && state.config.handoffText) || '';
      modal.hidden = false;
      if (window.parent !== window) {
        window.parent.postMessage({ type: 'moqian:handoff', payload: data }, '*');
      }
    } catch (e) {
      toast(I.alert + 'Transfer failed: ' + escapeHtml((e && e.message) || 'please retry'));
    }
  }

  wHandoffHead.addEventListener('click', function () { openHandoff(state.lastMessageId); });
  $('wHandoffCancel').addEventListener('click', function () { modal.hidden = true; });
  $('wHandoffOk').addEventListener('click', function () {
    modal.hidden = true;
    toast(I.headphone + 'Connected you to the on-call support channel.');
  });
  modal.addEventListener('click', function (e) { if (e.target === modal) modal.hidden = true; });

  /* ---------- 输入区 ---------- */
  function autoGrow() {
    wInput.style.height = 'auto';
    wInput.style.height = Math.min(wInput.scrollHeight, 88) + 'px';
  }
  wInput.addEventListener('input', function () { autoGrow(); syncSendState(); });
  wInput.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      sendQuestion();
    }
  });
  wSend.addEventListener('click', function () { sendQuestion(); });
  wClose.addEventListener('click', function () {
    if (window.parent !== window) window.parent.postMessage({ type: 'moqian:close' }, '*');
  });

  /* ---------- 宿主通信 ---------- */
  var bootStarted = false;
  var bootTimer = null;

  function applyContextMessage(context, identity) {
    state.hostContext = context || {};
    state.identity = identity || { source: 'host_injected', claimedEngineerId: null };
    state.sessionId = null;
    state.engineer = null;
    state.gateBlocked = false;
    state.lastMessageId = null;
    state.msgCount = 0;
    state.citeOrder = [];
    state.citations = {};
    state.evidenceCache = {};
    state.clarified = {};
    wBody.innerHTML = '';
    renderIdentity();
    renderCtxLine();
    syncSendState();
  }

  window.addEventListener('message', function (event) {
    var data = event.data || {};
    if (data.type === 'moqian:context') {
      applyContextMessage(data.context, data.identity);
      if (!bootStarted) {
        if (bootTimer) clearTimeout(bootTimer);
        bootTimer = null;
        bootStarted = true;
        loadService(false);
      } else {
        loadService(true);
      }
    } else if (data.type === 'moqian:open') {
      wInput.focus();
    } else if (data.type === 'moqian:fault') {
      // 演示故障注入：只影响下一次/后续 /chat 的请求头，不改任何业务逻辑
      state.demoFault = data.fault || null;
    }
  });

  window.addEventListener('offline', function () {
    state.offline = true;
    renderOffline();
    if (state.sending && state.abort) {
      try { state.abort.abort(); } catch (e) { /* 已在错误分支处理 */ }
    }
  });
  window.addEventListener('online', function () {
    var wasOffline = state.offline;
    state.offline = false;
    renderOffline();
    if (!wasOffline) return;
    var pending = state.pendingRetry;
    state.pendingRetry = null;
    if (pending && !state.sending) {
      toast(I.check + 'Network is back - retrying your question.');
      sendTurn(pending.text, pending.node, pending.clarify);
    } else {
      toast(I.check + 'Network is back - you can keep asking.');
    }
  });

  /* ---------- 启动 ---------- */
  function readBootContext() {
    /* 独立打开（无宿主）时可用 URL 参数模拟宿主上下文，便于演示与联调：
       widget.html?orderNo=WB-10001&country=US&locale=en&engineerId=ENG-006 */
    var ctx = {};
    ['orderNo', 'country', 'locale', 'ticketNo', 'deviceModel', 'deviceName', 'batch'].forEach(function (key) {
      var value = params.get(key);
      if (value) ctx[key] = value;
    });
    if (Object.keys(ctx).length) state.hostContext = ctx;

    var engineerId = params.get('engineerId');
    if (engineerId) {
      state.identity = {
        source: params.get('source') || 'host_injected',
        claimedEngineerId: engineerId
      };
    } else if (params.get('source')) {
      state.identity = { source: params.get('source'), claimedEngineerId: null };
    }
  }

  function boot() {
    readBootContext();
    state.offline = !navigator.onLine;
    renderIdentity();
    renderCtxLine();
    renderOffline();
    syncSendState();

    /* 先告诉宿主 iframe 已就绪：loader 会立刻 flush 暂存的上下文；
       给这条 postMessage 留一个消息回合再自行启动，避免先建一个无上下文的会话。 */
    if (window.parent !== window) {
      window.parent.postMessage({ type: 'moqian:ready' }, '*');
    }
    bootTimer = setTimeout(function () {
      bootTimer = null;
      if (bootStarted) return;
      bootStarted = true;
      loadService(false);
    }, 200);
  }

  boot();
})();
