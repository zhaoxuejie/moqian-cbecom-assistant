/**
 * i18n.js — 后台轻量中英切换（演示用，不引框架）。
 *
 * 两层机制：
 * 1) `data-i18n="key"`：静态文案，精确控制（导航、登录）；
 * 2) `TEXT_MAP`：按中文原文整句替换文本节点，覆盖页面里的表头/按钮/空态，
 *    页面上新渲染的节点由 MutationObserver 继续翻译（只在 en 模式下工作）。
 *
 * 默认中文；切换后 localStorage 记忆并刷新。没收录的句子保持中文——
 * 宁可少翻，不要机翻糊弄。
 */
(function () {
  "use strict";

  var DICT = {
    en: {
      "app.title": "Support Agent Admin",
      "nav.groupOps": "Operations",
      "nav.groupGovern": "Governance",
      "nav.groupMe": "Me",
      "nav.sources": "Knowledge sources",
      "nav.jobs": "Ingestion jobs",
      "nav.settle": "Knowledge review",
      "nav.conversations": "Conversations",
      "nav.gaps": "Knowledge gaps",
      "nav.eval": "Evaluation",
      "nav.dashboard": "Ops dashboard",
      "nav.users": "Users",
      "nav.config": "Settings",
      "nav.account": "My account",
      "nav.my-qa": "My questions",
      "nav.my-save": "My saved",
      "role.admin": "Admin",
      "role.user": "User",
      "common.logout": "Sign out",
      "common.lang": "中文",
      "login.title": "Sign in · Support Agent Admin",
      "login.h1": "Sign in",
      "login.account": "Account",
      "login.password": "Password",
      "login.submit": "Sign in",
      "dashboard.title": "Ops dashboard",
    },
    zh: {
      "app.title": "客服智能体管理台",
      "nav.groupOps": "运营",
      "nav.groupGovern": "治理",
      "nav.groupMe": "我的",
      "nav.sources": "知识源管理",
      "nav.jobs": "解析任务",
      "nav.settle": "知识沉淀",
      "nav.conversations": "会话管理",
      "nav.gaps": "知识缺口",
      "nav.eval": "评测面板",
      "nav.dashboard": "主管驾驶舱",
      "nav.users": "用户管理",
      "nav.config": "系统配置",
      "nav.account": "我的账号",
      "nav.my-qa": "我的问答",
      "nav.my-save": "我的收藏",
      "role.admin": "管理员",
      "role.user": "普通用户",
      "common.logout": "退出",
      "common.lang": "EN",
      "login.title": "登录 · 客服智能体管理台",
      "login.h1": "登录",
      "login.account": "账号",
      "login.password": "密码",
      "login.submit": "登 录",
      "dashboard.title": "主管驾驶舱",
    },
  };

  /* 中文原文 → 英文（整句匹配，覆盖演示会扫到的表头/按钮/空态） */
  var TEXT_MAP = {
    "全部": "All",
    "搜索": "Search",
    "筛选": "Filter",
    "刷新": "Refresh",
    "导出": "Export",
    "导出 JSON": "Export JSON",
    "导出 Markdown": "Export Markdown",
    "查看详情": "View details",
    "返回列表": "Back to list",
    "知识源管理": "Knowledge sources",
    "知识源": "Knowledge sources",
    "解析任务": "Ingestion jobs",
    "知识沉淀": "Knowledge review",
    "知识缺口": "Knowledge gaps",
    "会话管理": "Conversations",
    "会话详情": "Conversation",
    "会话列表": "Conversations",
    "评测面板": "Evaluation",
    "主管驾驶舱": "Ops dashboard",
    "用户管理": "Users",
    "系统配置": "Settings",
    "我的账号": "My account",
    "我的问答": "My questions",
    "我的收藏": "My saved",
    "运营": "Operations",
    "治理": "Governance",
    "我的": "Mine",
    "退出": "Sign out",
    "登录": "Sign in",
    "账号": "Account",
    "密码": "Password",
    "国家": "Country",
    "国家 / 语言": "Country / language",
    "语言": "Language",
    "订单号": "Order",
    "消息数": "Messages",
    "工具调用": "Tool calls",
    "转人工": "Handoff",
    "转人工原因": "Handoff reason",
    "原因": "Reason",
    "时间": "Time",
    "渠道": "Channel",
    "身份": "Identity",
    "文档总数": "Documents",
    "切片总数": "Chunks",
    "今日问答": "Questions today",
    "知识缺口数": "Open gaps",
    "近 7 天新增": "Added in 7 days",
    "问题": "Question",
    "答案": "Answer",
    "引用": "Citations",
    "步骤": "Steps",
    "状态": "Status",
    "层": "Layer",
    "期望": "Expectation",
    "关键词": "Keywords",
    "用例": "Cases",
    "用例集": "Case set",
    "评测用例集": "Evaluation cases",
    "评测执行与快照": "Runs & snapshots",
    "分层覆盖": "Layer coverage",
    "失败清单": "Failures",
    "指标": "Metrics",
    "值": "Value",
    "暂无评测批次": "No evaluation runs yet",
    "评测集为空": "No evaluation cases",
    "知识源列表": "Knowledge sources",
    "上传": "Upload",
    "归档": "Archive",
    "启用": "Enable",
    "停用": "Disable",
    "恢复": "Restore",
    "保存": "Save",
    "取消": "Cancel",
    "确认": "Confirm",
    "关闭": "Close",
    "加载中…": "Loading…",
    "没有数据": "No data",
    "无数据": "No data",
  };

  var KEY = "admin_lang";
  var original = new WeakMap();
  var observer = null;

  function lang() {
    var saved = "";
    try { saved = localStorage.getItem(KEY) || ""; } catch (e) { saved = ""; }
    return saved === "en" ? "en" : "zh";
  }

  function t(key, fallback) {
    var table = DICT[lang()] || DICT.zh;
    return (table[key] != null ? table[key] : (DICT.zh[key] != null ? DICT.zh[key] : (fallback != null ? fallback : key)));
  }

  function translateNode(node) {
    if (lang() !== "en") return;
    if (!node || !node.nodeValue) return;
    var raw = node.nodeValue;
    var text = raw.trim();
    if (!text || !TEXT_MAP[text]) return;
    if (!original.has(node)) original.set(node, raw);
    var lead = raw.slice(0, raw.indexOf(text));
    var tail = raw.slice(raw.indexOf(text) + text.length);
    var next = lead + TEXT_MAP[text] + tail;
    if (node.nodeValue !== next) node.nodeValue = next;
  }

  function walk(root) {
    if (!root) return;
    if (root.nodeType === 3) { translateNode(root); return; }
    if (root.nodeType !== 1 && root.nodeType !== 9) return;
    if (root.nodeType === 1) {
      var tag = (root.tagName || "").toLowerCase();
      if (tag === "script" || tag === "style" || tag === "textarea") return;
    }
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    var node = walker.nextNode();
    while (node) { translateNode(node); node = walker.nextNode(); }
  }

  function apply(root) {
    var scope = root || document.body || document;
    scope.querySelectorAll && scope.querySelectorAll("[data-i18n]").forEach(function (el) {
      var key = el.getAttribute("data-i18n");
      if (key) el.textContent = t(key, el.textContent);
    });
    if (document.body && document.body.dataset.page === "login") {
      document.title = t("login.title");
    }
    document.documentElement.lang = lang() === "en" ? "en" : "zh-CN";
    var toggle = document.getElementById("langToggle");
    if (toggle) toggle.textContent = t("common.lang");
    walk(scope);
    if (lang() === "en" && !observer && document.body) {
      observer = new MutationObserver(function (mutations) {
        mutations.forEach(function (m) {
          if (m.type === "childList") {
            m.addedNodes.forEach(function (n) { walk(n); });
          } else if (m.type === "characterData") {
            translateNode(m.target);
          }
        });
      });
      observer.observe(document.body, { childList: true, subtree: true, characterData: true });
    }
  }

  function setLang(next) {
    var value = next === "en" ? "en" : "zh";
    try { localStorage.setItem(KEY, value); } catch (e) { /* 隐私模式忽略 */ }
    apply();
  }

  function toggle() {
    setLang(lang() === "en" ? "zh" : "en");
    window.location.reload();
  }

  window.AdminI18n = {
    t: t, lang: lang, apply: apply, setLang: setLang, toggle: toggle,
    dict: DICT, textMap: TEXT_MAP,
  };

  function bindToggle() {
    var toggleEl = document.getElementById("langToggle");
    if (toggleEl && !toggleEl.dataset.bound) {
      toggleEl.dataset.bound = "1";
      toggleEl.addEventListener("click", toggle);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { apply(); bindToggle(); });
  } else {
    apply();
    bindToggle();
  }
})();
