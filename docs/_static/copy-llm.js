(() => {
  "use strict";

  const copyText = async (value) => {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(value);
      return;
    }
    const helper = document.createElement("textarea");
    helper.value = value;
    helper.setAttribute("readonly", "");
    helper.style.position = "fixed";
    helper.style.opacity = "0";
    document.body.appendChild(helper);
    helper.select();
    document.execCommand("copy");
    helper.remove();
  };

  const documentationRoot = () => {
    const contentRoot = document.documentElement.dataset.content_root || "./";
    return new URL(contentRoot, window.location.href);
  };

  const setStatus = (status, message) => {
    status.textContent = message;
    window.setTimeout(() => {
      status.textContent = "";
    }, 3000);
  };

  const attachToolbar = () => {
    const article = document.querySelector("article[role='main']");
    if (!article || article.dataset.llmToolbar === "ready") {
      return;
    }
    article.dataset.llmToolbar = "ready";

    const toolbar = document.createElement("div");
    toolbar.className = "llm-toolbar";
    toolbar.setAttribute("aria-label", "LLM documentation tools");

    const copyPage = document.createElement("button");
    copyPage.type = "button";
    copyPage.textContent = "Copy this page";

    const copyAll = document.createElement("button");
    copyAll.type = "button";
    copyAll.textContent = "Copy all docs for an LLM";

    const llmGuide = document.createElement("a");
    llmGuide.textContent = "LLM reference";
    llmGuide.href = new URL("for-llms/", documentationRoot()).href;

    const status = document.createElement("span");
    status.className = "llm-copy-status";
    status.setAttribute("aria-live", "polite");

    copyPage.addEventListener("click", async () => {
      try {
        const clone = article.cloneNode(true);
        clone.querySelector(".llm-toolbar")?.remove();
        await copyText(clone.innerText.trim());
        setStatus(status, "Page copied");
      } catch (error) {
        setStatus(status, "Copy failed");
        console.error(error);
      }
    });

    copyAll.addEventListener("click", async () => {
      try {
        const response = await fetch(new URL("llms-full.txt", documentationRoot()));
        if (!response.ok) {
          throw new Error(`Cannot load LLM reference: ${response.status}`);
        }
        await copyText(await response.text());
        setStatus(status, "Full docs copied");
      } catch (error) {
        setStatus(status, "Copy failed");
        console.error(error);
      }
    });

    toolbar.append(copyPage, copyAll, llmGuide, status);
    article.prepend(toolbar);
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", attachToolbar);
  } else {
    attachToolbar();
  }
})();
