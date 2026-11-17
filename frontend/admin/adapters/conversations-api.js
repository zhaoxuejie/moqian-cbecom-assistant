/* ============================================================
 * conversations-api.js — 会话列表 / 会话详情 / 证据导出 适配层
 *
 * 后端（P5-1，app/routers/admin_conversations.py）：
 *   GET /admin/conversations?country&language&handoffReason&q&page&pageSize
 *   GET /admin/conversations/{id}                 → 证据包（session/messages/evidence/documents/handoffs/integrity）
 *   GET /admin/conversations/{id}/export?format=json|md → 附件下载
 *
 * 约定：页面控制器只操作 DOM；本文件负责契约 → 页面结构、以及带鉴权的文件下载。
 * ============================================================ */
(function () {
  "use strict";

  var API = window.AdminAPI;
  if (!API) throw new Error("conversations-api.js 需要先加载 real-api.js");

  /* 转人工原因 → 中文标签（与 app/handoff_rules.py 的分类一致） */
  var HANDOFF_REASON_LABEL = {
    refund: "退款",
    complaint: "投诉",
    legal: "法务",
    account: "账户安全",
    consecutive_failures: "连续失败",
    write_request: "写操作诉求",
    keyword: "规则命中",
  };

  var CHANNEL_LABEL = {
    widget: "访客浮层",
    admin_preview: "后台自测",
  };

  var LOCALE_LABEL = { en: "英语", es: "西语", fr: "法语", de: "德语" };

  function normalizePage(data) {
    var pagination = (data && data.pagination) || {};
    return {
      items: (data && data.items) || [],
      pagination: {
        page: Number(pagination.page || 1),
        pageSize: Number(pagination.pageSize || 20),
        total: Number(pagination.total || 0),
      },
    };
  }

  function list(params) {
    return API.request("GET", "/conversations", { query: params || {} }).then(normalizePage);
  }

  function detail(id) {
    return API.request("GET", "/conversations/" + encodeURIComponent(id));
  }

  /** 下载证据包：带 JWT 取 blob，文件名取后端 Content-Disposition。 */
  function download(id, format) {
    var token = API.auth.token();
    var url = API.apiUrl("/conversations/" + encodeURIComponent(id) + "/export", {
      format: format,
    });
    return fetch(url, {
      headers: token ? { Authorization: "Bearer " + token } : {},
    }).then(function (res) {
      if (!res.ok) {
        return res.text().then(function (text) {
          var message = "导出失败（HTTP " + res.status + "）";
          try {
            var data = JSON.parse(text);
            if (data && data.message) message = data.message;
          } catch (e) { /* 非 JSON 响应保留默认提示 */ }
          throw { status: res.status, message: message };
        });
      }
      var disposition = res.headers.get("Content-Disposition") || "";
      var matched = /filename="?([^";]+)"?/.exec(disposition);
      var filename = matched
        ? matched[1]
        : "conversation-" + id + (format === "md" ? ".md" : ".json");
      return res.blob().then(function (blob) {
        return { blob: blob, filename: filename };
      });
    });
  }

  window.ConversationsAPI = {
    list: list,
    detail: detail,
    download: download,
    HANDOFF_REASON_LABEL: HANDOFF_REASON_LABEL,
    CHANNEL_LABEL: CHANNEL_LABEL,
    LOCALE_LABEL: LOCALE_LABEL,
  };
})();
