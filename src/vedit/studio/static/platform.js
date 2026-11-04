"use strict";

/* The keyboard this person has. Mac shortcuts say Cmd; every other platform says Ctrl.
   Loaded first, so the shortcut list and the timeline's tooltips name the right key. */
(function (root) {
  const nav = root.navigator || {};
  const name = String((nav.userAgentData && nav.userAgentData.platform) || nav.platform || nav.userAgent || "");
  const mac = /mac|iphone|ipad|ipod/i.test(name);
  const api = { mac, mod: mac ? "Cmd" : "Ctrl" };
  root.BriefcutPlatform = api;
  if (root.document) root.document.querySelectorAll("kbd[data-mod]").forEach((node) => { node.textContent = api.mod; });
  if (typeof module === "object" && module.exports) module.exports = api;
})(typeof window === "undefined" ? globalThis : window);
