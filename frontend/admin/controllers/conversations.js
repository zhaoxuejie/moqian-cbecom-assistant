/* ============================================================
 * conversations.js — 会话列表页控制器（DOM / 交互）
 *
 * 数据来源：adapters/conversations-api.js（契约 → 页面结构），本文件不直接 fetch。
 * 口径：列表是**会话级**（一次访问一行），筛选走后端（country/language/handoffReason/q），
 *       分页也走后端；右侧「查看详情」进 conversation.html 导出举证材料。
 * ============================================================ */
(function () {
  "use strict";
  window.AdminPages = window.AdminPages || {};

  function initConversations() {
    const UI = window.UI || {};
    const API = window.ConversationsAPI;
    const tbody = document.getElementById("convRows");
    const emptyEl = document.getElementById("convEmpty");
    const noteEl = document.getElementById("convNote");
    const pagerEl = document.getElementById("convPager");
    const searchInput = document.getElementById("convSearch");
    const countrySel = document.getElementById("convCountry");
    const languageSel = document.getElementById("convLanguage");
    const handoffSel = document.getElementById("convHandoff");
    const resetBtn = document.getElementById("convReset");
    const refreshBtn = document.getElementById("convRefresh");
    if (!tbody || !emptyEl || !API) return;

    const esc = UI.esc || function (value) { return String(value == null ? "" : value); };
    const PAGE_SIZE_KEY = "moqian_conversations_page_size";

    const state = {
      page: 1,
      pageSize: UI.readPageSize ? UI.readPageSize(PAGE_SIZE_KEY, 20) : 20,
      q: "",
      country: "",
      language: "",
      handoffReason: "",
      total: 0,
    };
    let loadSeq = 0;
    let searchTimer = null;

    function fmtTime(value) {
      if (!value) return "—";
      const date = new Date(value);
      if (isNaN(date.getTime())) return String(value);
      return date.toLocaleString("zh-CN", { hour12: false });
    }

    function reasonLabel(reason) {
      if (!reason) return null;
      return API.HANDOFF_REASON_LABEL[reason] || reason;
    }

    function rowHtml(item) {
      const locale = item.locale ? (API.LOCALE_LABEL[item.locale] || item.locale) : "—";
      const channel = API.CHANNEL_LABEL[item.channel] || item.channel || "—";
      const reason = reasonLabel(item.handoffReason);
      const handoff = item.handoffCount
        ? '<span class="tag tag--amber">' + item.handoffCount + ' 次</span>'
        : '<span class="sub">—</span>';
      const order = item.orderNo
        ? '<span class="tag tag--gray">' + esc(item.orderNo) + '</span>'
        : '<span class="sub">未提供</span>';
      return (
        '<tr data-id="' + esc(item.id) + '">' +
        '<td><div class="col-title">' + esc(item.id) + '</div>' +
        '<div class="sub">' + esc(channel) + '</div></td>' +
        '<td>' + esc(fmtTime(item.createdAt)) + '</td>' +
        '<td>' + esc(item.country || "—") +
        ' <span class="sub">/ ' + esc(locale) + '</span></td>' +
        '<td>' + order + '</td>' +
        '<td class="num">' + Number(item.messageCount || 0) + '</td>' +
        '<td class="num">' + Number(item.toolCount || 0) + '</td>' +
        '<td>' + handoff + '</td>' +
        '<td>' + (reason ? '<span class="tag tag--gray">' + esc(reason) + '</span>'
          : '<span class="sub">—</span>') + '</td>' +
        '<td class="cell-actions">' +
        '<a class="btn btn--ghost btn--sm" href="conversation.html?id=' +
        encodeURIComponent(item.id) + '">查看详情</a></td>' +
        '</tr>'
      );
    }

    function renderPager() {
      if (!pagerEl || !UI.pager) return;
      UI.pager(pagerEl, state.total, state.page, state.pageSize, function (page, pageSize) {
        state.page = page;
        state.pageSize = pageSize;
        load();
      }, PAGE_SIZE_KEY);
    }

    function load() {
      const seq = ++loadSeq;
      if (noteEl) noteEl.textContent = "加载中…";
      API.list({
        page: state.page,
        pageSize: state.pageSize,
        q: state.q,
        country: state.country,
        language: state.language,
        handoffReason: state.handoffReason,
      }).then(function (data) {
        if (seq !== loadSeq) return;
        state.total = data.pagination.total;
        state.page = data.pagination.page;
        tbody.innerHTML = data.items.map(rowHtml).join("");
        emptyEl.hidden = data.items.length > 0;
        if (noteEl) {
          noteEl.textContent = data.items.length
            ? "共 " + state.total + " 个会话"
            : "当前筛选下没有会话";
        }
        renderPager();
      }).catch(function (err) {
        if (seq !== loadSeq) return;
        tbody.innerHTML = "";
        emptyEl.hidden = false;
        if (noteEl) noteEl.textContent = (err && err.message) || "加载失败";
        if (UI.toast) UI.toast("会话列表加载失败：" + ((err && err.message) || "未知错误"));
      });
    }

    function resetFilters() {
      state.page = 1;
      state.q = "";
      state.country = "";
      state.language = "";
      state.handoffReason = "";
      if (searchInput) searchInput.value = "";
      if (countrySel) countrySel.value = "";
      if (languageSel) languageSel.value = "";
      if (handoffSel) handoffSel.value = "";
      load();
    }

    if (searchInput) {
      searchInput.addEventListener("input", function () {
        if (searchTimer) clearTimeout(searchTimer);
        searchTimer = setTimeout(function () {
          state.q = searchInput.value.trim();
          state.page = 1;
          load();
        }, 300);
      });
    }
    [countrySel, languageSel, handoffSel].forEach(function (sel, index) {
      if (!sel) return;
      sel.addEventListener("change", function () {
        if (index === 0) state.country = sel.value;
        if (index === 1) state.language = sel.value;
        if (index === 2) state.handoffReason = sel.value;
        state.page = 1;
        load();
      });
    });
    if (resetBtn) resetBtn.addEventListener("click", resetFilters);
    if (refreshBtn) refreshBtn.addEventListener("click", load);

    /* 行点击也能进详情（操作列是链接，点链接时不重复跳转） */
    tbody.addEventListener("click", function (event) {
      if (event.target.closest("a,button")) return;
      const row = event.target.closest("tr[data-id]");
      if (!row) return;
      location.href = "conversation.html?id=" + encodeURIComponent(row.dataset.id);
    });

    load();
  }

  window.AdminPages.conversations = initConversations;
})();
