/* ============================================================
 * conversation.js — 会话详情页控制器（DOM / 交互）
 *
 * 数据来源：GET /admin/conversations/{id}（证据包），本文件不直接拼接口。
 * 页面职责：消息流 + 引用原文（按需展开）+ 工具调用 + 转人工记录 + 证据导出。
 * 导出走 adapters/conversations-api.js 的 download()，带 JWT 取 blob。
 * ============================================================ */
(function () {
  "use strict";
  window.AdminPages = window.AdminPages || {};

  function initConversation() {
    const UI = window.UI || {};
    const API = window.ConversationsAPI;
    const summaryEl = document.getElementById("convSummary");
    const summaryCard = document.getElementById("convSummaryCard");
    const messagesEl = document.getElementById("convMessages");
    const messagesCard = document.getElementById("convMessagesCard");
    const handoffsEl = document.getElementById("convHandoffs");
    const handoffsCard = document.getElementById("convHandoffsCard");
    const docsEl = document.getElementById("convDocs");
    const docsCard = document.getElementById("convDocsCard");
    const integrityEl = document.getElementById("convIntegrity");
    const errorEl = document.getElementById("convError");
    const errorTextEl = document.getElementById("convErrorText");
    const btnJson = document.getElementById("exportJson");
    const btnMd = document.getElementById("exportMd");
    if (!messagesEl || !API) return;

    const esc = UI.esc || function (value) { return String(value == null ? "" : value); };
    const params = new URLSearchParams(location.search);
    const conversationId = params.get("id");
    let packageData = null;

    const STATUS_TAG = {
      ok: '<span class="tag tag--green">ok</span>',
      not_found: '<span class="tag tag--gray">未找到</span>',
      timeout: '<span class="tag tag--red">超时</span>',
      error: '<span class="tag tag--red">失败</span>',
    };

    function fmtTime(value) {
      if (!value) return "—";
      const date = new Date(value);
      if (isNaN(date.getTime())) return String(value);
      return date.toLocaleString("zh-CN", { hour12: false });
    }

    function showError(message) {
      if (errorEl) errorEl.hidden = false;
      if (errorTextEl) errorTextEl.textContent = message;
    }

    function evidenceMap() {
      const map = {};
      (packageData.evidence || []).forEach(function (item) {
        map[item.evidenceId] = item;
      });
      return map;
    }

    /* ---------------- 摘要 ---------------- */
    function renderSummary(pkg) {
      const session = pkg.session || {};
      const messages = pkg.messages || [];
      const toolCount = messages.reduce(function (sum, msg) {
        return sum + ((msg.toolCalls || []).length);
      }, 0);
      const handoffCount = (pkg.handoffs || []).length;
      const items = [
        ["会话号", esc(session.id || "—")],
        ["开始时间", esc(fmtTime(session.createdAt))],
        ["渠道", esc((API.CHANNEL_LABEL[session.channel] || session.channel || "—"))],
        ["国家", esc(session.country || "—")],
        ["语言", esc(session.locale ? (API.LOCALE_LABEL[session.locale] || session.locale) : "—")],
        ["订单号", session.orderNo ? '<span class="tag tag--gray">' + esc(session.orderNo) + "</span>" : "—"],
        ["消息 / 工具", messages.length + " / " + toolCount],
        ["转人工", handoffCount ? '<span class="tag tag--amber">' + handoffCount + " 次</span>" : "—"],
      ];
      summaryEl.innerHTML = items.map(function (item) {
        return '<div class="conv-summary__item"><div class="conv-summary__label">' +
          esc(item[0]) + "</div><div class=\"conv-summary__value\">" + item[1] + "</div></div>";
      }).join("");
      summaryCard.hidden = false;
      if (integrityEl) {
        const digest = (pkg.integrity && pkg.integrity.sha256) || "";
        integrityEl.textContent = digest
          ? "sha256 " + digest.slice(0, 12) + "… · 导出时间 " + fmtTime(pkg.exportedAt)
          : "导出时间 " + fmtTime(pkg.exportedAt);
      }
    }

    /* ---------------- 消息流 ---------------- */
    function citationHtml(citation, evidence) {
      const evidenceId = citation.evidenceId || "";
      const excerpt = evidence[evidenceId];
      const title = (excerpt && excerpt.documentTitle) || citation.title || "知识库";
      const loc = citation.loc || (excerpt && excerpt.loc) || "";
      const version = excerpt && excerpt.version ? " · v" + excerpt.version : "";
      const body = excerpt
        ? esc(excerpt.contentExcerpt || "") + (excerpt.truncated ? "…" : "")
        : "（导出包内没有这条证据的原文快照）";
      return (
        '<div class="conv-cite">' +
        '<button class="conv-cite__head" type="button" aria-expanded="false">' +
        '<span class="tag tag--gray">' + esc(evidenceId) + "</span>" +
        "<span>" + esc(title) + (loc ? " · " + esc(loc) : "") + esc(version) + "</span>" +
        '<span class="conv-cite__more">展开原文</span></button>' +
        '<div class="conv-cite__excerpt" hidden>' + body + "</div>" +
        "</div>"
      );
    }

    function toolHtml(call) {
      const status = STATUS_TAG[call.status] || esc(call.status || "—");
      const args = call.args && Object.keys(call.args).length
        ? Object.keys(call.args).map(function (key) {
            return key + "=" + call.args[key];
          }).join(", ")
        : "—";
      const latency = call.latencyMs == null ? "—" : call.latencyMs + " ms";
      const result = call.result
        ? esc(JSON.stringify(call.result))
        : "—";
      return (
        '<div class="conv-tool">' +
        "<div><b>" + esc(call.tool || "tool") + "</b> " + status +
        ' <span class="sub">' + esc(args) + " · " + esc(latency) + "</span></div>" +
        '<div class="conv-tool__result">' + result + "</div>" +
        "</div>"
      );
    }

    function messageHtml(message, evidence) {
      const isUser = message.role === "user";
      const tags = [];
      if (message.gateReason) {
        tags.push('<span class="tag tag--amber">' + esc(message.gateReason) + "</span>");
      }
      if (message.refused) tags.push('<span class="tag tag--red">refused</span>');
      if (message.intent) tags.push('<span class="tag tag--gray">' + esc(message.intent) + "</span>");
      if (message.handoff) tags.push('<span class="tag tag--amber">转人工</span>');

      const citations = (message.citations || []).map(function (citation) {
        return citationHtml(citation, evidence);
      }).join("");
      const tools = (message.toolCalls || []).map(toolHtml).join("");
      const handoff = message.handoff
        ? '<div class="conv-tool"><b>转人工</b> <span class="sub">' +
          esc((message.handoff.context && message.handoff.context.reason) || "handoff") +
          " · " + esc(fmtTime(message.handoff.createdAt)) + "</span></div>"
        : "";

      return (
        '<div class="conv-msg conv-msg--' + (isUser ? "user" : "ai") + '">' +
        '<div class="conv-msg__head">' +
        '<span class="conv-role">' + (isUser ? "访客" : "助手") + "</span>" +
        '<span class="sub">' + esc(message.id || "") + " · " + esc(fmtTime(message.createdAt)) + "</span>" +
        tags.join("") +
        "</div>" +
        '<div class="conv-msg__body">' + esc(message.content || "") + "</div>" +
        (tools ? '<div class="conv-tools">' + tools + "</div>" : "") +
        (citations ? '<div class="conv-cites">' + citations + "</div>" : "") +
        handoff +
        "</div>"
      );
    }

    function renderMessages(pkg) {
      const evidence = evidenceMap();
      const messages = pkg.messages || [];
      messagesEl.innerHTML = messages.map(function (message) {
        return messageHtml(message, evidence);
      }).join("");
      messagesCard.hidden = messages.length === 0;

      messagesEl.addEventListener("click", function (event) {
        const head = event.target.closest(".conv-cite__head");
        if (!head) return;
        const body = head.parentElement.querySelector(".conv-cite__excerpt");
        if (!body) return;
        const open = body.hidden;
        body.hidden = !open;
        head.setAttribute("aria-expanded", open ? "true" : "false");
        head.querySelector(".conv-cite__more").textContent = open ? "收起原文" : "展开原文";
      });
    }

    function renderHandoffs(pkg) {
      const handoffs = pkg.handoffs || [];
      if (!handoffs.length) {
        handoffsCard.hidden = true;
        return;
      }
      handoffsEl.innerHTML = handoffs.map(function (item) {
        const context = item.context || {};
        return (
          '<div class="conv-tool">' +
          "<div><b>" + esc(context.reason || "handoff") + "</b>" +
          ' <span class="sub">' + esc(item.messageId || "—") + " · " +
          esc(fmtTime(item.createdAt)) + "</span></div>" +
          '<div class="conv-tool__result">' + esc(JSON.stringify(context)) + "</div>" +
          "</div>"
        );
      }).join("");
      handoffsCard.hidden = false;
    }

    function renderDocuments(pkg) {
      const documents = pkg.documents || [];
      if (!documents.length) {
        docsCard.hidden = true;
        return;
      }
      docsEl.innerHTML = documents.map(function (doc) {
        return (
          '<div class="conv-tool"><div><b>' + esc(doc.title || doc.docCode || "文档") + "</b>" +
          ' <span class="sub">' + esc(doc.docCode || "") +
          (doc.version ? " · v" + esc(doc.version) : "") +
          (doc.effectiveDate ? " · 生效 " + esc(doc.effectiveDate) : "") +
          " · " + esc(doc.status || "") + "</span></div></div>"
        );
      }).join("");
      docsCard.hidden = false;
    }

    /* ---------------- 导出 ---------------- */
    function download(format) {
      const button = format === "md" ? btnMd : btnJson;
      if (!button || !packageData) return;
      const original = button.innerHTML;
      button.disabled = true;
      button.textContent = "导出中…";
      API.download(conversationId, format).then(function (result) {
        const url = URL.createObjectURL(result.blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = result.filename;
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
        if (UI.toast) UI.toast("已导出 " + result.filename);
      }).catch(function (err) {
        if (UI.toast) UI.toast("导出失败：" + ((err && err.message) || "未知错误"));
      }).finally(function () {
        button.disabled = false;
        button.innerHTML = original;
      });
    }

    if (btnJson) btnJson.addEventListener("click", function () { download("json"); });
    if (btnMd) btnMd.addEventListener("click", function () { download("md"); });

    if (!conversationId) {
      showError("URL 缺少会话号（应为 conversation.html?id=ses-3）");
      return;
    }

    API.detail(conversationId).then(function (pkg) {
      packageData = pkg;
      errorEl.hidden = true;
      renderSummary(pkg);
      renderMessages(pkg);
      renderHandoffs(pkg);
      renderDocuments(pkg);
      if (btnJson) btnJson.disabled = false;
      if (btnMd) btnMd.disabled = false;
    }).catch(function (err) {
      showError("加载失败：" + ((err && err.message) || "未知错误"));
      if (UI.toast) UI.toast("会话详情加载失败：" + ((err && err.message) || "未知错误"));
    });
  }

  window.AdminPages.conversation = initConversation;
})();
